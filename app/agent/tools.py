import asyncio
import json
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from google.genai import types
from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent.schemas import Evidence, StrictInput, clean_text
from app.db import engine
from app.knowledge.embedding import Embedder, EmbeddingFailure, get_embedder
from app.knowledge.models import SourceType
from app.knowledge.search import calibrated_min_score, search_index
from app.models import Ticket, TicketEvent, User


class SearchArgs(StrictInput):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    limit: Annotated[int, Field(ge=1, le=5, strict=True)]


class TicketArgs(StrictInput):
    ticket_id: UUID


class TicketFilters(StrictInput):
    status: Literal["open", "in_progress", "waiting"] | None = None
    service: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = None
    limit: Annotated[int, Field(ge=1, le=5, strict=True)] = 5


class OpenArgs(StrictInput):
    filters: TicketFilters


SPECS = {
    "search_similar_tickets": (SearchArgs, "Find resolved historical incidents. A similar incident is not the current cause."),
    "search_sop": (SearchArgs, "Find SOP excerpts relevant to the incident and its diagnostic questions."),
    "get_ticket_details": (TicketArgs, "Read a ticket linked by the user or previously returned by an authorized tool. Use a UUID."),
    "get_ticket_history": (TicketArgs, "Read recent status and edit events for an in-scope ticket UUID."),
    "get_open_tickets": (OpenArgs, "List open incidents only when the user asks about current or related active incidents."),
}


def tool_declaration(specs=SPECS) -> types.Tool:
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(name=name, description=description, parameters_json_schema=schema.model_json_schema())
        for name, (schema, description) in specs.items()
    ])


@dataclass
class ToolOutcome:
    status: str
    payload: dict
    details: dict


def rejection(code: str, details: dict | None = None) -> ToolOutcome:
    return ToolOutcome("rejected", {"error": code}, {**(details or {}), "error_code": code})


def require_active_user(db: Session, user_id: UUID) -> None:
    if not db.scalar(select(User.id).where(User.id == user_id, User.is_active.is_(True))):
        raise PermissionError("Operator inactive")


class Toolbox:
    def __init__(self, user_id: UUID, ticket_id: UUID | None, provider: Embedder | None = None):
        self.user_id = user_id
        self.ticket_id = ticket_id
        self.allowed_tickets = {ticket_id} if ticket_id is not None else set()
        self.provider = provider
        self.evidence: dict[str, Evidence] = {}
        self._references: dict[tuple, str] = {}
        self._executed: set[tuple] = set()
        self._vectors: dict[str, list[float]] = {}
        self._search_attempts: dict[str, int] = {}
        self.embedding_requests = 0

    def _add_source(self, **data) -> Evidence:
        signature = data["kind"], data["source_id"], data.get("chunk_index"), data.get("ticket_version")
        if signature in self._references:
            return self.evidence[self._references[signature]]
        key = f"K{len(self.evidence) + 1}"
        item = Evidence(key=key, **data)
        self.evidence[key] = item
        self._references[signature] = key
        return item

    def _ticket_source(self, kind: str, ticket: Ticket, content: str, version: int | None = None) -> Evidence:
        return self._add_source(
            kind=kind, source_id=ticket.id, title=clean_text(ticket.title, 160), content=clean_text(content, 1200),
            source_url=f"/tickets/{ticket.id}", ticket_version=ticket.version if version is None else version,
        )

    def _read_tickets(self, name: str, args: TicketArgs | OpenArgs) -> list[Evidence]:
        with Session(engine()) as db:
            db.execute(text("SET LOCAL statement_timeout = '4s'"))
            require_active_user(db, self.user_id)
            if isinstance(args, OpenArgs):
                filters = args.filters
                stmt = select(Ticket).where(
                    Ticket.simulation_run_id.is_(None), Ticket.archived_at.is_(None), Ticket.status.in_(("open", "in_progress", "waiting"))
                )
                if filters.status is not None:
                    stmt = stmt.where(Ticket.status == filters.status)
                if filters.service is not None:
                    stmt = stmt.where(Ticket.service == filters.service)
                tickets = db.scalars(stmt.order_by(Ticket.updated_at.desc(), Ticket.id).limit(filters.limit)).all()
                self.allowed_tickets.update(ticket.id for ticket in tickets)
                return [
                    self._ticket_source("OPEN_TICKET", ticket, self._ticket_text(ticket, summary=True))
                    for ticket in tickets
                ]

            ticket = db.scalar(select(Ticket).where(Ticket.id == args.ticket_id, Ticket.simulation_run_id.is_(None)))
            if ticket is None:
                raise LookupError("Ticket missing")
            if name == "get_ticket_details":
                return [self._ticket_source("TICKET_DETAILS", ticket, self._ticket_text(ticket))]

            events = db.scalars(
                select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
                .order_by(TicketEvent.ticket_version.desc()).limit(8)
            ).all()
            return [
                self._ticket_source("TICKET_HISTORY", ticket, self._event_text(event), event.ticket_version)
                for event in events
            ]

    @staticmethod
    def _ticket_text(ticket: Ticket, *, summary: bool = False) -> str:
        parts = [f"티켓 {ticket.display_number} · 상태 {ticket.status} · 우선순위 {ticket.severity}", f"서비스: {ticket.service}"]
        if not summary:
            parts.extend([
                f"증상: {ticket.description[:500]}",
                f"발생 시각: {ticket.detected_at.isoformat()}",
                f"영향: {ticket.impact or '미기록'}",
                f"다음 조치: {ticket.next_action or '미기록'}",
                f"해결 요약: {ticket.resolution or '미기록'}",
            ])
        return "\n".join(parts)

    @staticmethod
    def _event_text(event: TicketEvent) -> str:
        details = event.details
        changes = details.get("changes", {})
        keys = set(changes) & {"title", "description", "severity", "status", "impact", "next_action", "resolution", "archived_at"}
        transition = changes.get("status", {})
        parts = [f"{event.created_at.isoformat()} · {event.event_type}", f"변경 필드: {', '.join(sorted(keys)) or '없음'}"]
        if transition:
            parts.append(f"상태: {transition.get('before')} → {transition.get('after')}")
        if reason := details.get("reason"):
            parts.append(f"기록된 사유: {reason}")
        return "\n".join(parts)

    async def _search(self, name: str, args: SearchArgs) -> list[Evidence]:
        with Session(engine()) as db:
            require_active_user(db, self.user_id)
        provider = self.provider or get_embedder()
        self.provider = provider
        query = clean_text(args.query, 300).strip()
        qkey = " ".join(query.casefold().split())
        if qkey not in self._vectors:
            self.embedding_requests += 1
            self._vectors[qkey] = await asyncio.to_thread(provider.query, query)
        category = SourceType.SOP if name == "search_sop" else SourceType.TICKET
        with Session(engine()) as db:
            db.execute(text("SET LOCAL statement_timeout = '4s'"))
            require_active_user(db, self.user_id)
            results = search_index(
                db, vector=self._vectors[qkey], profile=provider.profile.fingerprint, source_types=[category],
                limit=min(20, args.limit + 1) if self.ticket_id and category == SourceType.TICKET else args.limit,
                min_score=calibrated_min_score(provider.profile.fingerprint),
            )
        sources = []
        for result in results:
            if category == SourceType.TICKET:
                if result.source_id == self.ticket_id:
                    continue
                self.allowed_tickets.add(result.source_id)
            sources.append(self._add_source(
                kind="SOP" if category == SourceType.SOP else "SIMILAR_TICKET",
                source_id=result.source_id, title=clean_text(result.title, 160), content=clean_text(result.content, 900),
                source_url=result.source_url, score=result.score, chunk_index=result.chunk_index,
                page_number=result.page_number, section=clean_text(result.section, 200) if result.section else None,
                ticket_version=result.ticket_version,
            ))
            if len(sources) == args.limit:
                break
        return sources

    async def execute(self, name: str, raw: object) -> ToolOutcome:
        if name not in SPECS:
            return rejection("TOOL_NOT_ALLOWED")
        try:
            args = SPECS[name][0].model_validate(raw)
        except ValidationError:
            return rejection("TOOL_INPUT_INVALID")

        summary = {}
        if isinstance(args, SearchArgs):
            summary = {"query_length": len(args.query), "limit": args.limit}
            fingerprint = (name, " ".join(clean_text(args.query, 300).casefold().split()))
        elif isinstance(args, TicketArgs):
            if args.ticket_id not in self.allowed_tickets:
                return rejection("TICKET_OUT_OF_SCOPE")
            summary = {"ticket_id": str(args.ticket_id)}
            fingerprint = (name, args.ticket_id)
        else:
            summary = {"limit": args.filters.limit, "filter_fields": sorted(args.filters.model_fields_set)}
            fingerprint = (name, json.dumps(args.model_dump(mode="json"), sort_keys=True))

        if fingerprint in self._executed:
            return ToolOutcome("skipped", {"error": "REPEATED_CALL", "instruction": "Use previous evidence or reformulate."},
                               {**summary, "error_code": "REPEATED_CALL"})
        self._executed.add(fingerprint)
        if isinstance(args, SearchArgs):
            count = self._search_attempts.get(name, 0)
            if count >= 2:
                return rejection("TOOL_SEARCH_LIMIT", summary)
            self._search_attempts[name] = count + 1

        try:
            if isinstance(args, SearchArgs):
                sources = await self._search(name, args)
            else:
                sources = self._read_tickets(name, args)
        except PermissionError:
            return rejection("TOOL_UNAUTHORIZED", summary)
        except LookupError:
            return rejection("TICKET_NOT_FOUND", summary)
        except EmbeddingFailure as exc:
            return ToolOutcome("error", {"error": exc.code}, {**summary, "error_code": exc.code})
        except Exception:
            return ToolOutcome("error", {"error": "TOOL_UNAVAILABLE"}, {**summary, "error_code": "TOOL_UNAVAILABLE"})

        return ToolOutcome(
            "ok", {"untrusted_data": [source.model_dump(mode="json") for source in sources]},
            {**summary, "source_count": len(sources), "citation_keys": [source.key for source in sources]},
        )

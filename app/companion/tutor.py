from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.agent.models import AgentRun
from app.agent.schemas import RunStatus, StrictInput
from app.auth import SESSION_COOKIE, csrf_value
from app.companion.schemas import PageContext
from app.models import TicketEvent

RECENT = timedelta(minutes=10)
SEARCH_WINDOW = timedelta(minutes=5)
IDLE_SECONDS = 90
OFFER_SECONDS = 10
OFFER_RECEIPT_GRACE = 5
MAX_HINTS_PER_MINUTE = 6
RETENTION = timedelta(hours=24)
REJECTION_COOLDOWN = timedelta(minutes=15)
FREQUENCIES = {"low": (7, 420), "normal": (5, 180), "high": (3, 90)}
WEIGHTS = {"empty_sop": 3, "repeated_analysis": 2, "no_progress": 1, "sop_not_observed": 2}
PROGRESS = {"updated", "transitioned", "virtual_action_performed", "work_created", "work_updated", "learning_evidence"}


class TutorEvent(StrictInput):
    kind: Literal["TICKET_VIEWED", "SOP_SEARCH_COMPLETED", "SOP_OPENED", "ANALYSIS_COMPLETED", "ANALYSIS_FAILED",
                  "WORKFLOW_STEP_COMPLETED", "WORKFLOW_CANCELLED", "TICKET_PROGRESS"]
    id: UUID
    at: datetime
    receipt: Annotated[str, StringConstraints(max_length=260)] | None = None


class OfferInput(StrictInput):
    page_context: PageContext
    tab_id: UUID
    frequency: Literal["low", "normal", "high"]
    active_seconds: int = Field(ge=0, le=600, strict=True)
    events: list[TutorEvent] = Field(max_length=40)


class HintInput(StrictInput):
    page_context: PageContext
    tab_id: UUID
    request_id: UUID
    command: Literal["view", "more", "retry"]


class FeedbackInput(StrictInput):
    tab_id: UUID
    event: Literal["EXPIRED", "DISMISSED", "INVALIDATED"]


def login_scope(request):
    return csrf_value("companion-tutor:" + request.cookies[SESSION_COOKIE])


def search_receipt(user_id, run_id, ticket_id, tab_id, login_scope, count, now):
    payload = f"{uuid4()}.{int(now.timestamp())}.{count}"
    scope = f"tutor-search:{user_id}:{run_id}:{ticket_id}:{tab_id}:{login_scope}:{payload}"
    return payload + "." + csrf_value(scope)


def read_search_receipt(value, user_id, context, tab_id, login_scope, now):
    try:
        raw_id, raw_timestamp, raw_count, signature = value.split(".")
        payload = f"{raw_id}.{raw_timestamp}.{raw_count}"
        scope = f"tutor-search:{user_id}:{context.run_id}:{context.ticket_id}:{tab_id}:{login_scope}:{payload}"
        if not compare_digest(csrf_value(scope), signature):
            return None
        at = datetime.fromtimestamp(int(raw_timestamp), timezone.utc)
        if now - SEARCH_WINDOW <= at <= now + timedelta(seconds=1):
            return {"kind": "SOP_SEARCH_COMPLETED", "id": str(UUID(raw_id)), "at": at.isoformat(), "count": int(raw_count)}
    except (AttributeError, ValueError, OverflowError, OSError):
        pass
    return None


def verified_events(db, user_id, context, tab_id, login_scope, inputs, now):
    from app.knowledge.routes import sop_detail
    from app.http import ApiError
    from app.simulation.resources import evidence_allowed

    events = {}
    run_ids = {item.id for item in inputs if item.kind.startswith(("ANALYSIS_", "WORKFLOW_"))}
    runs = {row.id: row for row in db.scalars(select(AgentRun).where(AgentRun.id.in_(run_ids),
        AgentRun.requested_by == user_id, AgentRun.simulation_run_id == context.run_id,
        AgentRun.ticket_id == context.ticket_id, AgentRun.created_at >= now - RECENT))} if run_ids else {}
    approved_sops = set()
    for item in inputs:
        if item.kind == "SOP_SEARCH_COMPLETED":
            event = read_search_receipt(item.receipt, user_id, context, tab_id, login_scope, now)
            if event:
                events[(event["kind"], event["id"])] = event
            continue
        if not item.at.tzinfo or not (now - RECENT <= item.at <= now + timedelta(seconds=30)):
            continue
        at = min(item.at, now)
        detail = {}
        if item.kind.startswith(("ANALYSIS_", "WORKFLOW_")):
            work = runs.get(item.id)
            if not work:
                continue
            if item.kind.startswith("ANALYSIS_"):
                expected = RunStatus.SUCCEEDED if item.kind == "ANALYSIS_COMPLETED" else RunStatus.FAILED
                if work.companion or work.status != expected or not work.finished_at:
                    continue
                at = work.finished_at
                if expected == RunStatus.SUCCEEDED:
                    detail["related_sop_ids"] = list(dict.fromkeys(
                        entry["source_id"] for entry in (work.result or {}).get("evidence", [])
                        if entry.get("kind") == "SOP" and evidence_allowed(db, context.run_id, None, entry)
                    ))[:5]
            else:
                flow = (work.result or {}).get("workflow") or {}
                if item.kind == "WORKFLOW_CANCELLED" and flow.get("status") != "CANCELLED":
                    continue
                if item.kind == "WORKFLOW_STEP_COMPLETED":
                    completed = [datetime.fromisoformat(step["finished_at"]) for step in flow.get("steps", [])
                                 if step["status"] == "COMPLETED" and step.get("finished_at")]
                    if not completed:
                        continue
                    at = max(completed)
        elif item.kind == "SOP_OPENED" and item.id not in approved_sops:
            try:
                sop_detail(db, item.id, context.run_id)
            except ApiError:
                continue
            approved_sops.add(item.id)
        elif item.kind == "TICKET_PROGRESS":
            continue  # Use the authoritative ticket event below.
        if now - RECENT <= at <= now:
            events[(item.kind, str(item.id))] = {"kind": item.kind, "id": str(item.id), "at": at.isoformat(), **detail}
    progress = db.scalar(select(TicketEvent).where(
        TicketEvent.simulation_run_id == context.run_id, TicketEvent.ticket_id == context.ticket_id,
        TicketEvent.actor_id == user_id, TicketEvent.event_type.in_(PROGRESS), TicketEvent.created_at >= now - RECENT,
    ).order_by(TicketEvent.created_at.desc()).limit(1))
    if progress:
        events[("TICKET_PROGRESS", str(progress.id))] = {"kind": "TICKET_PROGRESS", "id": str(progress.id), "at": progress.created_at.isoformat()}
    return sorted(events.values(), key=lambda event: event["at"])[-40:]


def intervention(events, active_seconds, now):
    recent = [(event, datetime.fromisoformat(event["at"])) for event in events]
    recent = [event for event, at in sorted(recent, key=lambda entry: entry[1]) if now - RECENT <= at <= now]
    searches = [event for event in recent if event["kind"] == "SOP_SEARCH_COMPLETED"
                and datetime.fromisoformat(event["at"]) >= now - SEARCH_WINDOW]
    analysis = [event for event in recent if event["kind"] in {"ANALYSIS_COMPLETED", "ANALYSIS_FAILED"}]
    ref = next((event for event in reversed(analysis) if event["kind"] == "ANALYSIS_COMPLETED"
                and event.get("related_sop_ids")), None)
    opened = {event["id"] for event in recent if event["kind"] == "SOP_OPENED"}
    progress = any(event["kind"] in {"TICKET_PROGRESS", "WORKFLOW_STEP_COMPLETED"}
                   and now - datetime.fromisoformat(event["at"]) < timedelta(seconds=IDLE_SECONDS) for event in recent)
    evidence = {}
    if len(searches) >= 2 and all(event["count"] == 0 for event in searches[-2:]):
        evidence["empty_sop"] = [event["id"] for event in searches[-2:]]
    if len(analysis) >= 2:
        evidence["repeated_analysis"] = [event["id"] for event in analysis[-2:]]
    if ref and opened.isdisjoint(ref["related_sop_ids"]):
        evidence["sop_not_observed"] = [ref["id"]]
    if active_seconds >= IDLE_SECONDS and not progress:
        evidence["no_progress"] = []
    score = sum(WEIGHTS[rule] for rule in evidence)
    # Elapsed time alone must not make a previously offered issue new.
    fingerprint = sha256(repr([(rule, ids) for rule, ids in evidence.items() if ids]).encode()).hexdigest()
    return score, evidence, fingerprint

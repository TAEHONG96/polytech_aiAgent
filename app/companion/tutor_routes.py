import re
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.models import AgentRun
from app.agent.queue import ACTIVE_RUNS, utcnow
from app.agent.schemas import RunStatus
from app.auth import CurrentUser, WriteUser
from app.companion.actions import UNSUPPORTED, action_scope, proposal_view
from app.companion.routes import lock_chat, session_binding, turn_view
from app.companion.service import require_trainee, scoped_context, scoped_result
from app.companion.tutor import (
    FREQUENCIES, MAX_HINTS_PER_MINUTE, OFFER_RECEIPT_GRACE, OFFER_SECONDS, RECENT, REJECTION_COOLDOWN,
    FeedbackInput, HintInput, OfferInput,
    intervention, login_scope, verified_events,
)
from app.companion.tutor_models import TutorOffer
from app.companion.workflows import active_workflow
from app.db import Database
from app.http import ApiError

router = APIRouter(prefix="/companion/tutor", tags=["companion tutor"])
TUTOR_MORE = re.compile(r"조금\s*더|더\s*알려|어디에서\s*확인|어디서\s*확인|자세하게\s*설명|추가\s*힌트")
TUTOR_LOOKUP = re.compile(r"(?:찾아|검색해|비교해|추천해)\s*(?:줘|주세요|주고|봐\s*줘)|(?:찾아|검색하고)\s*(?:비교|열어)")
TUTOR_CONSENT = re.compile(r"^\s*(?:응|네|그래|좋아|예)[\s,.!]*")


def find_offer(db, user_id, offer_id, tab_id, request, now, *, lock=False):
    stmt = select(TutorOffer).where(TutorOffer.id == offer_id, TutorOffer.user_id == user_id,
                                  TutorOffer.login_scope == login_scope(request), TutorOffer.tab_id == tab_id,
                                  TutorOffer.created_at >= now - RECENT)
    offer = db.scalar(stmt.with_for_update() if lock else stmt)
    if not offer:
        raise ApiError(404, "TUTOR_OFFER_NOT_FOUND", "현재 로그인과 실습에서 제공된 힌트를 찾을 수 없습니다.")
    return offer


def check_context(db, user_id, context, offer=None):
    if context.page_id != "ticket" or not context.ticket_id or not context.run_id:
        raise ApiError(409, "TUTOR_CONTEXT_CHANGED", "실습 티켓 상세 화면에서 힌트를 요청해 주세요.")
    if offer and offer.context != action_scope(context):
        raise ApiError(409, "TUTOR_CONTEXT_CHANGED", "힌트를 받은 티켓 화면으로 돌아가 주세요.")
    scoped_context(db, user_id, context)


def busy_work(db, user_id, request, now):
    if active_workflow(db, user_id, now) or db.scalar(select(AgentRun.id).where(
            AgentRun.requested_by == user_id, AgentRun.status.in_(ACTIVE_RUNS)).limit(1)):
        return True
    rows = db.scalars(select(AgentRun).where(AgentRun.requested_by == user_id,
        AgentRun.created_at >= now - RECENT, AgentRun.companion["action_session"].astext == session_binding(request),
        AgentRun.result["action_proposal"]["status"].astext == "pending").limit(30))
    return any(proposal_view(row, row.result, now)["status"] == "pending" for row in rows)


def log_event(offer, event, now, **extra):
    offer.data = {**offer.data, "history": [*offer.data["history"], {"event": event, "at": now.isoformat(), **extra}][-20:]}


@router.post("/offers")
def create_offer(body: OfferInput, db: Database, user: WriteUser, request: Request, response: Response) -> dict | None:
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    context = body.page_context
    check_context(db, user.id, context)
    lock_chat(db, user.id)
    now = utcnow()
    if busy_work(db, user.id, request, now):
        return None
    scope = login_scope(request)
    events = verified_events(db, user.id, context, body.tab_id, scope, body.events, now)
    score, rules, fingerprint = intervention(events, body.active_seconds, now)
    threshold, cooldown = FREQUENCIES[body.frequency]
    if score < threshold:
        return None
    reports = list(db.scalars(select(TutorOffer).where(TutorOffer.user_id == user.id,
        TutorOffer.login_scope == scope, TutorOffer.tab_id == body.tab_id, TutorOffer.run_id == context.run_id,
        TutorOffer.created_at >= now - REJECTION_COOLDOWN).order_by(TutorOffer.created_at.desc()).limit(25)))
    if sum(row.data.get("status") == "DISMISSED" for row in reports) >= 2:
        return None
    current_scope = action_scope(context)
    retired = [row for row in reports if row.data["status"] == "OFFERED" and row.context != current_scope]
    for row in retired:
        log_event(row, "INVALIDATED", now)
        row.data = {**row.data, "status": "INVALIDATED"}
    if any(row.data["status"] == "OFFERED" and now - row.created_at <= timedelta(seconds=OFFER_SECONDS + OFFER_RECEIPT_GRACE)
           or row.ticket_id == context.ticket_id and (
               row.data["status"] != "INVALIDATED" and now - row.created_at < timedelta(seconds=cooldown)
               or (row.data["status"] != "INVALIDATED" or row.context != current_scope)
               and now - row.created_at < RECENT and row.data["fingerprint"] == fingerprint) for row in reports):
        if retired:
            db.commit()
        return None
    offer = TutorOffer(user_id=user.id, login_scope=scope, tab_id=body.tab_id, run_id=context.run_id,
        ticket_id=context.ticket_id, context=current_scope, created_at=now,
        data={"status": "OFFERED", "rules": rules, "score": score, "events": events, "fingerprint": fingerprint,
              "history": [], "generations": []})
    log_event(offer, "OFFERED", now)
    db.add(offer)
    db.commit()
    return {"id": offer.id, "rules": list(rules), "score": score, "threshold": threshold}


def queue_hint(db, offer, context, request_id, command, now, *, conversation_id=None, message="키로 맞춤 힌트"):
    requests = offer.data["generations"]
    previous = next((item for item in requests if item["request_id"] == str(request_id)), None)
    if previous:
        return db.get(AgentRun, UUID(previous["run_id"]))
    recent = db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.requested_by == offer.user_id,
        AgentRun.created_at >= now - timedelta(minutes=1), AgentRun.companion["tutor_hint"].astext.is_not(None)))
    if recent >= MAX_HINTS_PER_MINUTE:
        raise ApiError(429, "TUTOR_RATE_LIMIT", "맞춤 힌트 요청이 많습니다. 잠시 뒤 다시 시도해 주세요.")
    last = requests[-1] if requests else None
    prior = db.get(AgentRun, UUID(last["run_id"])) if last else None
    status = offer.data["status"]
    if prior and prior.status in ACTIVE_RUNS:
        raise ApiError(409, "TUTOR_BUSY", "현재 힌트를 생성 중입니다.")
    if command == "view":
        if status != "OFFERED" or prior or now - offer.created_at >= timedelta(seconds=OFFER_SECONDS + OFFER_RECEIPT_GRACE):
            raise ApiError(409, "TUTOR_OFFER_EXPIRED", "힌트 제안 시간이 지났습니다.")
        level = 1
        event = "VIEWED"
    else:
        if status != "VIEWED" or not prior:
            raise ApiError(409, "TUTOR_HINT_UNAVAILABLE", "먼저 제공된 힌트를 확인해 주세요.")
        level = last["level"]
        if command == "more":
            if prior.status != RunStatus.SUCCEEDED or level >= 3:
                raise ApiError(409, "TUTOR_LEVEL_LIMIT", "더 자세한 내용은 키로 채팅에서 질문해 주세요.")
            level += 1
            event = "MORE_HELP_REQUESTED"
        else:
            if prior.status != RunStatus.FAILED or not prior.retryable or sum(item["level"] == level for item in requests) >= 2:
                raise ApiError(409, "TUTOR_RETRY_LIMIT", "이 힌트를 다시 요청할 수 없습니다.")
            event = "RETRY_REQUESTED"
    run = AgentRun(requested_by=offer.user_id, simulation_run_id=offer.run_id, ticket_id=offer.ticket_id,
                   status=RunStatus.QUEUED, message=message, created_at=now,
                   companion={"conversation_id": str(conversation_id or offer.id), "request_id": str(request_id),
                              "page_context": context.model_dump(mode="json"),
                              "tutor_hint": {"id": str(offer.id), "level": level, "tab_id": str(offer.tab_id)}})
    db.add(run)
    try:
        db.flush()
        log_event(offer, event, now, level=level)
        offer.data = {**offer.data, "status": "VIEWED", "generations": [*requests,
                       {"request_id": str(request_id), "run_id": str(run.id), "level": level}]}
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) != "uq_agent_runs_active_user":
            raise
        raise ApiError(409, "AGENT_BUSY", "이전 질문이나 분석이 처리 중입니다. 잠시 뒤 다시 시도하세요.") from exc
    return run


def hint_view(db, run, offer):
    turn = turn_view(db, run)
    level = run.companion["tutor_hint"]["level"]
    usage = run.usage or {}
    return {"id": run.id, "level": level, "status": turn["status"], "answer": turn["answer"], "sources": turn["sources"],
            "execution_mode": turn["execution_mode"], "error": turn["error"], "model": turn["model"], "retrieval": turn["retrieval"],
            "search_mode": turn["search_mode"],
            "can_retry": bool(turn["error"] and turn["error"]["retryable"] and
                              sum(item["level"] == level for item in offer.data["generations"]) < 2),
            "model_calls": usage.get("model_requests", 0), "embedding_calls": usage.get("embedding_requests", 0),
            "elapsed_ms": round((run.finished_at - run.started_at).total_seconds() * 1000) if run.finished_at and run.started_at else None}


@router.post("/{offer_id}/hints", status_code=202)
def generate_hint(offer_id: UUID, body: HintInput, db: Database, user: WriteUser, request: Request, response: Response):
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    lock_chat(db, user.id)
    now = utcnow()
    offer = find_offer(db, user.id, offer_id, body.tab_id, request, now, lock=True)
    check_context(db, user.id, body.page_context, offer)
    if not any(item["request_id"] == str(body.request_id) for item in offer.data["generations"]) and busy_work(db, user.id, request, now):
        raise ApiError(409, "TUTOR_BUSY", "진행 중인 작업이 끝난 뒤 힌트를 요청해 주세요.")
    run = queue_hint(db, offer, body.page_context, body.request_id, body.command, now)
    return hint_view(db, run, offer)


@router.get("/{offer_id}/hints/{hint_id}")
def read_hint(offer_id: UUID, hint_id: UUID, tab_id: UUID, db: Database, user: CurrentUser, request: Request, response: Response):
    require_trainee(user)
    response.headers["Cache-Control"] = "no-store"
    offer = find_offer(db, user.id, offer_id, tab_id, request, utcnow())
    if not any(item["run_id"] == str(hint_id) for item in offer.data["generations"]):
        raise ApiError(404, "TUTOR_HINT_NOT_FOUND", "이 요청의 힌트를 찾을 수 없습니다.")
    return hint_view(db, db.get(AgentRun, hint_id), offer)


@router.post("/{offer_id}/feedback", status_code=204)
def feedback(offer_id: UUID, body: FeedbackInput, db: Database, user: WriteUser, request: Request):
    require_trainee(user)
    lock_chat(db, user.id)
    now = utcnow()
    offer = find_offer(db, user.id, offer_id, body.tab_id, request, now, lock=True)
    if offer.data["status"] == "OFFERED" or (offer.data["status"] == "VIEWED" and body.event == "DISMISSED"):
        log_event(offer, body.event, now)
        offer.data = {**offer.data, "status": body.event}
        db.commit()


def linked_offer(db, user_id, request, body, now):
    offer = find_offer(db, user_id, body.tutor_offer_id, body.tutor_tab_id, request, now, lock=True)
    check_context(db, user_id, body.page_context, offer)
    if offer.data["status"] != "VIEWED" or not offer.data["generations"]:
        raise ApiError(409, "TUTOR_HINT_UNAVAILABLE", "확인한 힌트만 채팅과 연결할 수 있습니다.")
    last = db.get(AgentRun, UUID(offer.data["generations"][-1]["run_id"]))
    if not last or last.status != RunStatus.SUCCEEDED:
        raise ApiError(409, "TUTOR_HINT_UNAVAILABLE", "확인한 힌트만 채팅과 연결할 수 있습니다.")
    scoped_result(db, user_id, body.page_context, last.result)
    return offer, last


def linked_workflow(message):
    if UNSUPPORTED.search(message) or not TUTOR_LOOKUP.search(message) or re.search(r"하지\s*마|열지\s*마|찾지\s*마", message):
        return None
    if not (TUTOR_CONSENT.search(message) or re.search(r"SOP|절차서", message, re.I)):
        return None
    return "sop_compare_open" if re.search(r"열어|열기|띄워", message) else "sop_compare"

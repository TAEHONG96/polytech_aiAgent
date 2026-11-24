import re
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.agent.models import AgentRun
from app.agent.schemas import Evidence, RunStatus, StrictInput
from app.companion.actions import UNSUPPORTED, action_scope, create_proposal
from app.companion.schemas import ActionDraft, PageContext
from app.http import ApiError

MAX_STEPS = 5
MAX_MODEL_CALLS = 4
MAX_STEP_ATTEMPTS = 2
MAX_PLAN_ATTEMPTS = 2
WORKFLOW_SECONDS = 600
STEP_SECONDS = 26
ACTIVE = ("PLANNING", "RUNNING", "WAITING_APPROVAL", "WAITING_USER_INPUT", "PAUSED")
Step = Literal["read_ticket", "search_sop", "analyze", "report", "verify_sops", "open_sop",
               "compare_sops", "explain_search", "navigate_ticket_list", "focus_ticket_search",
               "navigate_handover", "explain_handover", "highlight_handover"]
LABELS = {"read_ticket": "현재 티켓 확인", "search_sop": "관련 승인 SOP 검색", "analyze": "티켓과 SOP 근거 종합",
          "report": "다음 확인 항목 안내", "verify_sops": "문서 후보 검증", "open_sop": "승인 SOP 선택 후 문서 열기",
          "explain_search": "티켓 검색 방법 안내", "navigate_ticket_list": "티켓 목록으로 이동",
          "focus_ticket_search": "티켓 검색창 포커스", "compare_sops": "승인 SOP 관련성 비교와 설명",
          "navigate_handover": "발신 인계로 이동", "explain_handover": "발신 인계에서 확인할 항목 설명",
          "highlight_handover": "발신 인계 섹션 강조"}
UI_STEPS = {"open_sop", "navigate_ticket_list", "focus_ticket_search", "navigate_handover", "highlight_handover"}
NAVIGATION_STEPS = {"navigate_ticket_list": ("ticket_list", "tickets"), "navigate_handover": ("outgoing_handover", "outgoing")}
COMPARISON_WORKFLOWS = {"sop_compare", "sop_compare_open", "sop_compare_review", "sop_compare_review_open"}
GOALS = {
    "read_ticket": ("READ_CURRENT_TICKET", "ticket_id"), "search_sop": ("SEARCH_RELEVANT_SOPS", "source_ids"),
    "verify_sops": ("VERIFY_SOP_CANDIDATES", "source_ids"), "compare_sops": ("EXPLAIN_SOP_RELEVANCE", "explained"),
    "analyze": ("ANALYZE_TICKET", "analyzed"), "report": ("REPORT_NEXT_CHECKS", "explained"),
    "open_sop": ("OPEN_SELECTED_SOP", "sop_id"), "explain_search": ("EXPLAIN_TICKET_SEARCH", "explained"),
    "navigate_ticket_list": ("NAVIGATE_TICKET_LIST", "destination"), "focus_ticket_search": ("FOCUS_TICKET_SEARCH", "anchor_id"),
    "navigate_handover": ("NAVIGATE_HANDOVER", "destination"), "explain_handover": ("EXPLAIN_HANDOVER", "explained"),
    "highlight_handover": ("HIGHLIGHT_HANDOVER", "anchor_id"),
}


class WorkflowPlan(StrictInput):
    steps: Annotated[list[Step], Field(min_length=1, max_length=MAX_STEPS)]


class WorkflowControl(StrictInput):
    command: Literal["pause", "cancel", "resume"]
    view_id: UUID
    page_context: PageContext
    clue: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)] | None = None


def requested_workflow(message: str, context: PageContext) -> str | None:
    if UNSUPPORTED.search(message):
        return None
    related = re.search(r"SOP|절차서", message, re.I)
    if context.ticket_id and context.page_id == "ticket" and related:
        lookup = re.search(r"찾|검색|후보|추천|문서|절차서", message)
        compare = re.search(r"관련성|적합|우선\s*(?:할|순위|참고)|추천|비교|(?:가장|제일)\s*(?:맞|관련)|(?:맞는|관련된)\s*(?:이유|근거)", message)
        opening = re.search(r"열어|열기|열고|띄워", message)
        review = re.search(r"분석|진단|조사", message) and re.search(r"다음|해야 할|확인 항목", message)
        if lookup and compare:
            if review:
                return "sop_compare_review_open" if opening else "sop_compare_review"
            return "sop_compare_open" if opening else "sop_compare"
        if re.search(r"찾(?:아|고|은|아서)|검색(?:해|하고)", message) and opening:
            return "ticket_review_open" if review else "sop_open"
        if review:
            return "ticket_review"
    if context.run_id and re.search(r"인수\s*인계|발신\s*인계|인계\s*(?:페이지|화면|섹션)", message):
        on_handover = (context.page_id, context.section) == ("workspace", "outgoing")
        navigation = re.search(r"이동|열어|가서|가\s*줘", message)
        explanation = re.search(r"설명|무엇|어떤|확인\s*(?:할|해야)|알려|안내", message)
        highlight = re.search(r"강조|가리켜|(?:부분|영역|섹션|위치).{0,12}안내", message)
        if (navigation or on_handover and highlight) and (explanation or highlight):
            return "handover_highlight" if highlight else "handover_guide"
    if context.run_id and re.search(r"티켓\s*(?:을\s*)?검색", message) and re.search(
        r"검색창|검색 입력", message,
    ) and re.search(r"방법|알려주고|이동한\s*다음|이동해서|이동.*찾", message):
        return "ticket_search"
    return None


def plan_template(kind: str, context: PageContext) -> list[Step]:
    if kind in {"ticket_review", "ticket_review_open"}:
        review: list[Step] = ["read_ticket", "search_sop", "analyze", "report"]
        return review + ["open_sop"] if kind == "ticket_review_open" else review
    if kind == "sop_open":
        return ["read_ticket", "search_sop", "verify_sops", "open_sop"]
    if kind in COMPARISON_WORKFLOWS:
        comparison: list[Step] = ["read_ticket", "search_sop", "compare_sops"]
        if "review" in kind:
            comparison.append("analyze")
        return comparison + ["open_sop"] if kind.endswith("_open") else comparison
    if kind in {"handover_guide", "handover_highlight"}:
        handover: list[Step] = []
        if (context.page_id, context.section) != ("workspace", "outgoing"):
            handover.append("navigate_handover")
        handover.append("explain_handover")
        return handover + ["highlight_handover"] if kind == "handover_highlight" else handover
    steps: list[Step] = ["explain_search"]
    if (context.page_id, context.section) != ("workspace", "tickets"):
        steps.append("navigate_ticket_list")
    return steps + ["focus_ticket_search"]


def goal_requirements(steps: list[Step]) -> list[dict]:
    requirements = []
    for index, step in enumerate(steps, 1):
        if step == "open_sop":
            requirements.append({"id": "USER_SELECT_SOP", "step_id": index, "step_type": step})
        requirements.append({"id": GOALS[step][0], "step_id": index, "step_type": step})
        if step == "analyze" and "report" not in steps:
            requirements.append({"id": "REPORT_NEXT_CHECKS", "step_id": index, "step_type": step})
    return requirements


def initial_workflow(kind: str, context: PageContext, view_id: str, now: datetime) -> dict:
    return {"kind": kind, "status": "PLANNING", "context": action_scope(context), "view_id": view_id, "steps": [],
            "current_step_index": 0, "expires_at": (now + timedelta(seconds=WORKFLOW_SECONDS)).isoformat(),
            "queued_at": now.isoformat(), "model_calls": 0, "embedding_calls": 0, "plan_attempts": 0, "data": {},
            "goal_requirements": goal_requirements(plan_template(kind, context))}


def goal_progress(flow: dict) -> list[dict]:
    steps = {step["id"]: step for step in flow["steps"]}
    data = flow["data"]
    explanations = {"report": data.get("summary"), "compare_sops": (data.get("sop_comparison") or {}).get("candidates"),
                    "explain_search": data.get("explanation"), "explain_handover": data.get("handover_guide")}
    progress = []
    for goal in flow.get("goal_requirements", []):
        step = steps.get(goal["step_id"], {})
        name = step.get("type")
        output = step.get("result")
        verified = False
        if name in GOALS and isinstance(output, dict):
            if goal["id"] == "USER_SELECT_SOP":
                verified = bool(output.get("selected_sop_id"))
            elif name == "analyze" and goal["id"] == "REPORT_NEXT_CHECKS":
                verified = step["status"] == "COMPLETED" and bool(data.get("summary"))
            elif goal["id"] == GOALS[name][0] and step["status"] == "COMPLETED":
                verified = bool(output.get(GOALS[name][1]))
                if name in UI_STEPS:
                    verified = verified and bool(step.get("approved_proposal_id"))
                if name == "open_sop":
                    verified = verified and output.get("sop_id") == output.get("selected_sop_id")
                if name in explanations:
                    verified = verified and bool(explanations[name])
        progress.append({**goal, "status": "COMPLETED" if verified else "PENDING"})
    return progress


def finish_workflow(result: dict) -> None:
    flow = result["workflow"]
    goals = goal_progress(flow)
    complete = (bool(flow["steps"]) and all(step["status"] == "COMPLETED" for step in flow["steps"])
                and all(goal["status"] == "COMPLETED" for goal in goals))
    flow["status"] = "COMPLETED" if complete else "FAILED"
    if not complete:
        flow.update(error_code="WORKFLOW_GOAL_INCOMPLETE", retryable=False)
    record_outcome(result)


def record_outcome(result: dict) -> None:
    flow = result["workflow"]
    goals = goal_progress(flow)
    pending = {goal["step_id"] for goal in goals if goal["status"] != "COMPLETED"}
    flow["outcome"] = {
        "performed": [step["label"] for step in flow["steps"] if step["status"] == "COMPLETED" and step["id"] not in pending],
        "evidence": [source["title"] for source in result.get("sources", [])]
                    or (["현재 실습의 발신 인계 화면"] if flow["data"].get("handover_guide") else []),
        "incomplete": ([step["label"] for step in flow["steps"] if step["status"] != "COMPLETED" or step["id"] in pending]
                       if flow["steps"] else list(dict.fromkeys(LABELS[goal["step_type"]] for goal in goals))),
    }


def active_workflow(db, user_id, now: datetime, *, except_id=None):
    query = select(AgentRun.id).where(AgentRun.requested_by == user_id, AgentRun.status != RunStatus.FAILED,
                                    AgentRun.created_at > now - timedelta(seconds=WORKFLOW_SECONDS),
                                    AgentRun.result["workflow"]["status"].astext.in_(ACTIVE))
    if except_id:
        query = query.where(AgentRun.id != except_id)
    return db.scalar(query.limit(1))


def step_state(flow: dict, status: str, now: datetime, **fields):
    index = flow["current_step_index"]
    if index < len(flow["steps"]):
        step = flow["steps"][index]
        step.update(status=status, **fields)
        if status in {"COMPLETED", "FAILED", "SKIPPED"}:
            step["finished_at"] = now.isoformat()


def effective_state(run, flow: dict, proposal: dict | None, now: datetime) -> str:
    if flow["status"] in {*ACTIVE, "FAILED"} and now >= datetime.fromisoformat(flow["expires_at"]):
        return "EXPIRED"
    if flow["status"] in {"RUNNING", "PLANNING"} and run.status == RunStatus.FAILED:
        return "FAILED"
    if flow["status"] in {"WAITING_APPROVAL", "WAITING_USER_INPUT"} and proposal and proposal["status"] in {"pending", "approved"} and now >= datetime.fromisoformat(proposal["expires_at"]):
        return "PAUSED"
    return flow["status"]


def workflow_view(run, result: dict, now: datetime) -> dict | None:
    flow = result.get("workflow")
    if not flow:
        return None
    status = effective_state(run, flow, result.get("action_proposal"), now)
    view = {key: flow[key] for key in ("kind", "steps", "current_step_index", "expires_at", "model_calls", "embedding_calls")}
    step = flow["steps"][flow["current_step_index"]] if flow["current_step_index"] < len(flow["steps"]) else None
    remaining = step["attempts"] < MAX_STEP_ATTEMPTS if step else flow["plan_attempts"] < MAX_PLAN_ATTEMPTS
    input_kind = ("sop_selection" if (result.get("action_proposal") or {}).get("requires_selection")
                  else "clue") if status == "WAITING_USER_INPUT" else None
    if status == "PAUSED" and flow.get("clue_required"):
        input_kind = "clue"
    return {**view, "id": str(run.id), "goal": run.message, "status": status, "goal_requirements": goal_progress(flow),
            "outcome": flow.get("outcome"), "input_kind": input_kind,
            "context_fingerprint": sha256(repr(sorted(flow["context"].items())).encode()).hexdigest()[:16],
            "error_code": flow.get("error_code") or run.error_code,
            "can_resume": remaining and (status == "PAUSED" or input_kind == "clue"
                                        or status == "FAILED" and flow.get("retryable", run.retryable))}


def release_job(run, now):
    run.status = RunStatus.SUCCEEDED
    run.finished_at = now
    run.lease_token = None
    run.lease_expires_at = None


def pause_or_cancel(run, outcome: str, now: datetime, *, reason=None) -> None:
    result = deepcopy(run.result)
    flow = result["workflow"]
    if outcome == "CANCELLED":
        for step in flow["steps"][flow["current_step_index"]:]:
            step.update(status="SKIPPED", finished_at=now.isoformat())
    elif flow["steps"]:
        step_state(flow, "PENDING", now)
    if proposal := result.get("action_proposal"):
        if proposal["status"] in {"pending", "approved"}:
            result["action_proposal"] = {**proposal, "status": "stale" if outcome == "PAUSED" else "cancelled"}
    flow.update(status=outcome, error_code=reason)
    if outcome == "CANCELLED":
        record_outcome(result)
    run.result = result
    release_job(run, now)
    run.error_code = run.error_message = None
    run.retryable = False


def queue_resume(run, body: WorkflowControl, now: datetime, clue: str | None):
    result = deepcopy(run.result)
    flow = result["workflow"]
    status = effective_state(run, flow, result.get("action_proposal"), now)
    if status not in {"PAUSED", "WAITING_USER_INPUT"} and not (status == "FAILED" and flow.get("retryable", run.retryable)):
        raise ApiError(409, "WORKFLOW_NOT_RESUMABLE", "현재 상태에서는 워크플로를 재개할 수 없습니다.")
    if flow["context"] != action_scope(body.page_context):
        raise ApiError(409, "WORKFLOW_CONTEXT_CHANGED", "기존 작업의 실습과 화면으로 돌아간 뒤 재개해 주세요.")
    if status == "WAITING_USER_INPUT" and (result.get("action_proposal") or {}).get("requires_selection"):
        raise ApiError(409, "WORKFLOW_SELECTION_REQUIRED", "열 문서를 먼저 선택해 주세요.")
    if (status == "WAITING_USER_INPUT" or flow.get("clue_required")) and not clue:
        raise ApiError(422, "WORKFLOW_CLUE_REQUIRED", "오류 코드나 관측 증상을 입력해 주세요.")
    if flow["steps"]:
        step = flow["steps"][flow["current_step_index"]]
        if step["attempts"] >= MAX_STEP_ATTEMPTS:
            raise ApiError(409, "WORKFLOW_LIMIT", "이 단계의 재시도 횟수를 모두 사용했습니다.")
        step_state(flow, "PENDING", now, error_code=None, finished_at=None, result=None)
    elif flow["plan_attempts"] >= MAX_PLAN_ATTEMPTS:
        raise ApiError(409, "WORKFLOW_LIMIT", "계획 생성 시도 횟수를 모두 사용했습니다.")
    if clue:
        flow["data"]["clue"] = clue
    if status in {"FAILED", "WAITING_USER_INPUT"}:
        result["answer"] = (flow["data"].get("explanation") or flow["data"].get("handover_guide") or flow["data"].get("summary"))
    flow.update(status="RUNNING" if flow["steps"] else "PLANNING", view_id=str(body.view_id),
                queued_at=now.isoformat(), error_code=None, clue_required=False, outcome=None)
    run.result = {**result, "action_proposal": None}
    run.status = RunStatus.QUEUED
    run.error_code = run.error_message = None
    run.retryable = False


def propose_step(flow: dict, context: PageContext, now: datetime) -> dict:
    step = flow["steps"][flow["current_step_index"]]["type"]
    sources = [Evidence.model_validate(row) for row in flow["data"].get("evidence", [])]
    if step == "open_sop" and sources:
        draft = ActionDraft(type="open_sop", sop_key=sources[0].key)
    elif step in NAVIGATION_STEPS:
        draft = ActionDraft(type="navigate_to_page", route_id=NAVIGATION_STEPS[step][0])
    elif step == "focus_ticket_search" and (context.page_id, context.section) == ("workspace", "tickets"):
        draft = ActionDraft(type="focus_input", anchor_id="ticket-search")
    elif step == "highlight_handover" and (context.page_id, context.section) == ("workspace", "outgoing"):
        draft = ActionDraft(type="highlight_element", anchor_id="outgoing-handover")
    else:
        raise ApiError(409, "WORKFLOW_TARGET_INVALID", "현재 단계의 실행 대상을 확인할 수 없습니다.")
    proposal = create_proposal(draft, draft.model_dump(exclude_none=True), sources, context, flow["view_id"], now)
    if not proposal:
        raise ApiError(409, "WORKFLOW_TARGET_INVALID", "승인된 실행 대상을 확인할 수 없습니다.")
    step_state(flow, "WAITING_APPROVAL", now)
    if step == "open_sop" and flow["kind"] in COMPARISON_WORKFLOWS:
        proposal["requires_selection"] = True
        flow["status"] = "WAITING_USER_INPUT"
        step_state(flow, "WAITING_USER_INPUT", now)
    else:
        flow["status"] = "WAITING_APPROVAL"
    return proposal


def select_sop(run, proposal, selected, now):
    result = deepcopy(run.result)
    flow = result["workflow"]
    result["action_proposal"] = {**proposal, "chosen_sop_id": str(selected)}
    step_state(flow, "WAITING_APPROVAL", now, result={"selected_sop_id": str(selected)})
    flow["status"] = "WAITING_APPROVAL"
    run.result = result


def after_feedback(db, run, proposal, body, now):
    from app.companion.service import scoped_context

    result = deepcopy(run.result)
    flow = result["workflow"]
    if flow["status"] != "WAITING_APPROVAL":
        return
    step = flow["steps"][flow["current_step_index"]]
    name = step["type"]
    flow.setdefault("feedback", []).append([proposal["id"], proposal["view_id"], body.outcome, body.error_code])
    if now >= datetime.fromisoformat(flow["expires_at"]):
        step_state(flow, "FAILED", now, error_code="WORKFLOW_EXPIRED")
        flow["status"] = "EXPIRED"
        record_outcome(result)
        run.result = result
        return
    if body.outcome == "failed":
        step_state(flow, "FAILED", now, error_code=body.error_code)
        flow.update(status="PAUSED" if body.error_code == "context_changed" else "FAILED", error_code=body.error_code,
                    retryable=step["attempts"] < MAX_STEP_ATTEMPTS)
        record_outcome(result)
    else:
        destination = body.destination_context
        expected = {**proposal["context"]}
        output = {"anchor_id": proposal.get("anchor_id")}
        if name in NAVIGATION_STEPS or name == "open_sop":
            expected.update(page_id="sop" if name == "open_sop" else "workspace", ticket_id=None,
                            incident_id=None, handover_revision=None,
                            section=None if name == "open_sop" else NAVIGATION_STEPS[name][1],
                            sop_id=proposal.get("selected_sop_id") if name == "open_sop" else None)
            if (not destination or not body.destination_view_id or str(body.destination_view_id) == proposal["view_id"]
                    or action_scope(destination) != expected):
                raise ApiError(409, "WORKFLOW_DESTINATION_UNCONFIRMED", "승인한 대상 화면에서만 작업을 이어갈 수 있습니다.")
            scoped_context(db, run.requested_by, destination)
            flow.update(context=expected, view_id=str(body.destination_view_id))
            output = {"destination": expected, "destination_view_id": str(body.destination_view_id)}
            if name == "open_sop":
                output.update(sop_id=str(destination.sop_id),
                              selected_sop_id=(step.get("result") or {}).get("selected_sop_id", proposal["selected_sop_id"]))
        step_state(flow, "COMPLETED", now, result=output, approved_proposal_id=proposal["id"])
        flow["current_step_index"] += 1
        if flow["current_step_index"] == len(flow["steps"]):
            finish_workflow(result)
            if flow["status"] == "COMPLETED" and name == "open_sop":
                opened = next(option["title"] for option in proposal["options"] if option["sop_id"] == proposal["selected_sop_id"])
                summary = flow["data"].get("summary")
                result["answer"] = (summary + "\n\n" if summary else "") + f"선택한 승인 SOP **{opened}**를 열었어요."
            elif flow["status"] == "COMPLETED" and name == "focus_ticket_search":
                result["answer"] = flow["data"]["explanation"] + " 검색창에 포커스를 옮겼어요."
        else:
            next_step = flow["steps"][flow["current_step_index"]]
            if next_step["type"] in UI_STEPS:
                step_state(flow, "RUNNING", now, started_at=now.isoformat(), attempts=1)
                result["action_proposal"] = propose_step(flow, PageContext.model_validate(flow["context"]), now)
            else:
                result["action_proposal"] = None
                busy = db.scalar(select(AgentRun.id).where(AgentRun.requested_by == run.requested_by, AgentRun.id != run.id,
                                 AgentRun.status.in_((RunStatus.QUEUED, RunStatus.RUNNING))).limit(1))
                if busy:
                    flow.update(status="PAUSED", error_code="WORKFLOW_QUEUE_BUSY")
                else:
                    flow.update(status="RUNNING", queued_at=now.isoformat(), error_code=None)
                    run.status = RunStatus.QUEUED
                    run.finished_at = None
    run.result = result

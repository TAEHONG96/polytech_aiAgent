import logging
import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.agent.schemas import Evidence
from app.companion.schemas import ActionDraft, PageContext
from app.http import ApiError
from app.knowledge.routes import sop_detail

ROUTES = {
    "practice_home": "내 실습", "workspace": "근무 워크스페이스", "ticket_list": "실습 티켓 목록",
    "outgoing_handover": "발신 인계", "sop_library": "승인 SOP 목록",
}
ANCHORS = {
    "search-sop": ("SOP 검색 버튼", {"workspace", "ticket"}),
    "sop-query": ("SOP 검색어 입력", {"workspace", "ticket"}),
    "ticket-search": ("티켓 검색창", {"workspace"}),
}
SECTIONS = {
    "tickets": "실습 티켓 표에서 티켓을 열거나 검색어와 상태로 목록을 좁힐 수 있어요.",
    "knowledge": "SOP·유사 장애 영역에서 승인 문서와 과거 사례를 찾고 출처를 열어 볼 수 있어요.",
    "outgoing": "발신 인계 영역은 다음 교대에 전달할 미해결 티켓, 관측 내용, 다음 조치를 정리하는 곳이에요.",
    "work": "작업 이력 영역은 이 티켓에 연결된 후속 작업과 지금까지 기록된 진행 상황을 확인하는 곳이에요.",
}
NAVIGATION = re.compile(r"이동|열어|가\s*줘|보내\s*줘|열기")
FIND_UI = re.compile(r"어디|위치|강조|가리켜|찾아\s*줘")
FOCUS = re.compile(r"이동|포커스|커서|옮겨|선택해")
EXPLAIN = re.compile(r"역할|무슨\s*기능|설명|무엇을\s*하는")
OPEN_SOP = re.compile(r"열어|띄워|보여\s*줘|열기")
UNSUPPORTED = re.compile(
    r"https?://|(?:javascript|data|file)\s*:|(?:CSS|DOM)\s*(?:선택자|selector|조작)|\beval\s*\("
    r"|(?:SSH|셸|터미널|SQL|스크립트)\s*(?:명령|실행|접속)|(?:지시|규칙|시스템\s*프롬프트).{0,10}무시"
    r"|(?:티켓|상태|작업\s*이력|인계|SOP).{0,15}(?:삭제|수정해|변경해|종료해|저장해|작성해|제출해)"
    r"|(?:폼|양식).{0,10}(?:자동\s*입력|제출|전송)|(?:실제\s*)?(?:장비|BMC).{0,12}(?:제어해|재부팅해)", re.I,
)
ACTION_LOG = logging.getLogger("uvicorn.error.companion.action")
INVALID_TEXT = "이 화면에서 실행할 수 있는 동작이 없어요. 해당 실습 화면을 열고 원하는 위치나 문서를 알려 주세요."
CONTEXT_FIELDS = {"page_id", "section", "run_id", "ticket_id", "sop_id", "incident_id", "handover_revision"}


def action_scope(context: PageContext) -> dict:
    return context.model_dump(mode="json", include=CONTEXT_FIELDS)


def plan_request(message: str, context: PageContext) -> dict | None:
    if UNSUPPORTED.search(message):
        return {"type": "unsupported"}
    if EXPLAIN.search(message):
        if context.page_id == "ticket" and re.search(r"작업\s*(?:이력|입력|영역)|교대\s*업무", message):
            return {"type": "explain_section", "section_id": "work"}
        if context.page_id == "workspace" and re.search(r"교대\s*업무|인계", message):
            return {"type": "explain_section", "section_id": "outgoing"}
        if context.page_id in {"workspace", "ticket"} and re.search(r"(?:SOP|유사 장애)\s*검색\s*(?:영역|기능)", message, re.I):
            return {"type": "explain_section", "section_id": "knowledge"}
        if context.page_id == "workspace" and re.search(r"티켓\s*(?:목록|표|영역)", message):
            return {"type": "explain_section", "section_id": "tickets"}
    anchor = None
    if re.search(r"티켓\s*검색\s*(?:창|어|입력)", message):
        anchor = "ticket-search"
    elif re.search(r"SOP\s*검색\s*(?:창|어|입력)", message, re.I):
        anchor = "sop-query"
    elif re.search(r"SOP\s*검색\s*(?:버튼|위치)", message, re.I):
        anchor = "search-sop"
    if anchor and (FOCUS.search(message) or FIND_UI.search(message)):
        if context.page_id not in ANCHORS[anchor][1]:
            return {"type": "unavailable"}
        kind = "focus_input" if anchor != "search-sop" and FOCUS.search(message) else "highlight_element"
        return {"type": kind, "anchor_id": anchor}

    if NAVIGATION.search(message):
        route = next((route_id for pattern, route_id in (
            (r"(?:인수\s*인계|발신\s*인계|인계\s*페이지)", "outgoing_handover"),
            (r"티켓\s*목록", "ticket_list"), (r"SOP\s*목록|문서\s*목록", "sop_library"),
            (r"워크스페이스", "workspace"), (r"내\s*실습|실습\s*홈", "practice_home"),
        ) if re.search(pattern, message, re.I)), None)
        if route:
            return {"type": "navigate_to_page", "route_id": route} if context.run_id or route == "practice_home" else {"type": "unavailable"}
    if re.search(r"SOP|절차\s*(?:서|문서)", message, re.I) and OPEN_SOP.search(message):
        return {"type": "open_sop"} if context.run_id else {"type": "unavailable"}
    if re.search(r"(?:페이지|화면)\s*(?:로|으로)?\s*(?:이동|열어)", message):
        return {"type": "unavailable"}
    return None


def create_proposal(draft: ActionDraft | None, plan: dict | None, evidence: list[Evidence],
                    context: PageContext, view_id: str | None, now: datetime) -> dict | None:
    if not (draft and plan and view_id) or draft.type != plan["type"]:
        return None
    proposal = {"id": str(uuid4()), "type": draft.type, "view_id": view_id, "status": "pending",
                "context": action_scope(context), "expires_at": (now + timedelta(minutes=2)).isoformat()}
    if draft.type == "open_sop":
        options = {str(item.source_id): {"sop_id": str(item.source_id), "title": item.title}
                   for item in evidence if item.kind == "SOP"}
        if not context.run_id or not any(item.kind == "SOP" and item.key == draft.sop_key for item in evidence):
            return None
        return {**proposal, "label": "승인 SOP 문서 열기", "options": list(options.values())}
    if draft.type == "navigate_to_page" and draft.route_id == plan.get("route_id"):
        return {**proposal, "label": ROUTES[draft.route_id], "route_id": draft.route_id}
    if draft.type in {"highlight_element", "focus_input"} and draft.anchor_id == plan.get("anchor_id"):
        return {**proposal, "label": ANCHORS[draft.anchor_id][0], "anchor_id": draft.anchor_id}
    return None


def proposal_view(run, result: dict, now: datetime) -> dict | None:
    stored = result.get("action_proposal")
    if not stored:
        return None
    proposal = {key: value for key, value in stored.items() if key not in {"decided_at", "completed_at", "selected_sop_id"}}
    if proposal["status"] == "pending":
        if run.companion.get("action_view_closed"):
            proposal["status"] = "stale"
        elif now >= datetime.fromisoformat(proposal["expires_at"]):
            proposal["status"] = "expired"
    return proposal


def execution_command(db: Session, proposal: dict, context: PageContext, selected: UUID | None) -> dict:
    kind = proposal["type"]
    if kind == "open_sop" and context.run_id and selected and any(
        item["sop_id"] == str(selected) for item in proposal["options"]
    ):
        sop_detail(db, selected, context.run_id)
        return {"type": kind, "run_id": context.run_id, "sop_id": selected}
    if kind == "navigate_to_page" and selected is None and proposal.get("route_id") in ROUTES:
        if context.run_id or proposal["route_id"] == "practice_home":
            return {"type": kind, "run_id": context.run_id, "route_id": proposal["route_id"]}
    if kind in {"highlight_element", "focus_input"} and selected is None and proposal.get("anchor_id") in ANCHORS:
        anchor = proposal["anchor_id"]
        if context.page_id in ANCHORS[anchor][1] and (kind != "focus_input" or anchor != "search-sop"):
            return {"type": kind, "anchor_id": anchor}
    raise ApiError(409, "ACTION_TARGET_INVALID", "제안 범위에서 실행할 대상을 다시 선택해 주세요.")


def log_action(run, proposal: dict, status: str, *, target=None, error=None) -> None:
    context = proposal["context"]
    ACTION_LOG.info("companion_action proposal=%s user=%s type=%s target=%s page=%s run=%s ticket=%s status=%s code=%s",
                    proposal["id"], run.requested_by, proposal["type"],
                    target or proposal.get("route_id") or proposal.get("anchor_id"),
                    context["page_id"], context["run_id"], context["ticket_id"], status, error)

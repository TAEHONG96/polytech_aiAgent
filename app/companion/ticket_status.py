import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import Evidence, clean_text
from app.companion.schemas import PageContext
from app.models import TicketEvent
from app.tickets import TRANSITIONS, TicketStatus, ticket_by_id

STATUS_LABELS = {
    TicketStatus.OPEN: "접수",
    TicketStatus.IN_PROGRESS: "처리 중",
    TicketStatus.WAITING: "대기",
    TicketStatus.RESOLVED: "해결",
    TicketStatus.CLOSED: "종료",
}
STATUS_MEANINGS = {
    TicketStatus.OPEN: "티켓이 등록되어 처리를 시작할 수 있는 상태입니다.",
    TicketStatus.IN_PROGRESS: "원인을 조사하거나 조치를 진행하고 있는 상태입니다.",
    TicketStatus.WAITING: "추가 확인, 회신 또는 재점검을 기다리는 상태입니다.",
    TicketStatus.RESOLVED: "해결 요약을 기록한 상태입니다. 후속 작업이 남아 있을 수 있습니다.",
    TicketStatus.CLOSED: "미완료 후속 작업을 모두 정리하고 티켓을 마감한 상태입니다.",
}

STATUS_NAMES = {
    TicketStatus.OPEN: r"접수|\bopen\b",
    TicketStatus.IN_PROGRESS: r"(?:처리|진행)\s*중|\bin[ _-]progress\b",
    TicketStatus.WAITING: r"대기|\bwaiting\b",
    TicketStatus.RESOLVED: r"해결|\bresolved\b",
    TicketStatus.CLOSED: r"종료|\bclosed\b",
}
COMPARISON = re.compile(r"차이|비교|구분|달라|다른\s*점")
EXPLANATION = re.compile(r"뜻|의미|정의|종류|조건|규칙|무슨|뭐|어떤|알려|설명|언제|어떻게|가능|수\s*있")
TRANSITION = re.compile(r"전(?:이|환)|바꿀|바꾸는\s*법|변경|넘어갈")
REASON = re.compile(r"왜|어쩌다|이유|사유")
CURRENT = re.compile(r"이\s*(?:티켓|건)|이거|이\s*장애|현재|지금|됐|되었|바뀌|변경됐")
CURRENT_STATUS = re.compile(r"(?:현재|지금)\s*(?:이\s*티켓(?:의)?\s*)?상태(?:가|는|를)?\s*(?:뭐|무엇|어떤|알려|확인)")
DIAGNOSIS = re.compile(
    r"(?<![A-Z])SOP(?![A-Z])|(?:장애|오류)(?:의)?\s*원인|점검|진단|(?:복구|확인)\s*절차", re.IGNORECASE,
)
DEVICE = re.compile(r"서버|장비|센서|네트워크|메모리|스토리지")
TICKET_NUMBER = re.compile(r"(?<![A-Z0-9])[A-Z]{2,10}-\d{1,12}(?!\d)", re.IGNORECASE)
STATUS_ENDING = (r"\s*(?:상태|이?랑|와|과|에서|인\s*(?:이유|건지)|이?야|인가|중|됐|되었"
                 r"|(?:으)?로\s*(?:바|변|전|넘)|는\s*(?:뭐|무슨|어떤|왜|언제|어떻게)|[?!]|$)")


def status_question(message: str, context: PageContext) -> tuple[str, tuple[TicketStatus, ...]] | None:
    names = tuple(status for status, pattern in STATUS_NAMES.items() if re.search(pattern, message, re.IGNORECASE))
    has_ticket = "티켓" in message or TICKET_NUMBER.search(message)
    ticket_view = context.page_id == "ticket" or context.page_id == "workspace" and context.section in (None, "tickets")
    status_topic = "상태" in message or any(
        re.search(f"(?:{STATUS_NAMES[status]}){STATUS_ENDING}", message, re.IGNORECASE) for status in names
    )
    if not status_topic or DIAGNOSIS.search(message) or (not has_ticket and (not ticket_view or DEVICE.search(message))):
        return None

    asks_reason = REASON.search(message) and (names or CURRENT.search(message))
    if (asks_reason or CURRENT_STATUS.search(message)) and not COMPARISON.search(message):
        return "history", names
    if COMPARISON.search(message) or EXPLANATION.search(message):
        return ("transitions" if TRANSITION.search(message) else "meaning"), names
    return None


def explain_status(kind: str, names: tuple[TicketStatus, ...]) -> str:
    if kind == "transitions":
        def transition_line(status: TicketStatus) -> str:
            targets = ", ".join(STATUS_LABELS[target] for target in TRANSITIONS[status])
            return f"- **{STATUS_LABELS[status]}**에서 변경할 수 있는 상태는 {targets}입니다."

        lines = [transition_line(status) for status in names or tuple(TicketStatus)]
        return "\n".join(lines) + "\n\n변경 사유가 필요합니다. 해결로 바꿀 때는 해결 요약을 입력하고, 종료하려면 미완료 후속 작업을 먼저 정리하세요."

    return "\n".join(f"- **{STATUS_LABELS[status]}** — {STATUS_MEANINGS[status]}" for status in names or tuple(TicketStatus))


def explain_current_status(db: Session, context: PageContext, message: str) -> tuple[str, Evidence | None]:
    if not context.ticket_id:
        return "상태가 바뀐 이유를 확인할 티켓을 목록에서 열고 다시 질문해 주세요.", None

    ticket = ticket_by_id(db, context.ticket_id, run_id=context.run_id)
    numbers = TICKET_NUMBER.findall(message)
    if any(number.casefold() != ticket.display_number.casefold() for number in numbers):
        return "질문에 적힌 번호의 티켓을 열면 해당 티켓의 상태 변경 기록을 확인할 수 있습니다.", None

    label = STATUS_LABELS[TicketStatus(ticket.status)]
    event = db.scalar(select(TicketEvent).where(
        TicketEvent.ticket_id == ticket.id, TicketEvent.simulation_run_id == context.run_id,
        TicketEvent.event_type == "transitioned", TicketEvent.ticket_version <= ticket.version,
        TicketEvent.details["changes"]["status"]["after"].astext == ticket.status,
    ).order_by(TicketEvent.ticket_version.desc()).limit(1))
    answer = f"{ticket.display_number}의 현재 상태는 **{label}**입니다."
    reason = event.details.get("reason") if event else None
    if reason:
        answer += f"\n\n가장 최근 **{label}** 변경에 기록된 사유는 다음과 같습니다.\n\n{clean_text(reason, 600)}"
    else:
        answer += "\n\n현재 상태로 변경된 사유는 티켓 이력에서 확인되지 않았습니다."
    source = Evidence(key="STATUS_HISTORY", kind="TICKET_HISTORY" if event else "TICKET_DETAILS",
                      source_id=ticket.id, title=f"{ticket.display_number} 티켓", content=answer,
                      source_url=f"/api/v1/simulation-runs/{context.run_id}/tickets/{ticket.id}",
                      ticket_version=event.ticket_version if event else ticket.version)
    return answer, source

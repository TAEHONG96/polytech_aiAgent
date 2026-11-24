import re
from enum import StrEnum


class Intent(StrEnum):
    TICKET_SUMMARY = "TICKET_SUMMARY"
    TROUBLESHOOTING = "TROUBLESHOOTING"
    SOP_LOOKUP = "SOP_LOOKUP"
    GENERAL_QUESTION = "GENERAL_QUESTION"
    FOLLOW_UP = "FOLLOW_UP"


SOP = re.compile(r"(?<![a-z])sop(?![a-z])|표준\s*(?:작업\s*)?절차(?:서)?|(?:작업|절차|관련)\s*문서|매뉴얼", re.I)
GENERAL_SOP = re.compile(r"SOP(?:란|가\s*뭐|의\s*(?:뜻|의미))|SOP\s*뜻", re.I)
FOLLOW_UP = re.compile(r"^(?:그\s*(?:다음|럼|렇다면)|그\s*후|다음(?:엔|에는|으로)|이어서)|앞서|아까|방금|이전\s*답변|더\s*(?:자세히|설명)")
THIS_TICKET = re.compile(r"(?:이|현재|지금\s*보고\s*있는)\s*티켓|이\s*장애")
NEW_TICKET = re.compile(r"(?:이|현재|새|다른|지금\s*보고\s*있는)\s*티켓")
SUMMARY = re.compile(r"요약|무슨\s*(?:작업|일|내용|장애)|무엇을\s*하는|(?:티켓|작업)\s*(?:내용|목적|지시|설명)|(?:현재|지금)\s*상태|어떤\s*(?:작업|티켓)")
DIAGNOSIS = re.compile(r"원인|진단|점검|트러블슈팅|복구|재현|해결\s*(?:법|방법)|확인\s*(?:할|해야|순서|방법|절차)|(?:무엇|뭐|어디)[을를부터\s]*확인|먼저\s*(?:확인|관찰)|오류\s*코드")
CONCEPT = re.compile(r"차이|비교|원리|정의|개념|어떤\s*기술|무슨\s*뜻")
SIMILAR = re.compile(r"유사|비슷|과거\s*(?:장애|사례)|이전\s*사례")


def classify(message: str) -> Intent:
    if SOP.search(message) and not GENERAL_SOP.search(message):
        return Intent.SOP_LOOKUP
    if FOLLOW_UP.search(message) and not NEW_TICKET.search(message):
        return Intent.FOLLOW_UP
    if SUMMARY.search(message):
        return Intent.TICKET_SUMMARY
    if CONCEPT.search(message) and not THIS_TICKET.search(message):
        return Intent.GENERAL_QUESTION
    if DIAGNOSIS.search(message) or SIMILAR.search(message):
        return Intent.TROUBLESHOOTING
    return Intent.GENERAL_QUESTION


def needs_sop(intent: Intent, message: str, previous_intent: str | None) -> bool:
    return intent in (Intent.SOP_LOOKUP, Intent.TROUBLESHOOTING) or (
        intent == Intent.FOLLOW_UP
        and (DIAGNOSIS.search(message) is not None or previous_intent in (Intent.SOP_LOOKUP, Intent.TROUBLESHOOTING))
    )

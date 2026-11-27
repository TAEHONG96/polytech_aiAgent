from app.simulation.evaluation_input import RESOLVED
from app.simulation.corpus_actions import DECISIONS, DIAGNOSTICS, INTERVENTIONS

RULES = {
    "UNDERSTAND_INCIDENT": ("장애 이해", "가상 상태나 BMC 진단으로 증상과 오류를 먼저 확인하세요."),
    "CHECK_HOST_HISTORY": ("동일 호스트 이력 확인", "승인된 동일 호스트의 발생 전 이력을 확인하세요."),
    "USE_HOST_HISTORY": ("동일 호스트 이력 활용", "과거 사례와 현재 결과를 비교하고 수행 조치에 근거를 연결하세요."),
    "CHECK_SOP": ("승인 SOP 확인", "승인 SOP에서 점검 절차와 재확인 조건을 확인하세요."),
    "USE_SOP": ("승인 SOP 활용", "SOP 확인 후 수행한 조치에 자료를 연결하세요."),
    "APPROPRIATE_VIRTUAL_ACTION": ("가상 조치 타당성", "가상 진단 결과와 선행 조건을 확인해 적절한 조치를 선택하세요."),
    "POST_REPAIR_VALIDATION": ("조치 후 재검증", "가상 조치 이후 재진단해 정상 또는 지속 장애를 기록하세요."),
    "DOCUMENT_ACCURACY": ("검증 가능한 업무 기록", "관측한 결과와 다음 조치를 작업 또는 인계에 남기세요."),
    "HANDOVER_UNRESOLVED": ("미해결 업무 인계", "미해결 티켓과 후속 작업을 인계에 포함하고 다음 조치를 적으세요."),
}


def bmc_decision_evidence(ticket_key: str, performed: list[dict]) -> list[dict]:
    if ticket_key == "ticket-01":
        decision = ("RECORD_REMAINING_RISK", "FOLLOWUP_RECORDED")
        prerequisites = (
            ("CHECK_HOST_SERVICE", "SERVICE_RESPONDING"), ("RUN_BMC_DIAGNOSTICS", "BMC_TIMEOUT_OBSERVED"),
            ("CHECK_MANAGEMENT_NETWORK", "MANAGEMENT_MEASURED"), ("OPEN_ALTERNATE_CONSOLE", "CONSOLE_REACHABLE"),
        )
    else:
        decision = ("REQUEST_ADDITIONAL_METRICS", "REQUEST_RECORDED")
        prerequisites = (("CHECK_MANAGEMENT_NETWORK", "MISSING_DATA"), ("OPEN_ALTERNATE_CONSOLE", "MISSING_DATA"))
    seen = {}
    for event in performed:
        details = event["details"]
        observed = (details.get("action_code"), (details.get("result") or {}).get("outcome"))
        if observed == decision and all(check in seen for check in prerequisites):
            return [seen[check] for check in prerequisites] + [event]
        seen[observed] = event
    return []


def grade(ticket: dict, report_id: str, *, flexible: bool = False, network_pilot: bool = False,
          bmc_pilot: bool = False) -> dict:
    timeline = ticket["events"]
    performed = [event for event in timeline if event["type"] == "virtual_action_performed"]
    by_id = {event["id"]: event for event in performed}
    observed = []
    evidence = {}

    def ref(kind, identifier):
        evidence[identifier] = {"kind": kind, "id": identifier}
        return identifier

    def emit(code, verdict, refs=()):
        observed.append({"rule_code": code, "result": verdict, "evidence_refs": list(dict.fromkeys(refs))})

    def event_refs(events):
        return [ref("event", event["id"]) for event in events]

    def code(event):
        return event["details"].get("action_code")

    def outcome(event):
        return (event["details"].get("result") or {}).get("outcome")

    diagnostic_codes = {"CHECK_SERVER_STATUS", "RUN_BMC_DIAGNOSTICS"}
    if flexible:
        diagnostic_codes |= DIAGNOSTICS | {"CHECK_MEMORY", "CHECK_STORAGE"}
    diagnosed = [event for event in performed if code(event) in diagnostic_codes]
    emit("UNDERSTAND_INCIDENT", "PASS" if diagnosed else "NOT_OBSERVED", event_refs(diagnosed))

    for kind, check, use, source_kind in (
        ("HOST_HISTORY", "CHECK_HOST_HISTORY", "USE_HOST_HISTORY", "incident"),
        ("SOP", "CHECK_SOP", "USE_SOP", "sop"),
    ):
        learning = [event for event in timeline if event["type"] == "learning_evidence" and event["details"]["kind"] == kind]
        views = {event["details"]["source_id"]: event for event in learning if event["details"]["activity"] == "VIEWED"}
        refs = [ref(source_kind, source) for source in views] + [ref("learning", event["id"]) for event in views.values()]
        emit(check, "PASS" if views else "NOT_OBSERVED", refs)
        used = []
        for event in learning:
            link = event["details"]
            action = by_id.get(link["action_event_id"])
            viewed = views.get(link["source_id"])
            if (link["activity"] == "USED" and action and viewed
                    and viewed["sequence"] < action["sequence"] < event["sequence"]):
                used += [ref(source_kind, link["source_id"]), ref("learning", event["id"]), ref("event", action["id"])]
        emit(use, "PASS" if used else "NOT_MET" if views else "NOT_OBSERVED", used or refs)

    asset = ticket["asset_kind"]
    action_code = {"memory": "REPLACE_MEMORY", "storage": "CHECK_STORAGE"}.get(asset)
    interventions = [event for event in performed if code(event) in INTERVENTIONS] if flexible else [
        event for event in performed if code(event) == action_code
    ]
    supported_decisions = [event for event in performed if code(event) in DECISIONS
                           and any(check["sequence"] < event["sequence"] for check in diagnosed)] if flexible else []
    pilot_ticket = ticket["key"] if network_pilot else None
    bmc_ticket = bmc_pilot and ticket["key"] in {"ticket-01", "ticket-02"}
    if bmc_ticket:
        supported_decisions = bmc_decision_evidence(ticket["key"], performed) if ticket["status"] not in RESOLVED else []
    elif pilot_ticket == "ticket-02":
        missing = [event for event in diagnosed if code(event) == "COMPARE_PORT_COUNTERS" and outcome(event) == "MISSING_DATA"]
        requests = [event for event in performed if code(event) == "REQUEST_PEER_MEASUREMENT"
                    and outcome(event) == "REQUEST_RECORDED" and any(check["sequence"] < event["sequence"] for check in missing)]
        supported_decisions = missing + requests if requests and not interventions and ticket["status"] not in RESOLVED else []
    elif pilot_ticket == "ticket-01":
        supported_decisions = []
    elif flexible and not interventions and not supported_decisions and ticket["status"] not in RESOLVED:
        supported_decisions = diagnosed if ticket["included"] and ticket["handover_next_action"] else []
    justified = interventions or supported_decisions
    applies = flexible or bool(action_code)
    emit("APPROPRIATE_VIRTUAL_ACTION", "NOT_APPLICABLE" if not applies else "PASS" if justified
         else "NOT_MET" if performed else "NOT_OBSERVED", event_refs(justified or performed))
    after = []
    if interventions:
        last = interventions[-1]
        validation_code = "RERUN_DIAGNOSTICS"
        if code(last) == "SWITCH_REDUNDANT_PATH":
            validation_code = "MEASURE_PACKET_LOSS" if asset == "network" else "MEASURE_STORAGE_LATENCY"
        after = [event for event in performed if code(event) == validation_code and event["sequence"] > last["sequence"]]
        if pilot_ticket == "ticket-01":
            after = [event for event in after if outcome(event) in {"SERVICE_RECOVERED", "SERVICE_IMPAIRED"}]
    emit("POST_REPAIR_VALIDATION", "NOT_APPLICABLE" if not interventions else "PASS" if after else "NOT_MET",
         event_refs(interventions + after))
    tasks = [task for task in ticket["work"] if task["note"] or task["status"] == "done"]
    recorded = bool(tasks or ticket["handover_next_action"] or ticket["resolution"])
    proof = event_refs(after or performed) + [ref("work", task["id"]) for task in tasks]
    if ticket["handover_next_action"]:
        proof.append(ref("handover", report_id))
    documented = (after or supported_decisions) and recorded
    explicit_handover = bmc_ticket or pilot_ticket in {"ticket-01", "ticket-02"}
    if explicit_handover:
        documented = (after or supported_decisions) and ticket["included"] and bool(ticket.get("handover_note"))
    emit("DOCUMENT_ACCURACY", "PASS" if documented else "NOT_MET" if proof else "NOT_OBSERVED", proof)
    pending = [task for task in ticket["work"] if task["status"] != "done"]
    handoff_required = ticket["status"] not in RESOLVED or bool(pending)
    handoff_passed = ticket["included"] and bool(ticket["handover_next_action"])
    if explicit_handover:
        handoff_required = True
        handoff_passed = ticket["status"] not in RESOLVED and ticket["included"] and bool(ticket.get("handover_note"))
    handoff_refs = [ref("handover", report_id)] + [ref("work", task["id"]) for task in pending] if handoff_required else []
    emit("HANDOVER_UNRESOLVED", "NOT_APPLICABLE" if not handoff_required else "PASS" if handoff_passed else "NOT_MET",
         handoff_refs)
    return {"rules": observed, "evidence": list(evidence.values())}

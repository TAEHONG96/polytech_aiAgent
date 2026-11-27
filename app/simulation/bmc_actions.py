"""Manage BMC training checks from the readings published with a scenario."""

from app.simulation.corpus_actions import change, diagnosis, inspect

BMC_SCENARIO = "synthetic-ops-bmc"
BMC_VERSION = "1.1"
BMC_RULE_VERSION = "bmc-training-1"


def bmc_options(readings):
    window = readings["window"]

    def measurement(label, value, **fields):
        return {"label": label, "value": value, "window": window, **fields}

    service = readings["service_replies"]
    attempts = readings["service_attempts"]
    service_ok = service == attempts
    timeouts = readings["bmc_timeouts"]
    bmc_attempts = readings["bmc_attempts"]
    timeout_confirmed = bool(timeouts) and timeouts == bmc_attempts
    timeouts_code = "BMC_TIMEOUT" if timeout_confirmed else None
    timeout_status = diagnosis(timeouts_code, "BMC_TIMEOUT", normal=timeouts == 0)
    if timeouts:
        timeout_status["diagnostic_status"] = "ISSUE_FOUND"
    bmc_measurements = [
        measurement("관리 접속 시간 초과", timeouts, unit="회"),
        measurement("접속 시도", bmc_attempts, unit="회"),
    ]
    local, peer = readings["gateway_ms"], readings["peer_ms"]
    limit = readings["management_limit_ms"]
    management_complete = local is not None and peer is not None
    management_ok = management_complete and max(local, peer) <= limit
    console = readings["alternate_console"]
    missing = [label for label, value in (
        ("관리 게이트웨이 응답 시간", local), ("같은 관리망의 비교 장비 응답 시간", peer),
        ("대체 콘솔 접속 기록", console), ("BMC 재접속 시험 기록", timeouts),
    ) if value is None]

    result = {
        "CHECK_HOST_SERVICE": inspect(
            "호스트 서비스 확인", "업무 서비스 시험 요청의 응답을 확인했습니다.",
            outcome="SERVICE_RESPONDING" if service_ok else "SERVICE_IMPAIRED",
            set={"service_confirmed": service_ok},
            diagnostic_status="NO_ISSUE" if service_ok else "ISSUE_FOUND",
            observations=[measurement("응답 성공", service, unit="건"), measurement("시험 요청", attempts, unit="건")],
        ),
        "RUN_BMC_DIAGNOSTICS": inspect(
            "BMC 진단", "관리 접속 시험 결과를 확인했습니다." if timeouts is not None
            else "관리 접속 시험 기록이 없어 시간 초과 범위를 확인할 수 없습니다.",
            outcome="MISSING_DATA" if timeouts is None else
            "BMC_TIMEOUT_OBSERVED" if timeout_confirmed else "BMC_INTERMITTENT" if timeouts else "BMC_REACHABLE",
            set={"bmc_timeout_observed": timeout_confirmed}, observations=bmc_measurements, **timeout_status,
        ),
        "CHECK_MANAGEMENT_NETWORK": inspect(
            "관리망 확인", "같은 구간의 관리망 응답 시간을 기준과 비교했습니다." if management_complete
            else "비교 장비의 응답 표본이 없어 장애 범위를 확정할 수 없습니다.",
            outcome="MANAGEMENT_MEASURED" if management_complete else "MISSING_DATA",
            diagnostic_status="NO_ISSUE" if management_ok else "ISSUE_FOUND" if management_complete else "INCONCLUSIVE",
            set={"management_reviewed": True},
            observations=[measurement("관리 게이트웨이", local, unit="ms"),
                          measurement("같은 관리망의 비교 장비", peer, unit="ms"),
                          measurement("실습 응답 기준 (이하)", limit, unit="ms")],
        ),
        "OPEN_ALTERNATE_CONSOLE": inspect(
            "대체 콘솔 접속", "대체 콘솔 시험 기록을 확인했습니다." if console is not None
            else "대체 콘솔 접속 기록을 확인할 수 없습니다.",
            outcome="CONSOLE_REACHABLE" if console is True else "CONSOLE_UNREACHABLE" if console is False else "MISSING_DATA",
            diagnostic_status="NO_ISSUE" if console is True else "ISSUE_FOUND" if console is False else "INCONCLUSIVE",
            set={"console_reviewed": True, "console_reachable": console is True},
            observations=[measurement("대체 콘솔", {True: "접속 성공", False: "접속 실패"}.get(console))],
        ),
    }
    if missing:
        result["REQUEST_ADDITIONAL_METRICS"] = change(
            "관리망·콘솔 자료 요청", "관리망 담당에 누락 자료를 요청했습니다. 회신 전까지 원인 판정을 보류하세요.",
            requires={"management_reviewed": True, "console_reviewed": True},
            blocked="먼저 관리망과 대체 콘솔을 확인해 누락된 항목을 파악하세요.",
            outcome="REQUEST_RECORDED", unconfirmed=missing,
            observations=[measurement("요청 대상", readings["owner"]),
                          measurement("요청 자료", "같은 구간의 응답 표본, 원격 콘솔 실패 시각과 대체 콘솔 시험 결과"),
                          measurement("다음 확인", readings["recheck_at"])],
        )
    elif readings.get("risk_owner"):
        result["RECORD_REMAINING_RISK"] = change(
            "관리 접속 장애 후속 점검 기록", "관리 접속의 잔여 위험을 기록했습니다. 발신 인계에도 다음 점검을 남기세요.",
            requires={"service_confirmed": True, "bmc_timeout_observed": True, "management_reviewed": True,
                      "console_reachable": True},
            blocked="먼저 서비스, BMC, 관리망, 대체 콘솔을 확인하세요.",
            requires_note=True, outcome="FOLLOWUP_RECORDED",
            observations=[measurement("담당", readings["risk_owner"]), measurement("다음 확인", readings["recheck_at"])],
        )
    return result

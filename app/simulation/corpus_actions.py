"""Virtual action options for the published synthetic operations corpus."""

SCENARIOS = frozenset(f"synthetic-ops-{key}" for key in (
    "boot", "memory", "storage", "network", "power", "bmc", "major", "recovery",
))


def inspect(label, message, **rule):
    return label, {"message": message, "repeatable": True, **rule}


def change(label, message, **rule):
    return label, {"message": message, "once": True, **rule}


def paths(kind, code):
    network = kind == "network"
    ready = code in {"LINK_FLAP", "PACKET_LOSS", "PATH_DOWN"}
    return {
        "CHECK_REDUNDANT_PATH": inspect("우회 경로 상태 확인" if network else "이중 스토리지 경로 확인",
            "가상 우회 경로가 사용 가능합니다." if ready else "현재 우회 경로 상태를 이전 기록과 비교하세요.",
            **({"set": {"alternate_path_ready": True}} if ready else {})),
        "SWITCH_REDUNDANT_PATH": change("가상 경로 전환", "가상 우회 경로로 전환했습니다. 응답을 다시 측정하세요.",
            requires={"alternate_path_ready": True}, blocked="먼저 사용 가능한 우회 경로를 확인하세요.",
            set={"path_switched": True, "health": "verification_pending"}),
    }


def options(state):
    asset = state.get("asset_kind")
    code = state.get("error_code")
    verified = state.get("path_switched") is True
    known = code is not None
    if asset == "boot":
        return {
            "CHECK_POST_LOG": inspect("POST 로그 확인", "POST 이후 부팅 장치 단계까지 진행했습니다."
                if code == "BOOT_DEVICE_MISSING" else "마지막 POST 코드와 기록 유무를 콘솔 자료에서 비교하세요."),
            "CHECK_BOOT_ORDER": inspect("부팅 순서 확인", "UEFI 부팅 항목과 마지막 정상 설정을 비교할 수 있습니다."),
            "CHECK_BOOT_VOLUME": inspect("논리 볼륨 확인", "예상 논리 볼륨이 확인되지 않습니다. 초기화 전에 확인을 요청하세요."
                if code == "BOOT_DEVICE_MISSING" else "볼륨 상태와 POST 시점을 대조했습니다."),
        }
    if asset in {"memory", "memory-pressure"}:
        return {
            "COMPARE_ECC_COUNTERS": inspect("ECC 카운터 비교", "가상 ECC 경보가 남아 있습니다." if code == "MEM_ECC_ERROR"
                else "현재 확인된 ECC 경보가 없습니다. 직전 기록이나 프로세스 사용량을 비교하세요."),
            "CHECK_PROCESS_MEMORY": inspect("프로세스 메모리 확인", "호스트와 프로세스 사용량, OOM 기록을 함께 확인하세요."
                if asset == "memory-pressure" else "프로세스 지표와 슬롯 오류 기록을 별도로 비교했습니다."),
        }
    if asset in {"storage", "storage-path"}:
        return {
            "MEASURE_STORAGE_LATENCY": inspect("응답 시간·대기열 측정",
                "전환 후 가상 응답은 정상 범위입니다. 이중화는 별도로 확인하세요." if verified
                else "스토리지 응답과 대기열을 측정했습니다. 같은 시간대의 지연·경로 기록을 대조하세요.",
                **({"set": {"health": "healthy", "error_code": None}} if verified else {})),
            "COMPARE_BATCH_HISTORY": inspect("배치 이력 비교", "가상 배치 시간대와 지연 발생 시간대를 대조했습니다."),
            **paths(asset, code),
        }
    if asset == "network":
        return {
            "COMPARE_PORT_COUNTERS": inspect("양단 포트 카운터 비교", "양단 링크와 오류 카운터를 비교했습니다."),
            **paths(asset, code),
            "MEASURE_PACKET_LOSS": inspect("손실률 재측정", "전환 후 가상 손실이 기준선으로 돌아왔습니다."
                if verified else ("가상 경로 오류가 남아 있습니다." if known else "재현 가능한 손실 증거가 부족합니다."),
                **({"set": {"health": "healthy", "error_code": None}} if verified else {})),
        }
    if asset == "power":
        return {
            "CHECK_POWER_INPUTS": inspect("A/B 입력 확인", "한 입력의 이중화 경보를 확인했습니다." if known
                else "두 입력을 판정할 센서 기록이 부족하거나 경보가 없습니다. 이전 기록과 대조하세요."),
            "CHECK_UPSTREAM_POWER": inspect("상위 분기 이벤트 조회",
                "시설팀 확인을 요청했습니다. 분기 원인은 응답을 받기 전까지 미확인입니다."
                if state.get("facilities_requested") else "가상 호스트의 경보 시각을 확보했습니다. 상위 분기 기록은 시설팀에 확인하세요."),
            "REQUEST_FACILITIES_CHECK": change("시설팀 확인 요청", "가상 시설팀 확인 요청을 기록했습니다.",
                set={"facilities_requested": True}),
        }
    if asset == "bmc":
        return {
            "CHECK_MANAGEMENT_NETWORK": inspect("관리망 확인", "관리망 응답과 동료 장비의 연결 기록을 비교했습니다."),
            "CHECK_HOST_SERVICE": inspect("호스트 서비스 확인", "가상 호스트의 서비스 지표를 관리 접속 상태와 분리해 확인했습니다."),
            "OPEN_ALTERNATE_CONSOLE": inspect("대체 콘솔 접속", "가상 대체 콘솔은 접근 가능합니다. 관리 경로 증상과 비교하세요."
                if known or state.get("health") == "healthy" else "가상 대체 콘솔 접속 여부를 확인할 기록이 부족합니다."),
        }
    return {
        "CHECK_SERVICE_IMPACT": inspect("서비스별 영향 확인", "서비스별 정상·영향·미확인 범위를 나누어 확인했습니다."),
        "REQUEST_ADDITIONAL_METRICS": change("추가 지표 요청", "가상 지표 수집 요청을 남겼습니다.", set={"metrics_requested": True}),
        "RECORD_SHARED_TIMELINE": change("공통 시간선 기록", "가상 공통 시간선에 확인된 관측을 기록했습니다.", requires_note=True),
        "START_RECOVERY_OBSERVATION": change("복구 관찰 시작", "가상 관찰 창을 열었습니다. 재발과 잔여 위험을 확인하세요.",
            set={"observing_recovery": True}),
        "COMPARE_RECOVERY_METRICS": inspect("조치 전후 지표 비교", "관찰 창의 지표를 앞선 기록과 비교했습니다."
            if state.get("observing_recovery") else "지표를 비교했습니다. 안정 판정에 필요한 관찰 창은 아직 열리지 않았습니다."),
        "CHECK_RECURRENCE": inspect("재발 조건 확인", "가상 재발 시각과 직전 조치를 비교했습니다."),
        "RECORD_REMAINING_RISK": change("잔여 위험 인계 기록", "가상 잔여 위험 기록을 남겼습니다. 발신 인계 초안에 다음 조치를 적으세요.",
            requires_note=True),
    }


DIAGNOSTICS = {
    "CHECK_POST_LOG", "CHECK_BOOT_ORDER", "CHECK_BOOT_VOLUME", "COMPARE_ECC_COUNTERS", "CHECK_PROCESS_MEMORY",
    "MEASURE_STORAGE_LATENCY", "COMPARE_BATCH_HISTORY", "CHECK_REDUNDANT_PATH", "COMPARE_PORT_COUNTERS",
    "MEASURE_PACKET_LOSS", "CHECK_POWER_INPUTS", "CHECK_UPSTREAM_POWER", "CHECK_MANAGEMENT_NETWORK",
    "CHECK_HOST_SERVICE", "OPEN_ALTERNATE_CONSOLE", "CHECK_SERVICE_IMPACT", "COMPARE_RECOVERY_METRICS", "CHECK_RECURRENCE",
}
INTERVENTIONS = {"SWITCH_REDUNDANT_PATH", "REPLACE_MEMORY"}
DECISIONS = {"REQUEST_FACILITIES_CHECK", "REQUEST_ADDITIONAL_METRICS", "RECORD_SHARED_TIMELINE",
             "START_RECOVERY_OBSERVATION", "RECORD_REMAINING_RISK"}

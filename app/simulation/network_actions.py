"""Versioned virtual measurements for the network training pilot."""

from app.simulation.corpus_actions import change, inspect

NETWORK_SCENARIO = "synthetic-ops-network"
NETWORK_VERSION = "1.1"
NETWORK_RULE_VERSION = "synthetic-network-1"


def network_options(state):
    baseline = state["network_observations"]
    peer_available = baseline["peer_crc_delta"] is not None
    ready = peer_available and baseline["alternate_probe"] is True
    switched = state.get("path_switched") is True
    missing = ["반대편 포트 CRC와 동일 구간 손실률"] if not peer_available else []
    residual = "기존 업링크의 CRC 증가 원인과 이중화 복구 여부"
    loss = {"label": "패킷 손실률", "target": baseline["probe_target"], "window": "10분",
            "value": baseline["loss_pct"], "unit": "%"}
    measure = inspect("패킷 손실률 재측정", "현재 경로의 10분 가상 측정값입니다.", outcome="MEASURED",
                      observations=[loss], unconfirmed=missing)
    if switched:
        recovered = baseline["switched_loss_pct"] <= baseline["loss_limit_pct"]
        measure = inspect(
            "패킷 손실률 재측정",
            "우회 경로의 손실률이 실습 복구 기준을 충족했습니다. 기존 경로 위험을 인계하세요."
            if recovered else "우회 경로의 손실률이 복구 기준을 초과합니다. 서비스 영향도 함께 인계하세요.",
            outcome="SERVICE_RECOVERED" if recovered else "SERVICE_IMPAIRED",
            set={"health": "degraded"},
            observations=[{**loss, "before": loss["value"], "value": baseline["switched_loss_pct"]},
                          {"label": "실습 복구 기준 (이하)", "value": baseline["loss_limit_pct"], "unit": "%"}],
            unconfirmed=[residual],
        )

    return {
        "COMPARE_PORT_COUNTERS": inspect(
            "양단 포트 카운터 비교", "같은 10분 구간의 포트 카운터를 조회했습니다." if peer_available
            else "로컬 카운터를 확인했습니다. 반대편 표본이 없어 손실 구간을 좁힐 수 없습니다.",
            outcome="MEASURED" if peer_available else "MISSING_DATA", set={"ports_reviewed": True},
            observations=[
                {"label": "링크 플랩", "target": baseline["local_port"], "window": baseline["window"],
                 "value": baseline["flaps"], "unit": "회"},
                {"label": "CRC 증가", "target": baseline["local_port"], "window": baseline["window"],
                 "value": baseline["local_crc_delta"], "unit": "건"},
                {"label": "CRC 증가", "target": baseline["peer_port"], "window": baseline["window"],
                 "value": baseline["peer_crc_delta"], "unit": "건"},
            ], unconfirmed=missing,
        ),
        "CHECK_REDUNDANT_PATH": inspect(
            "우회 경로 상태 확인", "우회 링크가 살아 있고 시험 요청이 성공했습니다." if ready
            else "전환을 승인할 근거가 부족합니다. 반대편 표본과 우회 경로 시험 결과를 확인하세요.",
            outcome="READY" if ready else "MISSING_DATA", set={"alternate_path_ready": ready},
            observations=[{"label": "우회 링크", "target": baseline["alternate_path"],
                           "value": baseline["alternate_link"]},
                          {"label": "우회 시험 요청", "target": baseline["probe_target"],
                           "value": "성공" if ready else None}],
            unconfirmed=[] if ready else [*missing, "우회 서비스 시험"],
        ),
        "SWITCH_REDUNDANT_PATH": change(
            "가상 경로 전환", "가상 우회 경로로 전환했습니다. 같은 구간과 시간창으로 손실률을 다시 측정하세요.",
            requires={"ports_reviewed": True, "alternate_path_ready": True},
            blocked="양단 카운터 비교와 우회 경로 시험이 모두 필요합니다." if ready
            else "반대편 표본과 우회 경로 시험 결과가 없어 전환을 보류합니다.",
            set={"path_switched": True, "health": "verification_pending"}, outcome="VERIFICATION_PENDING",
            observations=[{"label": "서비스 경로", "before": baseline["local_port"], "value": baseline["alternate_path"]}],
            unconfirmed=["전환 후 서비스 손실률", residual],
        ),
        "MEASURE_PACKET_LOSS": measure,
        **({"REQUEST_PEER_MEASUREMENT": change(
            "반대편 표본 요청", "가상 담당 팀에 동일 구간의 10분 표본을 요청했습니다. 응답 대기 상태입니다.",
            requires={"ports_reviewed": True}, blocked="먼저 양단 포트 카운터를 비교해 누락을 확인하세요.",
            outcome="REQUEST_RECORDED", observations=[
                {"label": "요청 대상", "target": baseline["peer_port"], "value": baseline["peer_owner"]},
                {"label": "요청 범위", "value": "같은 시각의 CRC 증가량과 양단 손실률, 우회 시험 결과", "window": "10분"},
            ], unconfirmed=[*missing, "우회 서비스 시험"],
        )} if not peer_available else {}),
    }

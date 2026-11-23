"""Synthetic source material for the v1.1 network scenario."""

from tests.corpus_data import SCENARIOS

SOP_KEY = "network-triage-v1.1"
SOP_TITLE = "네트워크 링크 플랩과 패킷 손실 대응 v1.1"
SOP_QUERY = "반대편 측정값 없이 경로를 전환해도 되나?"
SOP_SECTION = "반대편 측정값이 없을 때 경로 전환 보류"
SOP = f"""# {SOP_TITLE}

가상 데이터센터 교육용 SOP. 아래 수치와 판정 기준은 이 시나리오에 적용합니다.

## 적용 범위

`LINK_FLAP` 또는 `PACKET_LOSS`가 발생한 서비스 경로에 적용합니다. 티켓에서 영향받은 서비스와 현재 사용 중인 포트를 먼저 확인합니다.

## 양단 포트 카운터와 서비스 영향 확인

1. 두 포트에서 같은 10분 구간의 업·다운 횟수와 CRC 시작·종료값을 확인합니다. 비교표에는 누적값보다 해당 구간의 증가량을 기록합니다.
2. 서비스 시험 요청의 대상과 10분 시간창을 기록합니다. 로컬 손실률이 정상이어도 상대편 표본이 비어 있으면 해당 구간은 미확인으로 표시합니다.
3. 한쪽에서만 CRC가 증가하면 조사 범위를 그 링크로 좁힙니다. 카운터만으로 포트, 케이블, 상대 장비 중 하나를 원인으로 확정할 수는 없습니다.

## 가상 우회 경로 전환 조건

1. 양단 포트 비교를 완료합니다. 대상 경로와 우회 경로를 구분하고, 우회 링크가 연결된 상태에서 서비스 시험 요청이 성공했는지 확인합니다.
2. 두 조건이 충족되면 확인한 한 경로를 가상 전환합니다. 전환 직후에는 서비스 복구 판정을 보류합니다.

## {SOP_SECTION}

반대편 측정값이나 우회 경로 시험 결과가 없으면 경로 전환을 보류합니다. 상대편 네트워크 담당에게 동일 시각의 10분 구간 CRC 증가량과 양단의 패킷 손실률, 우회 시험 결과를 요청합니다. 발신 인계에 확인한 로컬 수치, 누락 지표, 요청 대상과 다음 확인 시각을 기록합니다.

## 전환 후 재측정과 잔여 위험 인계

1. 전환 전과 동일한 서비스 시험 대상에서 10분 손실률을 재측정합니다. 이 실습의 서비스 복구 기준은 0.5% 이하입니다. 조치 전후 값과 기준을 함께 기록합니다.
2. 기준을 넘으면 서비스 영향을 계속 추적합니다. 기준을 충족하면 서비스 회복을 기록하고, 기존 링크의 CRC 증가와 이중화 복구 여부를 별도로 확인합니다.
3. 원래 경로의 이상이나 이중화 문제가 남으면 티켓을 열어 두고, 점검할 포트와 담당 팀, 다음 확인 시각을 발신 인계에 남깁니다.
"""

NETWORK_ROW = (*SCENARIOS["network"][:-1], [
    ("in_progress", "P2", "link-flap", "업링크 반복 플랩", "서비스 시험 요청에 간헐 손실이 발생합니다.",
     "19:45–19:55 KST · LINK_FLAP · 플랩 7회 · 로컬 CRC +130건 · 서비스 손실률 4.0%",
     "로컬 포트 경보와 서비스 영향을 확인했습니다. 양단 비교와 우회 시험은 이번 교대에서 수행해야 합니다.",
     "양단 카운터와 우회 시험을 확인한 뒤 조치 여부를 결정"),
    ("waiting", "P2", "network-path", "특정 구간 패킷 손실", "서비스 일부 요청에서 통신이 끊깁니다.",
     "19:45–19:55 KST · PACKET_LOSS · 로컬 손실률 4.0% · 상대편 표본 미수집",
     "로컬 지표를 확보했습니다. 상대편과 우회 경로 시험 결과는 아직 받지 못했습니다.",
     "누락된 표본과 우회 시험 결과를 확인한 뒤 손실 구간을 좁힐 것"),
    *SCENARIOS["network"][-1][2:],
])

NETWORK_OBSERVATIONS = {
    "ticket-01": {"window": "19:45–19:55 KST (10분)", "local_port": "SIM-NETWORK-01/UP1",
                  "peer_port": "SIM-PEER-01/P17", "flaps": 7, "local_crc_delta": 130, "peer_crc_delta": 0,
                  "alternate_path": "SIM-NETWORK-01/UP2", "alternate_link": "연결", "alternate_probe": True,
                  "probe_target": "서비스 시험 요청 A", "loss_pct": 4.0, "switched_loss_pct": 0.2, "loss_limit_pct": 0.5},
    "ticket-02": {"window": "19:45–19:55 KST (10분)", "local_port": "SIM-NETWORK-02/UP1",
                  "peer_port": "SIM-PEER-02/P18", "flaps": 0, "local_crc_delta": 0, "peer_crc_delta": None,
                  "alternate_path": "SIM-NETWORK-02/UP2", "alternate_link": None, "alternate_probe": None,
                  "probe_target": "서비스 시험 요청 B", "loss_pct": 4.0, "peer_owner": "상대편 네트워크 담당"},
}

NETWORK_IMPACT = {
    "ticket-01": "서비스 시험 요청 A에서 10분 동안 4.0% 손실. 관리망 응답은 정상.",
    "ticket-02": "서비스 시험 요청 B의 로컬 경계에서 10분 동안 4.0% 손실. 전체 영향 범위는 확인 중.",
}

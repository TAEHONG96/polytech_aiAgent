"""Synthetic operational observations and SOP text. Assessment keys live separately."""

SOPS = {
    "post": (
        "서버 부팅 실패와 POST 점검",
        "전원은 들어오지만 POST가 멈추거나 콘솔에 부팅 화면이 나오지 않을 때 적용한다.",
        "콘솔의 마지막 POST 코드와 BMC 이벤트 시각을 기록한다. 전원 상태와 최근 변경 이력을 대조한다.",
        "데이터가 남아 있으면 강제 전원 차단 전에 영향과 승인 여부를 확인한다. 읽기 전용 콘솔로 마지막 단계와 메모리 초기화 여부를 확인한다.",
        "POST 완료와 OS 부팅을 각각 확인한다. 같은 단계에서 다시 멈추면 코드와 변경 이력을 첨부해 인계한다.",
    ),
    "boot-device": (
        "부팅 장치 미인식과 부팅 순서 점검",
        "BOOT_DEVICE_MISSING 또는 정상 POST 뒤 부팅 장치가 검색되지 않을 때 적용한다.",
        "콘솔 부팅 메시지, UEFI 부팅 순서, 스토리지 컨트롤러의 논리 볼륨 상태를 비교한다.",
        "마지막 정상 설정과 대조한다. 볼륨이 확인되지 않으면 초기화나 새 배열 생성 전에 스토리지 담당 조직에 인계한다.",
        "예상 논리 볼륨과 부팅 항목을 확인하고, 승인된 복구 후 OS와 서비스 점검을 각각 통과해야 종료한다.",
    ),
    "ecc": (
        "메모리 ECC 경보와 장애 모듈 확인",
        "MEM_ECC_ERROR, 정정 불가 ECC 또는 특정 슬롯의 정정 오류 증가에 적용한다.",
        "BMC 진단으로 오류 종류와 슬롯을 확인하고, 재발 시각과 누적 증가량을 기록한다.",
        "영향과 이중화 여유를 확인한다. 실습에서는 메모리 점검 후 가상 교체를 수행하고, 실제 절차에서는 승인된 점검 창을 확보한다.",
        "교체 뒤 재진단과 오류 카운터 관찰을 수행한다. 동일 슬롯에서 재발하면 모듈 외 경로도 조사한다.",
    ),
    "memory-capacity": (
        "메모리 압박과 하드웨어 경보 구분",
        "메모리 사용량 급증, OOM 또는 애플리케이션 재시작이 관측될 때 적용한다.",
        "호스트와 프로세스의 사용량 추이, OOM 이벤트, BMC ECC 이벤트 유무를 비교한다.",
        "메모리 압박이 확인되면 적용 범위와 승인 조건을 확인해 부하를 줄인다. 하드웨어 증거가 부족하면 부품 교체를 보류한다.",
        "사용량과 재시작률이 기준선으로 돌아왔는지 관찰한다. 시간대가 빠진 로그는 추가 요청한다.",
    ),
    "storage-latency": (
        "스토리지 대기열과 응답 지연 진단",
        "STORAGE_LATENCY 또는 파일·볼륨 요청의 응답 지연에 적용한다.",
        "p95 응답 시간, 대기열 깊이, IOPS와 오류율을 같은 시간대에 비교한다.",
        "읽기·쓰기 범위를 나누고 백업 배치 및 복제 지연을 대조한다. 지연만으로 장치 교체나 재시작을 결정하지 않는다.",
        "부하가 내려간 뒤 응답과 대기열을 재측정한다. 지연이 남으면 수치와 다음 관찰 시점을 인계한다.",
    ),
    "storage-path": (
        "이중 스토리지 경로 장애와 배열 보호",
        "PATH_DOWN, 다중 경로 축소 또는 배열 경로 경보에 적용한다.",
        "활성 경로 수, 경로별 오류와 배열 복제·재구축 상태를 확인한다.",
        "살아 있는 경로를 유지하고 경로별 장애 범위를 확인한다. 공유 경로를 동시에 변경하지 않는다.",
        "이중 경로 및 읽기·쓰기 검증을 완료한다. 단일 경로 상태는 위험과 복구 순서를 인계한다.",
    ),
    "link-flap": (
        "네트워크 링크 플랩과 오류 카운터 진단",
        "LINK_FLAP 또는 인터페이스의 반복 업·다운과 CRC 증가에 적용한다.",
        "양쪽 포트의 링크 이력, CRC, 수신 신호 지표와 영향받은 서비스 경로를 비교한다.",
        "이중화 여유를 확인하고 승인된 조건에서 한 번에 한 경로만 점검한다. 무관한 포트 변경을 피한다.",
        "오류 카운터 증가와 플랩이 멈췄는지 관찰한다. 반복 시간대와 영향 범위를 인계한다.",
    ),
    "network-path": (
        "네트워크 손실과 경로 경계 확인",
        "PACKET_LOSS 또는 특정 구간만 통신 지연이 증가할 때 적용한다.",
        "양단 측정, 게이트웨이 경로, 드롭 카운터와 최근 구성 변경을 같은 시각 기준으로 대조한다.",
        "영향 경계를 먼저 좁히고 구성 변경과 증상 시각의 상관성을 확인한다. 단일 관측만으로 원인을 확정하지 않는다.",
        "두 측정 지점의 손실률과 서비스 응답을 재측정한다. 측정이 빠진 구간은 요청 대상을 남긴다.",
    ),
    "power": (
        "이중 전원 공급 경보 대응",
        "PSU_REDUNDANCY_LOST 또는 한 전원 입력 경로가 상실됐을 때 적용한다.",
        "BMC 전원 이벤트, 이중 PSU 상태, 상위 전원 분기 경보와 같은 시각의 주변 장비를 비교한다.",
        "정상 전원 경로를 보존하고 영향 범위를 확인한다. 승인과 담당 조직 확인 없이 전원 분기를 변경하지 않는다.",
        "양쪽 입력과 이중화 정상 상태를 확인한다. 한 경로만 남으면 위험, 감시 기준, 인계 대상을 기록한다.",
    ),
    "bmc": (
        "BMC 관리 경로 장애 진단",
        "BMC_TIMEOUT 또는 원격 콘솔이 응답하지 않을 때 적용한다.",
        "관리망 응답, 콘솔 가용성, 호스트 서비스 지표와 다른 관리 장비 영향을 분리해 비교한다.",
        "호스트 서비스가 정상이라면 관리 경로를 우선 조사한다. 승인과 대체 콘솔 없이 전원 조치를 하지 않는다.",
        "관리 접속과 서비스 응답을 각각 확인한다. 장애 시각을 단정할 수 없으면 수집 누락을 인계한다.",
    ),
    "incident-command": (
        "긴급 장애 지휘와 공통 시간선",
        "P1 또는 여러 서비스에 동시에 영향이 발생해 조직 간 조율이 필요할 때 적용한다.",
        "영향받은 서비스, 시작 시각, 확인된 지표, 현재 조치와 미확인 사항을 공통 시간선에 수집한다.",
        "대응 책임 창구를 정하고, 동시 변경은 조율한다. 확인된 사실과 가설을 분리하며 단계별 승인과 복구 기준을 명시한다.",
        "서비스별 회복 지표를 검증하고, 남은 위험과 다음 갱신 시각을 다음 근무조에 인계한다.",
    ),
    "incident-recovery": (
        "긴급 복구 확인과 재발 감시 인계",
        "복구 직후 오류가 잠시 감소했거나 반복 경보로 종료 판단이 어려울 때 적용한다.",
        "조치 전후의 오류율, 응답 시간, 이중화 상태와 마지막 재발 시각을 비교한다.",
        "검증 항목별 담당 역할과 관찰 창을 정한다. 한 번의 정상 샘플만으로 종료하지 않는다.",
        "관찰 창에 재발이 없고 서비스 기준을 만족하면 완료를 기록한다. 미확인 항목은 담당 조직과 판정 기준을 인계한다.",
    ),
}

# Each observation has its own symptom, telemetry, work already done, and a remaining task.
# status, priority, SOP key, title, observed symptom, diagnostic excerpt, performed work, next action
SCENARIOS = {
    "boot": ("부팅 장애", "boot", "compute", "POST_STALL", [
        ("in_progress", "P2", "post", "POST 초기화 정지", "재부팅 후 콘솔이 초기화 화면에서 멈춤", "POST=0x15, console_elapsed=180s", "BMC 콘솔 확보 및 마지막 코드 기록", "마지막 정상 POST 코드와 변경 기록 비교"),
        ("waiting", "P2", "boot-device", "부팅 볼륨 미인식", "POST를 통과했으나 OS 시작 실패", "BOOT_DEVICE_MISSING, volume inventory unavailable", "부팅 순서 캡처, 배열 상태 조회 요청", "스토리지 담당 조직에서 볼륨 상태 확인"),
        ("resolved", "P3", "post", "콘솔 표시 지연", "부팅 화면이 잠시 검게 표시됨", "POST=complete, service_probe=pass", "콘솔 재접속과 서비스 응답 확인 완료", "다음 재부팅 시 콘솔 지연 재발 여부 관찰"),
        ("open", "P3", "boot-device", "복구 부팅 항목 반복 선택", "정상 장치 대신 복구 항목이 간헐 선택됨", "boot_entry=recovery, selected_twice=true", "초기 콘솔 캡처와 알림 접수", "정상 장치 상태와 UEFI 순서 대조"),
        ("open", "P4", "post", "부팅 로그 수집 누락", "사용자가 부팅 지연을 보고했으나 기록이 없음", "console_capture=missing, BMC clock unverified", "증상 시각 범위만 확보", "시각 동기화와 콘솔 로그 수집"),
    ]),
    "memory": ("메모리 장애", "memory", "compute", "MEM_ECC_ERROR", [
        ("in_progress", "P2", "ecc", "동일 슬롯 ECC 증가", "가상 메모리 슬롯 한 곳에 경보가 반복됨", "MEM_ECC_ERROR, DIMM=A1, delta=24", "BMC 이벤트와 호스트 성능 지표 대조", "BMC 메모리 진단 후 슬롯을 점검"),
        ("waiting", "P2", "memory-capacity", "프로세스 메모리 압박", "작업 프로세스가 반복 재시작함", "OOM=2, rss=94%, ECC_delta=0", "프로세스 사용량과 ECC 기록 수집", "배치 작업 메모리 사용량 확인"),
        ("resolved", "P3", "ecc", "메모리 경보 복구 확인", "이전 교대에서 슬롯 오류를 처리함", "post_validation=pass, ECC_delta=0", "교체 뒤 재진단과 서비스 응답 확인 완료", "다음 관찰 창에서 ECC 증가 여부 확인"),
        ("open", "P2", "ecc", "교체 뒤 동일 슬롯 경보 재발", "이전 교체 후 같은 위치에서 오류가 다시 발생", "MEM_ECC_ERROR, DIMM=B2, recurrence=2", "교체 전후 슬롯과 증상 시각 기록", "모듈 외 슬롯 및 경로를 함께 조사"),
        ("open", "P4", "memory-capacity", "메모리 알림 원인 미분류", "메모리 경보만 수신함", "memory_alarm=1, process metrics absent", "경보 발생 시각과 호스트 확인", "프로세스 지표와 BMC 이벤트 추가 수집"),
    ]),
    "storage": ("스토리지 장애", "storage", "storage", "STORAGE_LATENCY", [
        ("in_progress", "P2", "storage-latency", "파일 저장 지연", "파일 저장 요청이 느려짐", "STORAGE_LATENCY, p95=3200ms, queue=48", "읽기와 쓰기 지연 차이 기록", "배치 시간대와 대기열 변화를 대조"),
        ("waiting", "P2", "storage-path", "스토리지 경로 축소", "경로 두 개 중 하나가 사용 불가", "PATH_DOWN, active_paths=1", "정상 경로의 I/O 유지 여부 확인", "비정상 경로 상태를 담당 조직에 요청"),
        ("closed", "P3", "storage-latency", "백업 구간 일시 지연", "배치 종료 후 응답이 정상으로 돌아옴", "p95=42ms, queue=2", "배치 종료 뒤 응답과 대기열 정상 확인 완료", "다음 배치 때 동일 지표 비교"),
        ("open", "P2", "storage-path", "복제 경로 오류 재발", "복제 경로 경보가 두 번째 발생함", "PATH_DOWN, recurrence=2, replica_lag=18s", "경보와 복제 지연 시각 상관관계 기록", "경로별 오류 카운터와 동기화 상태 확인"),
        ("open", "P4", "storage-latency", "지연 측정 구간 불명", "지연 신고가 있으나 클라이언트 측정이 없음", "server_p95=unknown, trace_id=absent", "볼륨 ID 대신 가상 호스트 범위만 확보", "양단에서 같은 시각의 응답 측정"),
    ]),
    "network": ("네트워크 장애", "network", "network", "LINK_FLAP", [
        ("in_progress", "P2", "link-flap", "업링크 반복 플랩", "가상 업링크의 연결이 반복적으로 끊김", "LINK_FLAP, transitions=7, CRC_delta=130", "양단 포트 기록과 정상 우회 경로 확인", "오류 증가 구간을 좁히고 단일 경로 점검"),
        ("waiting", "P2", "network-path", "특정 구간 패킷 손실", "서비스 일부 요청에서 손실이 발생함", "PACKET_LOSS=4%, peer sample missing", "게이트웨이와 로컬 드롭 카운터 확보", "반대편 측정값 수집 뒤 구간 확정"),
        ("resolved", "P3", "link-flap", "포트 오류율 회복", "점검 후 링크가 안정화됨", "CRC_delta=0, link_flaps=0, window=30m", "양단 오류 증가와 서비스 응답 정상 확인 완료", "다음 근무에서 카운터 한 번 더 확인"),
        ("open", "P2", "link-flap", "야간 링크 오류 재발", "전일과 같은 시간대에 포트 경보가 다시 발생", "LINK_FLAP, previous_alert=night-window", "재발 시각과 우회 경로 활성 상태 기록", "동일 시간대 배치·변경 내역 대조"),
        ("open", "P4", "network-path", "단일 지점 통신 지연", "한 측정 지점에서만 지연 알림을 수신", "probe_A=180ms, probe_B=not_collected", "첫 측정 지점의 경로와 시각 저장", "두 번째 지점에서 동일 조건으로 측정"),
    ]),
    "power": ("전원 장애", "power", "facilities", "PSU_REDUNDANCY_LOST", [
        ("in_progress", "P2", "power", "전원 이중화 상실", "한 전원 입력에서 경보가 발생함", "PSU_REDUNDANCY_LOST, input_B=absent", "정상 입력과 서비스 연속성 확인", "시설 담당 조직에 분기 이벤트 대조 요청"),
        ("waiting", "P2", "power", "상위 전원 분기 상태 대기", "서버 전원 경보와 분기 경보가 같은 시간대에 발생", "PSU_B=fail, upstream_event=unknown", "BMC 이벤트 내보내기 및 시설 확인 요청", "분기 확인 전 정상 전원 경로 유지"),
        ("resolved", "P3", "power", "이중 입력 복구", "한쪽 입력 경보가 해제됨", "input_A=present, input_B=present, redundancy=ok", "두 입력 및 서비스 지표 정상 확인 완료", "다음 관찰 창에서 경보가 없는지 확인"),
        ("open", "P2", "incident-recovery", "짧은 전원 경보 반복", "정상으로 돌아왔다가 경보가 다시 발생", "redundancy_transitions=3, last_alert=08:00", "경보 시간선과 영향 장비 수 기록", "관찰 창을 설정하고 재발 시 시설 인계"),
        ("open", "P4", "power", "전원 입력 센서 데이터 누락", "관리 화면에 전원 데이터만 비어 있음", "PSU_sensor=not_available, service_probe=pass", "서비스 정상 확인 및 센서 누락 접수", "대체 경로에서 전원 상태를 확인"),
    ]),
    "bmc": ("관리 경로 장애", "bmc", "management", "BMC_TIMEOUT", [
        ("in_progress", "P2", "bmc", "BMC 응답 시간 초과", "관리 접속만 실패하고 서비스는 정상", "BMC_TIMEOUT, host_service=pass", "서비스와 관리 경로의 영향 분리", "관리망과 대체 콘솔 접속 확인"),
        ("waiting", "P3", "bmc", "원격 콘솔 자료 미수집", "콘솔 연결이 끊어지고 정확한 경계가 불명", "console=timeout, peer_management=unknown", "상태 보고와 캡처 요청 기록", "다른 관리 장비와 대체 콘솔 비교"),
        ("closed", "P3", "bmc", "관리 연결 회복", "관리 경로 점검 후 콘솔 연결이 정상화됨", "BMC=reachable, console=pass, service=pass", "관리 접속과 서비스 모두 확인 완료", "다음 근무에서 지연 재발 여부 확인"),
        ("open", "P2", "network-path", "관리망 지연 재발", "관리망 지연이 점검 뒤 반복 발생", "PACKET_LOSS=8%, business_net=pass", "서비스 경로 정상, 관리망 손실 시간선 기록", "다른 관리 장비와 경로 경계 대조"),
        ("open", "P4", "bmc", "관리 로그 시각 미확인", "장애 전에 수집된 로그의 타임존이 불명", "event_timezone=unknown, collection_gap=1h", "수집 누락 구간 표시", "BMC 시계와 로그 타임존 확인"),
    ]),
    "major": ("긴급 장애 지휘", "incident", "shared-services", "MULTI_SERVICE_ALERT", [
        ("in_progress", "P1", "incident-command", "복수 서비스 동시 오류", "같은 시간대에 여러 서비스 오류율이 증가", "MULTI_SERVICE_ALERT, affected_services=3", "공통 시간선과 영향 서비스 목록 작성", "대응 창구를 정하고 서비스별 지표를 확인"),
        ("waiting", "P1", "incident-command", "서비스 영향 범위 확인 대기", "증상은 지속되나 한 서비스의 영향이 미확인", "errors=high, service_C_metrics=missing", "나머지 두 서비스의 영향과 발생 시각 기록", "누락 지표를 담당 조직에 요청해 확인"),
        ("resolved", "P2", "incident-recovery", "일부 서비스 복구 검증", "영향 서비스 한 곳에서 응답이 회복됨", "p95=normal, error_rate=baseline, window=30m", "한 서비스의 기준선 회복과 재발 없음 확인 완료", "다른 서비스의 종료 판단과 분리해 관찰"),
        ("open", "P1", "incident-recovery", "긴급 복구 후 오류 재발", "일시 회복 뒤 같은 오류가 다시 나타남", "error_rate=12%, recovery_interval=9m", "회복 구간과 재발 시각을 공통 시간선에 추가", "복구 조치 전후 지표를 비교하고 지휘 채널에 갱신"),
        ("open", "P3", "incident-command", "공통 시간선 누락", "일부 변경의 실행 시각이 정리되지 않음", "timeline_entries=3, change_timestamp=missing", "현재까지 확인된 사실과 가설 분리 기록", "변경 담당 조직에 실행 시각과 결과 요청"),
    ]),
    "recovery": ("반복 장애와 복구 인계", "incident", "shared-services", "INTERMITTENT_ERROR", [
        ("in_progress", "P2", "incident-recovery", "복구 후 간헐 오류", "서비스가 정상과 오류 상태를 반복함", "INTERMITTENT_ERROR, recovery=8m, recurrence=3", "최근 조치 전후 오류율 기록", "관찰 창과 서비스 정상 판정 기준 설정"),
        ("waiting", "P2", "storage-latency", "잔여 스토리지 지연 확인 대기", "대부분 회복했으나 한 경로에서 지연 지속", "STORAGE_LATENCY, p95=1600ms, queue_sample=missing", "회복 구간과 지연 구간 구분", "지연 경로의 대기열 및 배치 상태 확인"),
        ("resolved", "P3", "incident-recovery", "관찰 창 종료 및 안정 확인", "기준치를 만족해 재발 감시 종료", "error_rate=baseline, redundancy=ok, window=60m", "상태와 이중화 정상, 관찰 중 재발 없음 확인 완료", "후속 근무에 종료 근거 보존"),
        ("open", "P2", "network-path", "경로 전환 구간 오류 반복", "경로 전환 시점에만 오류율이 증가", "PACKET_LOSS=3%, switchover_overlap=true", "전환과 오류의 시간 상관성을 기록", "반대쪽 지표와 경로 안정 상태를 비교"),
        ("open", "P4", "incident-recovery", "복구 판정 자료 부족", "정상 샘플 한 건만 수집됨", "sample_count=1, observation_window=unknown", "샘플이 수집된 시각 기록", "관찰 창을 정해 오류율과 응답 시간을 재수집"),
    ]),
}

# API 명세 — 데이터센터 교육 시뮬레이터

- 상태 · **통합 PRD 최종 후보 연계**. 교육 경로와 응답은 목표 계약이다.
- 기본 경로 · `/api/v1`. 루트 `GET /health`는 별도다.
- 제품 및 데이터 기준 · [통합 PRD 최종 후보](PRD-merged-draft.md) · [ERD](ERD.md)
- 기존 계약과 구현 근거 · [운영 API](API.md) · [현행 갭 분석](architecture-gap-analysis.md)

## 1. 공통 규칙

### 인증/CSRF

- 기존 `app/auth.py`의 **HttpOnly 세션 쿠키, Argon2, Origin/CSRF 검사** 유지. JWT 전환 금지.
- 인증된 읽기는 기존 사용자 검사를, 세션이 있는 변경과 기존 CSRF 보호 POST는 Origin·CSRF 검사를 적용한다. 세션이 없는 로그인·가입은 Origin·요청 제한을 적용한다. 가입의 세부 보호 방식은 공개 정책과 함께 확정한다.
- 쿠키 기반 cross-site 제약, SameSite/Secure 환경별 설정을 그대로 유지하며 가입/로그인 rate limiting 필요.
- 교육 컨텍스트는 반드시 **세션 user_id + simulation_run_id 소유권** 검증. `run_id` 또는 티켓 ID만으로 권한을 부여하지 않는다.
- 교육 계정이 기존 공유 운영 API를 호출할 수 없도록 서버에서 접근 차단(프론트 버튼 숨김만으로 불충분).

### 요청·응답/에러

- JSON 기본. 날짜와 시각은 오프셋 포함 ISO 8601, 표시 UI 시간대 `Asia/Seoul`.
- 식별자는 기존 코드 ID 형식을 유지하고, 신규 run ID는 UUID 권장.
- 기존 API의 DTO/커서·버전·CSRF·202 폴링 규약을 **호환 유지**; 신규 API에는 아래 형태를 권장.
- 오류 모양은 `app/http.py` 기존 형식을 우선. 아래 샘플은 의미 계약이며 실제 필드명은 어댑터 또는 명세 수정으로 맞춘다.
- `400`: 형식/의미 오류, `401`: 미인증, `403`: 인증되어도 금지된 관리 기능, `404`: 비소유·미존재 리소스, `409`: 버전 충돌·완료 실행 수정·중복 상태, `422`: 입력 검증 실패, `429`: 요청 제한, `503`: 필수 서비스 일시 장애.
- 비소유 `run_id`, `ticket_id`, 리비전, 평가·근거 문서는 존재 여부 노출 없는 `404` 권장.
- 실습 시작·가상 조치·보고서 확정의 재전송 방지에는 `Idempotency-Key`를 사용한다.
- 완료 실행의 티켓·작업·인계 원본 변경과 새 분석 요청을 차단한다. 기존 확정 요청의 재전송은 저장된 응답만 반환하며, 평가 worker는 별도의 평가 결과를 갱신한다.

### 공통 오류 예시(신규 UI 의미 계약)

```json
{
  "error": {
    "code": "SIMULATION_COMPLETED",
    "message": "완료된 실습은 수정할 수 없습니다."
  }
}
```

## 2. 경로 구조와 호환성 전략

- **현행 운영 경로**: `/tickets`, `/knowledge/search`, `/agent/analyze`, `/handovers` 등은 기존 DTO와 동작을 보존하는 방향으로 유지.
- **신규 교육 경로**: `/simulation-runs/{simulation_run_id}/...` 아래 기존 도메인 함수를 재사용한다. 아래 절에서 `{run_id}`는 항상 실습 실행 ID이며 분석 작업은 `analysis_id`로 구분한다.
- **충돌 회피**: 기존 공유 `/tickets/{id}`를 단순히 run_id 선택 없이 교육 계정에 노출하지 않는다.
- 공통 서비스에는 인증에서 파생한 실행 범위를 전달한다. 교육의 발신 보고서는 실행당 한 건이며 최종 확정에는 §4.9의 경로 하나를 사용한다. 기존 운영 `POST /handovers/{report_id}/confirm`은 운영 계약이다.
- 기존 worker 비동기 작업의 큐/lease 모델과 202+폴링 유지. worker의 tool allowlist도 실행 범위 상속.

## 3. API 목록 및 현행 재사용 매트릭스

### 3.1 재사용/수정/교육 비노출

| 분류 | 현재 코드에서 확인된 API | 교육 모드 계약 |
|---|---|---|
| REUSE | `POST /auth/login`, `GET /auth/session`, `POST /auth/logout` | 쿠키·CSRF 계약 보존 |
| REUSE | `GET /health`, `GET /api/v1/ready` | 루트 헬스 `{"status":"ok"}`와 내부 readiness 계약·프록시 공개 정책 유지 |
| MODIFY | `GET,POST /tickets`; `GET,PATCH,DELETE /tickets/{ticket_id}`; `POST /tickets/{ticket_id}/transitions` | 교육 컨텍스트 확인, 소유권·완료 읽기 전용, 기존 상태/버전 유지 |
| MODIFY | `GET,PUT /tickets/{ticket_id}/operations`; `POST /tickets/{ticket_id}/work`; `PUT /tickets/{ticket_id}/work/{work_id}` | 작업/담당·교대 정보가 동일 run 내에서만 사용되도록 적용 |
| MODIFY | `GET /sops`, `GET /sops/{sop_id}`, `GET /sops/{sop_id}/original`, `POST /knowledge/search` | 승인 공통 자료 + 현재 run만 검색; 인용 링크 검사 |
| MODIFY | `POST /agent/analyze`, `GET /agent/runs/{run_id}` | 기존 운영 DTO·202 폴링 유지. 교육 분석은 실행 범위를 큐·도구·근거·결과에 상속하고 분석 작업을 `analysis_id`로 식별 |
| MODIFY | `GET /shift-instances`, `GET /shift-instances/{shift_id}/handover-preview` | 교육 고정 슬롯을 읽는 뷰 제공 |
| MODIFY | `GET,POST /handovers`, `GET,PUT /handovers/{report_id}`, `GET /handovers/{report_id}/revisions/{version}`, `POST /handovers/{report_id}/confirm`, `POST /handovers/{report_id}/analyze` | 교육에서는 §3.2의 `outgoing-handover` 경로 사용. 리비전 읽기와 분석을 재사용하며 확정·종료는 `/outgoing-handover/finalize`로만 처리 |
| 교육 비노출 | SOP 등록·삭제·수동 재색인 / 공용 인계작업자 / 사람 ACK / 확정 후 정정 생성 | 기존 운영 데이터와 코드 삭제하지 않고 교육 계정 접근 차단 |

위 현행 API 정보는 제공된 Codex Gap Analysis 정적 조사 기준이다. 요청·응답 JSON 세부 계약은 실제 router/schema와 반드시 대조한다.

### 3.2 신규 교육 API(목표)

| Method | Path (모두 `/api/v1` 상대) | 설명 | 상태 제약 |
|---|---|---|---|
| POST | `/auth/register` | 계정 등록 | 비로그인·보안 검증 |
| GET | `/auth/registration` | 교육 가입 활성 여부 조회 | 공개, 캐시 금지 |
| GET | `/simulation` | 진행 중 회차 및 발행 시나리오의 키·버전·표시 제목 | 로그인 |
| GET | `/simulation-runs` | 내 실행 이력 목록 | 로그인 |
| POST | `/simulation-runs` | 새 실행 생성/중복 시작 방지 | ACTIVE 중복 생성 금지 |
| GET | `/simulation-runs/{run_id}` | 실행 상세·고정 시작 시각·현재 슬롯 | 소유자 |
| GET | `/simulation-runs/{run_id}/dashboard` | 홈 요약 | 소유자 |
| GET | `/simulation-runs/{run_id}/schedule` | 이전/현재/다음 슬롯·4조/2교대 표시 | 소유자 |
| GET | `/simulation-runs/{run_id}/incoming-handover` | 이전 Bot 인계 스냅샷 | 소유자 |
| POST | `/simulation-runs/{run_id}/incoming-handover/ack` | 선택 설계 후보인 인수 확인 행동 로그. 진행 조건에 포함하지 않음 | ACTIVE |
| GET | `/simulation-runs/{run_id}/hosts` | 호스트 목록 | 소유자 |
| GET | `/simulation-runs/{run_id}/tickets/{ticket_id}/host-incidents` | 대상 티켓의 호스트에서 발생 전 승인 이력 조회 | 소유자 |
| GET | `/simulation-runs/{run_id}/tickets/{ticket_id}/incident-sources/{incident_id}` | 승인 사고 출처 조회. 부모 티켓의 시나리오 범위·발생 전 조건 재검사 | 소유자 |
| GET | `/simulation-runs/{run_id}/tickets` | 현재 실행 티켓 목록, 보관 범위·커서 | 소유자 |
| POST | `/simulation-runs/{run_id}/tickets` | `open` 사용자 생성 티켓 등록. 상세 규칙 평가 범위에서는 제외 | ACTIVE |
| GET | `/simulation-runs/{run_id}/tickets/{ticket_id}` | 티켓 상세. SOP가 연결된 템플릿은 승인된 링크 목록 `related_sops`도 반환 | 소유자 |
| PATCH | `/simulation-runs/{run_id}/tickets/{ticket_id}` | 기존 필드·메모/버전 편집. 상태 전이는 전이 경로 사용 | ACTIVE |
| DELETE | `/simulation-runs/{run_id}/tickets/{ticket_id}` | `closed` 티켓 보관. 기존 버전·보관 의미 유지 | ACTIVE |
| POST | `/simulation-runs/{run_id}/tickets/{ticket_id}/transitions` | 기존 상태값·전이·해결 요약 제약 적용 | ACTIVE |
| GET, PUT | `/simulation-runs/{run_id}/tickets/{ticket_id}/operations` | 기존 배정·작업·감사 조회/업무 필드 편집 | GET 소유자, PUT ACTIVE |
| POST | `/simulation-runs/{run_id}/tickets/{ticket_id}/work` | 후속 작업 추가 | ACTIVE |
| PUT | `/simulation-runs/{run_id}/tickets/{ticket_id}/work/{work_id}` | 같은 실행·티켓의 후속 작업 수정 | ACTIVE |
| POST | `/simulation-runs/{run_id}/tickets/{ticket_id}/actions` | 시나리오 규칙 기반 가상 조치 | ACTIVE |
| GET | `/simulation-runs/{run_id}/tickets/{ticket_id}/actions` | 기존 ticket_work 포함 작업 이력 | 소유자 |
| POST | `/simulation-runs/{run_id}/tickets/{ticket_id}/analyses` | AI 장애 분석 요청(기존 agent 큐 내부 재사용) | ACTIVE·202 |
| GET | `/simulation-runs/{run_id}/analyses/{analysis_id}` | 소유 실행 AI 결과 폴링 | 소유자 |
| GET | `/simulation-runs/{run_id}/sops` | 승인 공통 SOP 목록 | 소유자 |
| GET | `/simulation-runs/{run_id}/sops/{sop_id}` | 승인 SOP 상세 | 소유자 |
| GET | `/simulation-runs/{run_id}/sops/{sop_id}/original` | 승인 SOP 원본 | 소유자 |
| POST | `/simulation-runs/{run_id}/knowledge/search` | 승인 SOP·범위 내 해결 티켓 RAG. 공통 과거 사고 검색에는 대상 티켓 필요 | 소유자 |
| GET | `/simulation-runs/{run_id}/outgoing-handover` | 초안·인계 후보·경고·리비전 또는 확정본·고정 경고 조회 | 소유자 |
| POST | `/simulation-runs/{run_id}/outgoing-handover/generate` | 기존 비동기 초안 기능 실행 범위에 맞게 재사용 | ACTIVE·202 가능 |
| PUT | `/simulation-runs/{run_id}/outgoing-handover` | 초안 저장·스냅샷 재수집·수동 후보 선택, 리비전 반환. 기본 후보는 미해결 티켓과 미완료 작업이 있는 티켓 | ACTIVE |
| GET | `/simulation-runs/{run_id}/outgoing-handover/revisions/{version}` | 동일 실행의 보고서 리비전 | 소유자 |
| POST | `/simulation-runs/{run_id}/outgoing-handover/finalize` | 유일한 교육 확정 경로. 확정+COMPLETED+평가 예약 | 최초 ACTIVE, 동일 요청 재전송 허용 |
| GET | `/simulation-runs/{run_id}/evaluation` | 규칙·설명 상태, 평가 대상 기준 종합 요약 | COMPLETED |
| GET | `/simulation-runs/{run_id}/evaluation/tickets` | 초기 실습·이월 티켓 평가 목록(무조치·보관 포함) | COMPLETED |
| GET | `/simulation-runs/{run_id}/evaluation/tickets/{ticket_id}` | 티켓 상세 평가/증거 | COMPLETED |
| POST | `/simulation-runs/{run_id}/tickets/{ticket_id}/learning-evidence` | 승인 학습 자료의 확인 또는 실제 조치에 연결된 활용을 기록 | ACTIVE, 소유자, CSRF |
| GET | `/simulation-runs/{run_id}/events` | 평가 근거용 사용자 행동 타임라인 | 소유자 |

**라우트 중복 최소화 권고:** 위 `simulation-runs/...` 경로는 목표로 하는 **외부 교육용 API 계약**이다. 구현 시 현재 ticket/agent/handover 서비스 함수를 호출할 것. 기존 코드 기능이 이미 만족한다면 새 비즈니스 로직을 별도로 구현하지 않는다.

## 4. 대표 API 계약

Phase 1은 현재 실습 요약, 실행 시작·상세, 고정 일정, 인수 보고서 스냅샷 및 초기 호스트·티켓의 소유자 읽기를 제공한다. 실행 상세의 `virtual_now`는 발행 버전의 고정 시작 시각이다. 근무 종료 시각은 실습 가능 여부에 영향을 주지 않는다. 교육 발신 인계 최종 확정만 실습을 종료한다. 행동 기록은 서버의 `occurred_at`과 실행별 `event_sequence`로 정렬한다. 평가는 소요 시간과 근무시간 준수 여부를 제외하며 종료 후 완료되는 분석은 분석 이력에만 저장한다.

Phase 1 시작 요청의 `Idempotency-Key`는 필수이며 공백 없이 1~128자의 출력 가능한 ASCII를 사용한다. 근무 일정과 고정 시나리오 시작 시각은 시나리오 시간대로 반환한다. `started_at`은 실행이 생성된 서버 시각이다. 초기 티켓 목록은 `items` 배열을 반환한다. 보관 필터와 커서는 교육 티켓 변경 기능과 함께 적용한다.


### 4.1 회원가입

`POST /api/v1/auth/register`

```json
{"username":"trainee01","password":"example-password"}
```

- 201 생성, 비밀번호/중복/요청 제한 및 Origin 정책 적용.
- 위 값은 문법 예시다. 로그인 식별자는 기존 `username`을 사용한다. 비밀번호 정책은 가입 구현 시 확정한다.
- 등록 후 기존 로그인 화면으로 진행한다. 공개 Compose 구성에서는 가입을 비활성화한다.
- `GET /api/v1/auth/registration`은 `{"enabled":false}` 또는 `{"enabled":true}`를 반환한다. 가입 안내 확인은 가입 쓰기 요청의 제한 버킷을 사용하지 않는다. 가입 POST의 Origin과 요청 제한은 유지한다.

### 4.2 현재 실습 요약

`GET /api/v1/simulation`

```json
{
  "status": "NOT_STARTED",
  "active_run_id": null,
  "available_scenarios": [{"scenario_key":"dco-basic","version":"1.0"}]
}
```

- ACTIVE 실행이 있으면 `status=ACTIVE`와 소유 실행 ID 표시; 과거 완료 기록만 있어도 현재 ACTIVE가 없다면 `NOT_STARTED`로 반환.

### 4.3 실행 시작

`POST /api/v1/simulation-runs` + `Idempotency-Key: <client-generated-key>`

```json
{"scenario_key":"dco-basic","scenario_version":"1.0"}
```

**201 예시**

```json
{
  "id": "a3b7f55c-45a0-4e5b-bd6b-82935a357e01",
  "status": "ACTIVE",
  "scenario": {"key":"dco-basic","version":"1.0"},
  "virtual_now": "2026-11-20T08:00:00+09:00",
  "shift": {
    "crew_code": "A", "type": "DAY",
    "starts_at": "2026-11-20T08:00:00+09:00",
    "ends_at": "2026-11-20T20:00:00+09:00"
  }
}
```

- 시작은 원자적 시드 복제. 동일 사용자 ACTIVE 복제 금지.
- 같은 멱등 키와 본문 재시도는 기존 실행을 200으로 반환한다. 같은 키에 다른 시나리오를 요청하거나 ACTIVE 중 다른 키로 새 시작을 요청하면 409다.
- 같은 시나리오 버전/초기 상태 재현 보장.

### 4.4 호스트 과거 사고 조회

`GET /api/v1/simulation-runs/{run_id}/tickets/{ticket_id}/host-incidents?limit=20`

```json
{
  "host": {"id":"host-uuid", "host_key":"ASSET-001", "hostname":"dc01-r12-u08"},
  "incidents": [
    {
      "incident_id":"737e9ea7-a6ea-4cec-8db0-19b05dab240e",
      "incident_key":"SYN-032",
      "ticket_key":"INC-HIST-000012",
      "occurred_at":"2026-10-20T09:30:00+09:00",
      "error_code":"MEM_ECC_ERROR",
      "summary":"메모리 ECC 오류",
      "resolution":"진단 후 메모리 모듈 교체",
      "source_type":"synthetic",
      "source_url":"/api/v1/simulation-runs/a3b7f55c-45a0-4e5b-bd6b-82935a357e01/tickets/c18fe0b7-0728-49bc-a5e3-c50703680405/incident-sources/737e9ea7-a6ea-4cec-8db0-19b05dab240e"
    }
  ]
}
```

- 예시는 가상 데이터다. 서버는 소유 실행의 대상 티켓에서 호스트·시나리오 버전·`detected_at`를 취한다. 승인된 합성 자료 중 같은 호스트이고 `occurred_at < ticket.detected_at`인 자료만 반환한다. 티켓 템플릿의 `initial_ticket.incident_key`와 같은 사고 키를 가진 자료는 조회·검색·출처 열기에서 제외한다.
- 호스트가 없는 유효 티켓의 응답은 `host: null`, `incidents: []`다. 출처 링크의 서버 조회에서도 시나리오·승인·발생 전 조건을 다시 적용한다. 호스트 목록 자체는 사고 이력을 반환하지 않는다. `ticket_key`는 합성 과거 티켓에 부여한 참조 키이며, 참조 키가 등록되지 않은 사고에는 `null`을 반환한다.
- 과거 장애 상세는 사건의 `error_code`와 당시 `resolution`, 발행 시나리오의 호스트명·호스트 키·자산 분류를 제공한다. 합성 서비스는 자산 분류에서 매핑하며 매핑이 없는 경우 `null`이다.
- `GET /simulation-runs/{run_id}/tickets/{ticket_id}/learning-evidence/actions?kind=HOST_HISTORY&source_id=...`는 자료 확인 여부(`viewed`), 활용 연결 여부(`linked`), 확인 후 대상 티켓에서 수행한 연결 가능한 가상 조치(`items`)를 반환한다. `kind=SOP`도 지원한다.

### 4.5 AI 분석 시작 및 폴링

`POST /api/v1/simulation-runs/{run_id}/tickets/{ticket_id}/analyses`

```json
{"question":"가능한 원인과 다음 점검 순서를 근거와 함께 알려줘"}
```

**202**

```json
{
  "analysis_id":"5b6e4e3d-42c0-44da-a8d1-a09669302912",
  "status":"QUEUED",
  "poll_url":"/api/v1/simulation-runs/a3b7f55c-45a0-4e5b-bd6b-82935a357e01/analyses/5b6e4e3d-42c0-44da-a8d1-a09669302912"
}
```

- 기존 `agent_runs` 큐/lease/인용 검증 재사용. 모델의 권고는 실제 티켓 작업을 변경하지 않는다.
- 완료 결과는 `causes`, `recommended_checks`, `references` 등 구조를 사용하되 기존 agent DTO에 맞춰 최종 필드 확정.
- 모든 도구 호출은 `run_id` 및 승인 공통 자료 범위 내에서 수행.

### 4.6 가상 조치 수행

`POST /api/v1/simulation-runs/{run_id}/tickets/{ticket_id}/actions`

```json
{
  "action_code":"RUN_BMC_DIAGNOSTICS",
  "parameters":{"target":"memory"},
  "note":"메모리 오류 진단"
}
```

**201 예시**

```json
{
  "action_id":"93fd54a3-e6aa-4480-81f0-9f8795722c09",
  "status":"COMPLETED",
  "result":{"error_code":"MEM_ECC_ERROR","message":"가상 진단에서 오류 확인"},
  "ticket_version":4,
  "occurred_at":"2026-11-20T00:15:00Z",
  "event_sequence":12
}
```

- `action_code`는 발행 시나리오의 허용된 조치만 장비/티켓 상태를 바꿀 수 있다.
- `CUSTOM_NOTE`처럼 자유 기록은 실제 조치·상태 전이를 자동 생성하지 않는다.
- 실제 인프라 제어 금지. 반복 호출 시 멱등성/버전 충돌 검증.

Phase 3에서는 `Idempotency-Key`가 필수다. 키의 범위는 실행이며 티켓과 본문을 변경해 재사용하면 409다. 같은 요청의 재전송은 최초 저장 응답을 200으로 반환한다. `expected_version`을 제공하면 현재 티켓 버전을 대조한다. 조치의 매개변수는 카탈로그와 일치해야 하며 선행 조건 미충족은 409다. `CUSTOM_NOTE`는 비어 있지 않은 메모만 받아 `kind: NOTE`, `status: RECORDED`, `result: null`로 반환한다. 수행한 가상 조치는 `kind: ACTION`이며 별도 결과를 저장한다. 자동 티켓 상태 전이는 없다.

같은 경로의 GET 응답은 기존 작업 목록 `items`와 시나리오 카탈로그 `catalog`, 조치·자유 메모의 저장 영수증 `activity`를 반환한다. `GET /simulation-runs/{run_id}/events?after_sequence=0&limit=50`은 실행 내 티켓 감사 이력을 순번 오름차순으로 반환한다. 현재 단계에서 인계 열람과 검색은 상태를 바꾸지 않는다.

### 4.7 인수 보고서 확인(선택적 기록)

`POST /api/v1/simulation-runs/{run_id}/incoming-handover/ack`

```json
{"acknowledged":true}
```

- 현재 실행에 `INCOMING_HANDOVER_VIEWED/ACKNOWLEDGED` 행동 로그를 기록. 미실행이라도 자유 실습 진입을 막지 않는다.

### 4.8 인계 보고서 초안 저장

`PUT /api/v1/simulation-runs/{run_id}/outgoing-handover`

```json
{
  "expected_revision":3,
  "content":{
    "summary":"주간 근무 중 메모리 장애 조치 및 미해결 건 인계",
    "open_items":[{"ticket_id":"ticket-uuid","next_action":"재진단 결과 확인"}]
  }
}
```

- `expected_revision`은 교육 계약의 편집 버전이며 기존 서비스의 보고서 버전에 매핑할 수 있다. 초안 저장·재수집은 리비전에 남긴다. 입력 형식·참조 검증 결과와 업무 경고를 구별한다.
- AI 생성 전에 `GET`으로 빈 유효 초안과 스냅샷·리비전을 읽을 수 있다. `PUT`의 `refresh_snapshot`은 실행 자료를 다시 수집하고, `selection`으로 같은 실행의 티켓을 수동 포함·제외할 수 있다. 저장·재수집 응답은 최신 리비전과 경고를 반환한다. AI 작업 완료를 기다리지 않고 초안을 재수집할 수 있어야 한다.
- `summary`·`open_items`의 구체 DTO는 현행 보고서 형식과 대조해 확정한다. 빈 업무 내용은 저장할 수 있고 값이 제공된 경우 타입과 참조 형식을 검증한다. 생성 초안의 완료 작업은 수행 기록으로 입증할 수 있어야 한다.
- 실습의 미완료 작업은 다음 근무조로 넘기며 개인 담당자는 인수 후 배정한다. 담당자가 비어 있는 미완료 작업에는 인계 경고를 생성하지 않는다.

### 4.9 인계 확정 = 실습 종료

`POST /api/v1/simulation-runs/{simulation_run_id}/outgoing-handover/finalize` + `Idempotency-Key`

```json
{"confirm":true,"expected_revision":4}
```

**200**

```json
{
  "run_id":"a3b7f55c-45a0-4e5b-bd6b-82935a357e01",
  "simulation_status":"COMPLETED",
  "report_status":"FINALIZED",
  "finalized_revision":5,
  "warnings":[{"code":"MISSING_NEXT_ACTION","ticket_id":"c18fe0b7-0728-49bc-a5e3-c50703680405"}],
  "evaluation":{"rules_status":"PENDING","explanation_status":"NOT_STARTED"}
}
```

- 소유권·인증·CSRF 확인 후 적용하는 보고서 검증의 차단 사유는 두 가지다. **구조적 오류**는 422다. 예시는 잘못된 요청 형식, 잘못된 항목 타입, 중복 참조, 허용 범위에서 확인할 수 없는 참조다. 포함된 무효 참조는 외부 리소스 존재 여부를 드러내지 않는 일반 오류로 처리한다.
- **버전 충돌**은 409다. `expected_revision`과 현재 초안의 불일치 또는 재수집이 필요한 원본 버전 변경을 검증한다. 경고 변화만으로 충돌을 만들지 않는다. 확정 성공은 새 최종 리비전을 만든다. 예시의 `finalized_revision`은 `expected_revision`보다 증가한 값이다.
- 빈 요약, 미해결 티켓의 인계 누락, 담당·다음 조치·재확인 미기록 등은 `warnings`로 반환한다. 확정본 조회에서 경고를 다시 읽을 수 있고, 상세 평가 대상에 해당하는 경고는 평가에 반영한다. 적절히 인계된 미해결 상태와 AI 초안의 처리 여부는 확정 조건에 영향을 주지 않는다.
- 확정 리비전과 경고, 실행 `COMPLETED`, 고정 평가 입력과 규칙 예약을 한 DB 트랜잭션에 기록한다. 동일 범위·동일 키·동일 요청은 저장된 성공 응답을 반환하고 같은 키에 다른 내용을 사용하면 409를 반환한다. 별개 요청이 이미 확정된 이전 초안 리비전으로 들어오면 버전 충돌이다. 두 처리 단계는 커밋 이후에 진행한다.
- `expected_revision: 0`은 초안이 없는 실행에서 허용한다. 서버가 초기 스냅샷과 확정 리비전을 같은 트랜잭션에서 저장한다. 재수집 시 모든 실행 티켓 버전을 저장한다. 버전 목록이 없는 Phase 3 초안은 포함된 스냅샷의 버전을 비교한다. 원본 버전 충돌은 `REPORT_SOURCE_CHANGED`, 보고서 리비전 충돌은 `VERSION_CONFLICT`, 동일 키의 다른 요청은 `IDEMPOTENCY_KEY_REUSED`다.

### 4.10 평가 상태/티켓별 피드백

`GET /api/v1/simulation-runs/{run_id}/evaluation`

```json
{
  "run_id":"a3b7f55c-45a0-4e5b-bd6b-82935a357e01",
  "rules_status":"READY",
  "explanation_status":"PROCESSING",
  "evaluated_ticket_counts":{"total":5,"resolved":3,"unresolved":2}
}
```

`GET /api/v1/simulation-runs/{run_id}/evaluation/tickets/{ticket_id}`

```json
{
  "ticket_id":"ticket-uuid",
  "ticket_key":"TKT-001",
  "activity_status":"ATTEMPTED",
  "training_origin":"CARRYOVER",
  "final_status":"resolved",
  "rules_status":"READY",
  "explanation_status":"READY",
  "rule_results":[
    {"rule_code":"CHECK_HOST_HISTORY","result":"PASS","evidence_refs":["event-101"]},
    {"rule_code":"POST_REPAIR_VALIDATION","result":"NOT_MET","evidence_refs":["action-203"]}
  ],
  "feedback":{
    "summary":"조치 후 검증 기록 보완 필요",
    "strengths":["과거 동일 호스트 이력을 확인함"],
    "improvements":["교체 후 진단 결과가 기록되지 않음"],
    "recommendations":["조치 후 재검진 로그와 시각을 남길 것"]
  }
}
```

- 규칙 상태는 `PENDING | PROCESSING | READY | FAILED`다. 설명은 `NOT_STARTED`에서 시작하고 규칙 결과가 저장된 후 `PENDING | PROCESSING | READY | FAILED`를 표시한다. 조회는 두 상태를 함께 반환한다. 규칙이 `READY`가 되기 전에는 규칙 결과를 `null`로 반환하고, 설명이 `READY`가 되기 전에는 `feedback`을 `null`로 반환한다. 설명 실패 중에도 준비된 규칙 결과와 근거는 반환한다.
- `INITIAL`·`CARRYOVER`는 무조치·인계 누락·보관 시에도 상세 평가 대상이다. 무조치는 `NOT_ATTEMPTED`로 표시한다. 사용자 생성 티켓은 작업·인계 기록을 보존하며 상세 평가 목록·집계에서 제외한다. 해당 사용자 생성 티켓의 상세 평가 조회는 `404 TICKET_EVALUATION_NOT_FOUND`다. 과거 사고는 참조 자료이며 평가 ID·대상 집계에 포함하지 않는다.
- `evaluated_ticket_counts`는 규칙 계산 진행과 관계없이 확정 시 고정한 필수 평가 대상을 집계한다. `resolved` 집계는 `resolved`·`closed`, `unresolved` 집계는 `open`·`in_progress`·`waiting`이다. 초기·이월 티켓 상세는 규칙 처리 전에도 두 상태와 결과 `null`을 반환할 수 있어야 한다. 판정은 `PASS`, `NOT_MET`, `NOT_OBSERVED`, `NOT_APPLICABLE`을 사용한다. 근거 링크는 소유·공개 범위를 재검증한다.
- 요약에 `evaluation_id`, `last_event_sequence`, `warnings`, 준비된 `rule_summary`(판정별 개수)를 제공한다. `GET .../evaluation/tickets`는 제목·출처·시도 및 최종 티켓 상태·처리 상태를 담은 `items`를 반환한다. 상세에는 확정 시 고정한 `timeline`(행동 ID, 순번, 서버 시각, 유형, 세부 정보) 및 티켓별 `warnings`, `evidence`(ID, 유형, 권한 검사 경로)가 포함된다. 자료 승인이 취소되면 해당 자료의 근거와 종속 설명은 조회에서 숨긴다. 확정본·규칙 원본은 보존한다.
- 설명만 `FAILED`인 평가를 재시도하는 작업은 내부 Worker CLI `python -m app.simulation.evaluation_worker --retry-explanation <evaluation_id>`에서 수행한다.

### 4.11 학습 자료 확인과 활용 기록

`POST /api/v1/simulation-runs/{run_id}/tickets/{ticket_id}/learning-evidence`

- `kind`는 `HOST_HISTORY | SOP`, `activity`는 `VIEWED | USED`, `source_id`는 자료의 UUID다. `USED`는 같은 실행과 티켓에서 `VIEWED` 후 수행한 가상 조치의 `action_event_id`가 필요하다. 승인 범위 밖 자료는 404, 자유 메모·다른 티켓의 조치·시간 순서 오류는 422다.
- 최초 등록은 201, 동일한 기록의 재전송은 저장된 이벤트와 200을 반환한다. 같은 `USED` 기록을 다른 조치에 다시 연결하려면 409가 반환된다. 응답은 이벤트 `id`, `event_sequence`, `activity`를 제공한다. 자료 유형과 서버 시각은 실행 타임라인에서 조회한다. `GET` 요청과 AI 권고는 학습 행동을 생성하지 않는다. 완료 실행의 신규 기록은 409다.

## 5. 검색/RAG/링크 보안 계약

1. 검색 요청에는 인증 user에서 파생된 run scope를 서버가 강제한다. **클라이언트의 임의 run_id만 신뢰 금지**.
2. 후보는 승인 공통 SOP, 승인된 시나리오 합성 사고, 현재 실행의 자격 있는 해결 티켓으로 제한한다. 현재 색인 버전과 임베딩 프로필을 일치시키고 보관 티켓을 검색 후보에서 제외한다.
3. 제출한 `ticket_id`는 실행 소유권과 자식 관계를 검증한다. 공통 사고 후보에는 대상 티켓의 시나리오 범위와 `occurred_at < ticket.detected_at`를 적용하고 동일 호스트 이력에는 호스트를 일치시킨다. 일반 검색에서 티켓을 생략하면 공통 사고 후보는 제외한다.
4. 범위·승인·시점 필터는 `LIMIT/top-K` 전에 적용한다. worker는 큐에 연결된 요청자·실행·부모 티켓과 보고서 리비전으로 범위를 재확인하고 모든 도구의 자동/수동 후보에 전달한다.
5. 인용·직접 URL·SOP 원본 다운로드·평가 근거는 원본 접근 권한을 요구한다. 호스트 사고 출처는 현재 실행과 대상 티켓을 함께 확인한다. 다른 계정·같은 계정의 다른 실행·레거시 운영 자료를 교차 참조할 수 없다.
6. 완료 뒤 보존된 결과·출처는 읽을 수 있다. 읽기 성격의 검색이 원본과 평가 입력에 새 활동을 기록하지 않도록 하며, 새 분석·조치·수정은 금지한다.

## 6. 비동기 작업·재시도 계약

- AI 장애분석: 기존 202+폴링/lease 모델 재사용. 분석 실패가 티켓 상태를 바꾸지 않음.
- 규칙과 설명은 별도의 처리 상태를 갖는다. 확정 시 규칙을 예약하고, 규칙 결과의 `READY` 발행과 설명의 `PENDING` 예약을 함께 처리한다. 규칙 실패 동안 설명은 `NOT_STARTED`다. AI 설명 실패는 설명 상태에만 기록한다.
- worker 중단·lease 만료 시 해당 단계만 재수거한다. 단계별 시도 횟수와 백오프를 추적하고 한도를 넘으면 해당 단계의 `FAILED`를 기록한다. 유일 제약과 조건부 발행으로 중복 결과를 막는다.
- 사용자 직접 재시도·재채점 API의 공개, 권한과 횟수는 후속 정책 결정이다. 내부 설명 재시도는 저장된 규칙 결과를 재사용한다.

## 7. API 통합 및 테스트 필수 조건

| ID | 테스트 | 기대 결과 |
|---|---|---|
| API-01 | 사용자 A/B 같은 시나리오 시작 | 동일 초기 내용, 서로 다른 run/ticket UUID |
| API-02 | A의 run, ticket, report, agent 결과를 B가 직접 조회 | 일관된 비소유 404 |
| API-03 | A/B 검색과 agent tool 실행 | 상대 사용자 티켓/인용/원본 노출 없음 |
| API-04 | A가 해결한 티켓을 B가 조회 | B는 초기 상태 유지 |
| API-05 | 한 사용자가 동시 POST 시작 | ACTIVE 실행 최대 1, 부분 복제 없음 |
| API-06 | 완료 후 티켓 수정·분석 시작·보고서 수정 | 409, 원본 변경 없음 |
| API-07 | 구조 오류·버전 충돌·확정과 티켓 변경 경합 | 각각 422·409 적용, 버전 일치 시 원자적 스냅샷 저장, 경고 변화만으로는 충돌 거부 없음 |
| API-08 | 단일 확정 경로, 동일 요청 재전송과 별개 경합 | 동일 요청에 저장 응답, 별개 구버전 요청에 409, 평가 레코드 1개 |
| API-09 | 인계 요약·미해결 후속 조치·업무 필드 누락과 AI 초안 대기 | 경고를 남겨 정상 완료하고 평가에 반영 |
| API-10 | 규칙과 설명 조회, AI 설명 생성 실패/worker 중단 | COMPLETED 유지. 규칙 `READY` 및 설명 `FAILED` 동시 표현, worker 단계별 재수거 |
| API-11 | 과거 이력 질의·RAG·출처 링크 | 동일 호스트 이력 및 승인·발생 전 조건. 동일 시각·미래·미승인·교차 실행 자료 제외 |
| API-12 | 기존 쿠키/CSRF/202 agent DTO 회귀 | 기존 클라이언트 계약 보존 |
| API-13 | 새로고침/직접 URL 접근 | 본인 실행만 올바르게 복원 |
| API-14 | 레거시 운영 행/라우트 | 교육 사용자에 비노출, 기존 운영 데이터 미파괴 |
| API-15 | 초기·이월·사용자 생성·과거 사고 혼합 | 초기·이월만 상세 평가와 집계. 무조치·누락·보관 포함, 사용자 생성 작업·인계 보존 |
| API-16 | 야간 경계와 시나리오 재실행 | 고정 이전·현재·다음 3슬롯과 Bot 링크·시드 내용 일치 |

## 8. 구현 전 확인 항목

- [ ] 실제 DTO 및 기존 API 경로 확인 후 **변경 충돌 표** 제시
- [ ] 기존 사용자명·세션에 연결할 가입 공개 정책과 요청 제한 확정
- [ ] 기존 티켓 상태·작업·전이 및 운영 DTO 호환 검증
- [ ] 분석 작업과 실습 실행의 ID 소유권 검사 및 실행 범위 전달 구조 결정
- [ ] 교육 `FINALIZED` 내부 매핑, 구조 검증·경고 코드, 원본 버전 비교 단위를 확정
- [ ] RAG의 top-K 전 실행 범위와 권한이 SQL에서 검증되는지 확인
- [ ] 평가 worker·레코드 원자성·lease 재사용 가능성 검증
- [ ] P0 API 계약과 테스트가 확정되기 전 실제 운영 DB 수정 금지

제품 요구와 미결정 사항은 통합 PRD 최종 후보에서 관리한다. [통합 PRD 최종 후보](PRD-merged-draft.md)

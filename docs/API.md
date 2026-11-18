# REST API 계약

설계 초안 · 2026-11-18 (KST)

## 공통 계약

- 업무 경로는 `/api/v1` 아래에 둔다. 아래 표는 접두사를 생략했다. 기존 `GET /health`는 그대로 200과 `{"status":"ok"}`를 반환한다.
- UUID와 RFC 3339 시간(오프셋 필수), JSON을 사용한다. `PATCH`에서 빠진 필드는 유지하고 명시적 `null`은 nullable 필드만 지운다. 입력의 알려지지 않은 필드는 거부한다.
- 목록 응답은 `{items, next_cursor}`를 사용한다. `limit` 기본 25, 최대 100. 커서는 필터와 정렬을 포함한 불투명 값이다. 티켓 목록은 `updated_at DESC, id DESC`, 이력은 버전 역순, 다른 목록은 생성 시각 및 ID 역순을 기본으로 한다.
- 오류는 `{error:{code,message,details,request_id}}`로 통일한다. 401 세션 없음·만료, 403 권한·CSRF 실패, 404 누락 또는 조회할 수 없는 리소스, 409 버전·전이·인계 자료 충돌, 422 잘못된 입력, 429 속도 제한, 503 DB 또는 작업 인프라 불가를 나타낸다. 모든 응답에 `X-Request-ID`를 포함한다. [FastAPI 예외 처리](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- 로그인 성공 시 세션 쿠키와 CSRF 값을 응답한다. 같은 출처의 `GET /auth/session`은 새로 고침 후 CSRF 값을 돌려준다. 로그인은 `Origin`을 검사하고 인증 이후 쓰기 요청은 `Origin`과 `X-CSRF-Token`을 검사한다. 개발 기본 환경은 루프백 HTTP, 운영 환경은 TLS 쿠키다.
- 티켓과 보고서 변경은 `expected_version`을 받는다. 상태 변경·배정·메모·작업은 동일 티켓 버전을 공유한다. `DELETE`는 `expected_version` 쿼리 인자를 받는다. 버전이 변경되면 409 `VERSION_CONFLICT`와 현재 버전을 반환한다.
- AI 실행 생성 및 인수인계 초기 생성·갱신은 사용자·종류별 `Idempotency-Key`(UUID)를 받는다. 같은 대상과 본문을 재전송하면 이전 실행 ID와 응답을 반환한다. 같은 키를 다른 대상이나 본문에 쓰면 409다. 티켓 분석은 동일 대상·입력 버전에 대기 또는 실행 중인 작업이 있으면 409와 해당 실행 ID를 반환한다. 인수인계는 보고서 버전으로 중복 갱신을 제어한다. 새 실행 요청은 새 키를 사용한다.

## 인증과 교대 조회

| 메서드와 경로 | 요청·응답 | 권한 |
| --- | --- | --- |
| `POST /auth/login` | `username, password` → 200 사용자, 역할·소속, CSRF 값과 쿠키 | 익명 |
| `GET /auth/session` | 200 사용자, 역할·소속·CSRF 값 | 인증 사용자 |
| `POST /auth/logout` | 204 세션 폐기 및 쿠키 제거 | 인증 사용자 |
| `GET /shift-instances` | `from, to, team_id` 필터, 200 회차·조·다음 회차 ID. 필터 생략 시 현재·직전·다음 회차 반환 | 인증 사용자 |
| `GET /shift-instances/{id}/members` | 200 활성 조원, 표시 이름, 역할 | 인증 사용자 |
| `GET /shift-instances/{id}/handoff-preview` | 200 현재/직전 인계 회차의 대상 티켓·작업·누락 정보, 이월 표시, 인수 회차. 직후 회차가 없으면 409 `HANDOFF_SHIFT_MISSING` | 인증 사용자 |
| `GET /ready` | 200 DB·스키마 준비, 503 의존성 미준비 | 내부 운영 점검 |

## 티켓과 후속 작업

| 메서드와 경로 | 요청·응답 |
| --- | --- |
| `POST /tickets` | 필수 `title, description, service, severity, detected_at`, 선택 `impact, shift_instance_id, assignee_id, next_action` → 201 티켓 |
| `GET /tickets` | `q, status, severity, shift_instance_id, assignee_id, scope` 및 커서. 미배정 회차는 `shift_instance_id=unassigned`로 지정한다. `scope=active\|archived\|all`이며 `active`는 비보관 전체(종료 포함) → 200 목록 |
| `GET /tickets/{id}` | 200 필드, 버전, 허용 전이, 활성 작업·누락 개수. 보관 티켓은 읽기 전용이다. |
| `PATCH /tickets/{id}` | 버전 + 편집 필드 및 배정 → 200 티켓. 상태, 해결 필드, 보관 필드는 전용 경로에서 처리한다. |
| `POST /tickets/{id}/transitions` | `expected_version, to_status, reason`, 해결 시 `resolution` → 200 티켓 |
| `DELETE /tickets/{id}` | `closed` 및 미완료 작업 없음 확인 후 보관 → 204. 보관 완료된 티켓의 재시도는 204. |
| `GET /tickets/{id}/events` | 200 작성자·시각·이벤트 유형·허용 필드의 변경 내용 |
| `POST /tickets/{id}/notes` | `expected_version, body` → 201 이벤트와 새 티켓 버전 |
| `GET /tickets/{id}/tasks` | 200 작업 목록과 티켓 버전 |
| `POST /tickets/{id}/tasks` | `expected_version, title` 및 선택 `assignee_id, due_at, note` → 201 초기 `todo` 작업과 새 티켓 버전 |
| `PATCH /tickets/{id}/tasks/{task_id}` | `expected_version` 및 작업 필드·상태 → 200 작업과 새 티켓 버전 |

모든 티켓 경로는 인증이 필요하다. 단일 조직에서 운영자와 책임자는 티켓을 조회·편집할 수 있다. 입력 한도 초깃값은 제목 160자, 서비스 80자, 설명 10,000자, 메모·해결·다음 조치 4,000자, 검색어 200자이다. 빈 값은 트림 후 검사한다. 티켓과 작업 담당자는 활성 사용자여야 하며, 티켓 담당자는 지정 회차의 소속 검사를 받는다. 후속 작업은 `todo`, `in_progress`, `blocked` 사이를 오갈 수 있고 `done`이나 `cancelled` 전환 시 결과·사유 메모가 필요하다. 완료 또는 취소 후 후속 작업은 새 작업으로 등록한다.

### 티켓 상태 전이

| 현재 | 다음 | 조건 |
| --- | --- | --- |
| `open` | `in_progress`, `waiting`, `resolved` | 해결이면 해결 요약 필요 |
| `in_progress` | `waiting`, `resolved` | 해결이면 해결 요약 필요 |
| `waiting` | `in_progress`, `resolved` | 해결이면 해결 요약 필요 |
| `resolved` | `closed`, `in_progress` | 종료는 미완료 작업이 없어야 한다. 재개는 사유를 기록하고 현재 해결 필드를 초기화한다. |
| `closed` | `in_progress` | 재개 사유를 기록하고 현재 해결 필드를 초기화한다. 보관된 티켓은 재개할 수 없다. |

모든 전이에 사유를 기록한다. `resolved`에서 미완료 작업을 추적할 수 있다. `closed`에서는 새 작업 생성 또는 활성화가 금지된다. `recheck_at`은 대기 상태 이후 보완할 수 있다.

## SOP, AI와 인수인계

| 메서드와 경로 | 요청·응답 | 권한·조건 |
| --- | --- | --- |
| `GET /sops` | `q` 및 커서 → 200 현재 발행 SOP 목록 | 인증 사용자 |
| `GET /sops/{id}` | 선택 `version` → 200 발행 버전과 절별 본문·키 | 인증 사용자, 이전 인용 열람 지원 |
| `POST /tickets/{id}/analyses` | `expected_version` + 멱등 키 → 202 `{run_id,status}`와 `Location: /api/v1/ai-runs/{run_id}` | 인증 사용자, 비보관 티켓 |
| `GET /tickets/{id}/analyses` | 200 해당 티켓 분석 실행 목록 | 인증 사용자 |
| `GET /ai-runs/{id}` | 200 종류, 상태, 대상·입력 버전, 결과·근거 또는 오류, 입력 변경 표시, 타임스탬프 | 실행 대상 조회 권한 필요 |
| `GET /ai-runs/{id}/tool-calls` | 200 호출명·순번·상태·시간·참조 ID·마스킹 인자 요약 | 실행 대상 조회 권한 필요 |
| `GET /handoffs` | 회차·상태 필터와 커서 → 200 목록 | 인증 사용자 |
| `POST /handoffs` | `outgoing_shift_id`와 멱등 키 → 202 `{handoff_id,version,run_id,status}`, AI 실행 Location | 인계조 소속. 수동 편집 가능한 초기 초안·스냅샷과 AI 실행을 함께 기록한다. 이미 생성된 보고서에 다른 요청을 하면 409와 기존 ID를 반환한다. |
| `GET /handoffs/{id}` | 200 최신 리비전, 스냅샷, 본문·확인 항목, 최근 실행 ID, 승인 정보 | 인증 사용자 |
| `PATCH /handoffs/{id}` | 버전, 편집 본문, 누락 정보별 확인 사유 → 200 보고서 | 인계조 소속, 초안만 |
| `POST /handoffs/{id}/regenerate` | 버전, `confirm_replace=true`, 멱등 키 → 202 실행 ID와 갱신한 보고서 버전 | 인계조 소속, 초안만. 기존 리비전을 보존하고 새 스냅샷과 수동 편집용 기본 내용으로 갱신한다. |
| `POST /handoffs/{id}/approve` | `expected_version` → 200 승인된 보고서 | 인계조 책임자. 스냅샷이 현재와 다르면 409 `HANDOFF_STALE`. 미확인 누락 정보 또는 인계 요약 미입력은 422. |

초안 생성·편집·갱신·승인은 현재 또는 직전 인계 회차에 적용한다. 해당 범위를 지난 미승인 보고서는 읽기 전용이며 변경 요청에는 409 `HANDOFF_SHIFT_EXPIRED`를 반환한다.

AI 실행 조회는 업무 응답 200 안에 `queued`, `running`, `succeeded`, `failed`를 표현한다. 실패 결과는 `error_code`, `retryable`, 안전한 사용자 메시지를 포함한다. 보고서 실행 결과는 해당 초안 버전이 그대로일 때만 적용한다. 사용자가 수정 또는 승인한 경우 생성 결과를 실행 상세에서 조회할 수 있다. 승인에 필요한 내용은 인계 요약과 스냅샷의 누락 ID 각각에 대한 확인 사유다. AI 실행과 승인 가능 여부는 독립적이다. 상세 결과 형식은 [에이전트 설계](AGENT.md)를 따른다.

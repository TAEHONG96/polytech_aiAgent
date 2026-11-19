# REST API 계약

설계 초안 · 2026-11-18 (KST)

## 공통 계약

- 업무 경로는 `/api/v1` 아래에 둔다. 아래 표는 접두사를 생략했다. 기존 `GET /health`는 그대로 200과 `{"status":"ok"}`를 반환한다.
- UUID와 RFC 3339 시간(오프셋 필수), JSON을 사용한다. `PATCH`에서 빠진 필드는 유지하고 명시적 `null`은 nullable 필드만 지운다. 입력의 알려지지 않은 필드는 거부한다.
- 티켓·SOP 목록은 `{items, next_cursor}`를 사용한다. 인수인계 목록은 `{items, next_before}`를 사용한다. `limit`은 기본 25, 최대 100이다. 티켓 목록은 `updated_at DESC, id DESC`, 인수인계 목록은 `created_at DESC, id DESC`로 정렬한다.
- FastAPI의 오류는 `{error:{code,message,details,request_id}}`를 사용한다. 401 세션 없음·만료, 403 권한·CSRF 실패, 404 누락 또는 조회할 수 없는 리소스, 409 버전·전이·인계 자료 충돌, 422 잘못된 입력, 429 속도 제한, 503 DB 또는 작업 인프라 불가를 나타낸다. FastAPI 응답과 Nginx의 413 `REQUEST_TOO_LARGE` 및 로그인 429 `SIGNIN_RATE_LIMIT`에 `X-Request-ID`를 포함한다. [FastAPI 예외 처리](https://fastapi.tiangolo.com/tutorial/handling-errors/)
- 로그인 성공 시 세션 쿠키와 CSRF 값을 응답한다. 같은 출처의 `GET /auth/session`은 새로 고침 후 CSRF 값을 돌려준다. 로그인은 `Origin`을 검사하고 인증 이후 쓰기 요청은 `Origin`과 `X-CSRF-Token`을 검사한다. 개발 기본 환경은 루프백 HTTP, 운영 환경은 TLS 쿠키다.
- 티켓과 보고서 변경은 `expected_version`을 받는다. 상태 변경·배정·메모·작업은 동일 티켓 버전을 공유한다. `DELETE`는 `expected_version` 쿼리 인자를 받는다. 버전이 변경되면 409 `VERSION_CONFLICT`와 현재 버전을 반환한다.
- 장애 분석과 인수인계 분석은 사용자당 진행 중인 실행 한 건을 공유한다. 인수인계 분석은 보고서당 진행 중인 실행도 한 건으로 제한한다.

## 인증과 교대 조회

| 메서드와 경로 | 요청·응답 | 권한 |
| --- | --- | --- |
| `POST /auth/login` | `username, password` → 200 사용자, 역할·소속, CSRF 값과 쿠키 | 익명 |
| `GET /auth/session` | 200 사용자, 역할·소속·CSRF 값 | 인증 사용자 |
| `POST /auth/logout` | 204 세션 폐기 및 쿠키 제거 | 인증 사용자 |
| `GET /shift-instances` | 200 `{items}`, 최근 100개 회차의 조·시작·종료·IANA 시간대 및 요청자의 `is_member, is_lead` | 인증 사용자 |
| `GET /handover-operators` | 200 `{items}`, 활성 운영자의 ID, 표시 이름, 소속 조 ID | 인증 사용자 |
| `GET /shift-instances/{id}/handover-preview` | 200 자동 선정 티켓·작업·누락 정보, 인계·인수 회차 및 캡처 시각. 적합한 직후 회차가 없으면 409 `INVALID_INCOMING_SHIFT` | 인증 사용자 |
| `GET /ready` | 200 DB·스키마 준비, 503 의존성 미준비 | 내부 운영 점검 |

인수인계 권한은 활성 세션의 사용자와 DB에 등록된 근무조 소속으로 판단한다. 화면은 회차별 `is_member, is_lead` 및 보고서 상세의 `permissions`를 사용한다.
현재 로그인·세션 응답의 `roles`와 `memberships`는 각각 `["operator"]`와 `[]`를 반환한다.

## 티켓과 후속 작업

| 메서드와 경로 | 요청·응답 |
| --- | --- |
| `POST /tickets` | 필수 `title, description, service, severity, detected_at`, 선택 `impact, next_action` → 201 티켓 |
| `GET /tickets` | `q, status, severity, scope` 및 커서. `scope=active\|archived\|all`이며 `active`는 비보관 전체(종료 포함) → 200 목록 |
| `GET /tickets/{id}` | 200 티켓 필드, 버전, 허용 전이. 보관 티켓은 읽기 전용이다. |
| `PATCH /tickets/{id}` | 버전 + 편집 필드 → 200 티켓. 담당 회차와 운영 정보는 `/operations`에서 관리한다. |
| `POST /tickets/{id}/transitions` | `expected_version, to_status, reason`, 해결 시 `resolution` → 200 티켓 |
| `DELETE /tickets/{id}` | `closed` 및 미완료 작업 없음 확인 후 보관 → 204. 보관 완료된 티켓의 재시도는 204. |
| `GET /tickets/{id}/operations` | 200 담당 회차, 담당자, 주의사항, 승인 필요 여부, 작업, 변경 이력 및 버전 |
| `PUT /tickets/{id}/operations` | `expected_version, shift_instance_id, owner_id, warnings, approval_required` → 200 교대 업무 정보. 생략·`null`은 미지정 |
| `POST /tickets/{id}/work` | `expected_version, title, status` 및 선택 `owner_id, due_at, note` → 201 갱신된 교대 업무 정보 |
| `PUT /tickets/{id}/work/{work_id}` | 등록과 동일한 필드 → 200 갱신된 교대 업무 정보 |

모든 티켓 경로는 인증이 필요하다. 단일 조직에서 운영자와 책임자는 티켓을 조회·편집할 수 있다. 입력 한도는 제목 160자, 서비스 80자, 설명 10,000자, 메모·해결·다음 조치 4,000자, 검색어 200자다. 티켓과 작업 담당자는 활성 사용자여야 하며, 티켓 담당자는 지정 회차의 소속 검사를 받는다. 작업 상태는 `todo`, `in_progress`, `blocked`, `done`이다. 승인 필요 여부는 `true`, `false`, 미확인 `null`이다.

### 티켓 상태 전이

| 현재 | 다음 | 조건 |
| --- | --- | --- |
| `open` | `in_progress`, `waiting`, `resolved` | 해결이면 해결 요약 필요 |
| `in_progress` | `waiting`, `resolved` | 해결이면 해결 요약 필요 |
| `waiting` | `in_progress`, `resolved` | 해결이면 해결 요약 필요 |
| `resolved` | `closed`, `in_progress` | 종료는 미완료 작업이 없어야 한다. 재개는 사유를 기록하고 현재 해결 필드를 초기화한다. |
| `closed` | `in_progress` | 재개 사유를 기록하고 현재 해결 필드를 초기화한다. 보관된 티켓은 재개할 수 없다. |

모든 전이에 사유를 기록한다. `resolved`에서도 미완료 작업을 추적한다. `closed` 티켓의 작업은 `done` 상태로만 기록할 수 있다. `recheck_at`은 티켓 수정 API에서 보완할 수 있다.

## SOP와 지식 검색

로그인 쿠키를 사용한다. 업로드, 삭제, 재색인, 검색 POST에는 `Origin`과 `X-CSRF-Token`도 전송한다.

| 메서드와 경로 | 요청과 성공 응답 |
| --- | --- |
| `POST /sops` | multipart `file`, 선택 `title` → 201과 문서·초기 `PENDING` 상태, `Location` |
| `GET /sops` | `limit` 1–100, 선택 `cursor` → 200 목록과 `next_cursor` |
| `GET /sops/{id}` | 200 문서 제목·파일명·크기·상태·오류·추출 텍스트·원본 링크 |
| `GET /sops/{id}/original` | 200 원본 파일을 첨부 다운로드 |
| `DELETE /sops/{id}` | 204. 작업·벡터와 원본을 정리 |
| `POST /sops/{id}/reindex` | 202 `{source_id,status}`. 대기 또는 실행 중인 작업은 이어서 진행 |
| `POST /knowledge/tickets/{id}/reindex` | 202 `{source_id,status}`. 해결 요약이 있는 미보관 `resolved`·`closed` 티켓 대상 |
| `POST /knowledge/search` | 질의와 필터 → 200 `{results,embedding_profile}` |

업로드는 텍스트 PDF와 UTF-8 TXT·Markdown을 받는다. **multipart 전체가 최대 8 MiB**다. 같은 파일 내용은 409 `SOP_DUPLICATE`와 기존 UUID를 반환한다. 지원하지 않는 파일은 415, 크기 초과는 413이다. 추출 또는 임베딩 실패는 SOP 상세의 `FAILED`·`error_code`·`error_message`로 확인한다. 일시적인 공급자 오류는 최초 시도를 포함해 최대 3회 처리한다.

### 검색 요청 및 응답

```json
{
  "query": "서버 부팅 실패와 메인보드 오류",
  "source_types": ["SOP", "TICKET"],
  "limit": 5
}
```

질의는 1–1,000자, `limit`은 1–20이다. 기본값은 두 출처 유형과 5건이다. 결과의 `source_id`는 원본 UUID다. `source_url`은 SOP 상세 또는 티켓 상세 웹 경로다. PDF는 `page_number`, Markdown은 `section`, 티켓은 `ticket_version`을 제공한다. `chunk_index`는 0부터 센다.

각 항목은 `source_type`, `source_id`, `title`, `content`, `score`, `chunk_index`, `page_number`, `section`, `ticket_version`, `source_url`을 반환한다. 점수는 `1 - cosine_distance`이다. 같은 임베딩 프로필에서 공급자가 색인한 청크를 정확 코사인 거리로 비교하고, 원본별 최고 순위 청크 하나를 반환한다. 점수가 높을수록 두 벡터가 가깝다. 현재 Gemini 검색 프로필에는 가상 자료 평가에서 검증한 최소 점수 0.62를 적용한다. 조건을 충족하는 출처가 없으면 빈 배열이다. worker 또는 임베딩 오류는 503, Gemini 호출 한도는 429를 반환한다.

## 장애 분석

| 메서드와 경로 | 응답 | 인증 |
| --- | --- | --- |
| `POST /agent/analyze` | 202와 `run_id`, `status` 및 `Location: /api/v1/agent/runs/{id}` | 세션·Origin·CSRF |
| `GET /agent/runs/{id}` | 200과 상태, 결과·출처 또는 오류, 도구 호출 기록과 사용량 | 요청자 세션 |

```json
{"message":"서버 부팅 실패 원인을 분석해줘","ticket_id":"48bdae70-1792-5c1b-91d1-97facd4120b6"}
```

`message`는 1–2,000자다. `ticket_id`는 선택적 UUID이며 실제 생성 티켓의 ID를 사용한다. 잘못된 형식은 422, 존재하지 않는 티켓이나 본인 소유가 아닌 실행은 404다. 진행 중인 요청은 `AGENT_BUSY`와 `details.run_id`를 반환한다.

조회 응답의 `status`는 `queued`, `running`, `succeeded`, `failed` 중 하나다. `result`는 `summary`, `confirmed_facts`, `hypotheses`, `diagnostic_steps`, `additional_information`, `limitations`, `evidence`를 포함한다. `tool_calls`에는 순서, 도구명, 상태, 검증된 요약, 근거 키와 소요 시간이 표시된다. 실패는 `error.code`, `error.message`, `error.retryable`로 전달된다. [도구와 응답 검증](AGENT.md)

## 인수인계

| 메서드와 경로 | 요청·응답 | 권한·조건 |
| --- | --- | --- |
| `GET /handovers` | 선택 `outgoing_shift_id, status, before, limit` → 200 `{items, next_before}` | 인증 사용자 |
| `POST /handovers` | `outgoing_shift_id`, 선택 `incoming_shift_id, include_ticket_ids, exclude_ticket_ids` → 201 `DRAFT` 보고서 및 `Location` | 시작한 회차의 인계조 구성원 |
| `GET /handovers/{id}` | 200 현재 본문·집계·스냅샷·확인 항목, 최신 분석 결과·도구 호출·사용량, 이력 및 요청자 권한 | 인증 사용자 |
| `POST /handovers/{id}/analyze` | `expected_version` → 202 실행과 보고서 `Location`. 상세의 `analysis`를 폴링 | 인계조 구성원, `DRAFT` |
| `PUT /handovers/{id}` | `expected_version`, 선택 `body, selection, refresh_snapshot, status` → 200 보고서 | 인계조 구성원, `DRAFT` 또는 `READY` |
| `POST /handovers/{id}/confirm` | `expected_version` → 200 `CONFIRMED` | 인계조 책임자, `READY` |
| `POST /handovers/{id}/acknowledge` | `expected_version` → 200 `ACKNOWLEDGED` | 인수조 구성원, `CONFIRMED` |
| `POST /handovers/{id}/revisions` | `expected_version` → 201 원본 확정본을 참조하는 새 `DRAFT` 및 `Location` | 인계조 구성원, `CONFIRMED` 또는 `ACKNOWLEDGED`, 후속 버전 미생성 상태 |
| `GET /handovers/{id}/revisions/{version}` | 200 해당 리비전의 본문과 스냅샷 | 인증 사용자 |

편집의 `body`는 인계 `summary`, `gap_responses: [{gap_id, answer}]`, `item_notes: [{ticket_id, summary, followup, warnings}]`로 구성한다. `body`를 보내면 본문 전체를 대체한다. `status`는 기본 `DRAFT`다. `READY`에는 요약과 모든 누락 항목의 답변 또는 미확인 사유가 필요하다. 조건을 충족하지 못하면 422 `REPORT_INCOMPLETE`를 반환한다. `selection`을 보내거나 `refresh_snapshot=true`면 현재 자료로 다시 수집한다. 변경된 원본의 확인 답변은 재검토 대상으로 돌아간다.

수동 포함·제외 목록은 각각 UUID 100개까지 입력한다. 같은 ID를 두 번 지정하면 422, 존재하지 않는 티켓 또는 회차는 404를 반환한다. 새 보고서가 이미 있으면 409 `HANDOVER_EXISTS`와 `details.handover_id`를 반환한다. 최종 확정에서 대상이나 원본 기록이 달라졌으면 409 `HANDOVER_STALE`을 반환한다. 자료를 갱신하고 준비 상태로 저장한 뒤 다시 확정한다.

분석 결과는 수집 리비전에 연결한다. AI 호출이 실패한 경우에도 수집된 원본으로 본문을 편집·확정할 수 있다. [도구와 결과 형식](AGENT.md#4단계-인수인계-연동)

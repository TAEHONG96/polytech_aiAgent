# 데이터센터 장애 대응 교육 시뮬레이터 Gap Analysis

## 1. Executive Summary

기존 티켓 상태 전이와 감사 이력, SOP 임베딩·검색, Gemini 근거 기반 분석, 인계 스냅샷과 리비전을 재사용할 수 있다. 전환의 선행 조건은 **simulation_run_id에 의한 API·worker·검색·근거 링크 전체 격리**다. 기존 인증과 교대 권한은 공유 티켓·보고서 및 검색 데이터의 사용자별 경계를 제공하지 않는다. 근거는 `app/tickets.py:174 ticket_by_id`, `app/handover/routes.py:72 list_reports`, `app/knowledge/search.py:50 search_index`.

발신 보고서 확정, 실습 완료, 평가 작업 예약을 한 DB 트랜잭션에서 처리한다. 규칙 판정은 서버가 담당하고 Gemini는 판정의 자연어 설명을 생성한다. 현재 `app/agent/evaluation.py:105 evaluate`는 RAG 품질 측정용이다.

분석 기준은 dev 브랜치 507a9ed와 조사 시작 시 작업 트리다. 기존 미추적 cloudflared.deb는 분석에서 제외했다. 정적 조사이며 실제 배포 상태·적용 revision·운영 데이터와 백업 규모·Gemini 품질은 **UNKNOWN**이다. 아래 목표 설계와 파일명은 제안이다.

## 2. Current Architecture

| 경로 | 현재 동작과 근거 |
| --- | --- |
| 루트 | main.py:14에서 인증·readiness·티켓·지식·분석·인계·티켓 운영을 /api/v1에 등록한다. main.py:23에 루트 /health가 있다. Dockerfile, requirements*.txt, alembic.ini, compose*.yaml이 빌드·실행 진입점이다. |
| app/ | 공통 DB, HTTP 오류, 인증, 모델 및 티켓. app/auth.py:44 require_user는 해시된 쿠키 토큰·만료·활성 계정을 확인하며 :64 require_write_user는 Origin과 CSRF도 검사한다. :90 로그인은 Argon2와 HttpOnly·SameSite=Lax 쿠키, 환경별 Secure 설정을 사용한다. 세션 응답은 공통 operator 역할이며 교대 권한은 별도 멤버십이다. 계정 생성은 app/bootstrap.py:13과 app/handover/schedule.py:19에서 수행한다. |
| 티켓 | app/tickets.py:185–228은 버전, 편집 가능 여부, 감사 이벤트, 인덱스 동기화를 처리한다. :231–340의 등록·목록·상세·편집·상태전이·보관은 기존 상태/해결 요약 제약과 커서 페이지네이션을 제공한다. 보관은 종료 티켓에 시각을 기록한다. app/handover/ticket_routes.py:38–85는 담당자·교대·경고·승인·작업 이력을 관리한다. |
| app/knowledge/ | documents.py:94 원본 등록, :136–197 Markdown/PDF 추출·청킹과 페이지·섹션 추적. embedding.py:30 프로필과 Fake/Gemini, indexing.py:52·87·164 버전 기반 인덱스 갱신·lease·발행·재시도. search.py:30 내부 worker 질의 임베딩, :50 pgvector 코사인 거리, 완료 색인·프로필·해결 티켓 현재 버전·보관 제외·소스별 최고 청크. migrations/versions/0002_knowledge.py:62 벡터와 프로필 인덱스. ANN 정의는 확인되지 않았다. |
| app/agent/ | routes.py:34는 202 큐 적재, :59는 요청자별 결과 조회. queue.py:55는 SKIP LOCKED와 lease로 작업을 처리한다. analysis.py:179 investigate와 tools.py:100·161은 읽기 전용 SOP·유사 티켓·열린 티켓·상세·과거 이벤트를 호출한다. schemas.py:86 validate_draft는 구조·인용을 검사한다. 도구 허용 목록 후보는 공유 티켓 풀에서 조회한다. |
| app/handover/ | schedule.py:55는 시간대와 교대 입력, service.py:29는 맞닿는 다른 조의 다음 근무를 찾는다. snapshot.py:53은 미해결·미완료·재확인·이월·교대 중 활동과 수동 선택을 수집한다. service.py:105·162·196은 DRAFT → READY → CONFIRMED → ACKNOWLEDGED, 리비전, 스냅샷 신선도를 처리한다. 발신 리드 확정과 수신 조 멤버 확인이 분리되며 routes.py:172에 Gemini 비동기 초안이 있다. |
| frontend/ | React·TypeScript·Vite. src/main.tsx:305 App에서 세션 확인과 경로 분기. src/에 티켓·작업·SOP·AI·인계 컴포넌트 및 API 클라이언트, nginx/에 프록시, e2e/에 브라우저 테스트가 있다. |
| Compose·infra/ | compose.yaml:14–130은 DB·migrate·API·worker·Nginx·test, DB/SOP named volume, 내부 네트워크를 정의한다. DB 호스트 포트 발행은 정의되지 않았다. infra/postgres/init-runtime.sh는 런타임 권한을 설정한다. migrate가 Alembic과 bootstrap을 실행하고 worker가 Gemini를 사용한다. override는 Vite, prod는 TLS, tunnel은 cloudflared를 사용한다. frontend/nginx/static.conf:8 및 local.conf:19에 API·/health 프록시, readiness 차단, 로그인 제한과 SOP 크기 제한이 있다. 정적 모드는 SPA fallback을 제공한다. |
| migrations/, tests/, docs/ | Alembic 체인은 0001_ticket_foundation → 0002_knowledge → 0003_agent → 0004_handover. app/db.py:42 expected_schema가 head를 계산하고 :47 ready가 적용 revision과 비교한다. pytest에 헬스·인증·티켓·지식·AI·인계·통합 테스트, 별도 QA에 성능·로스터·지속성·Gemini 스모크가 있다. frontend/e2e/system.spec.ts:28·134는 장애→인수 확인과 Nginx 제한을 검증한다. docs/에는 계약·설계·QA·배포 문서가 있다. |

## 3. Feature Gap Analysis

- **고정 교대와 공통 초기값.** 4개 조 로테이션, 주간 08:00–20:00, 야간 20:00–익일 08:00, 시간대와 기준일, 초기 티켓·호스트·이전 Bot 보고서를 불변 시나리오 버전으로 발행한다. 실행에 티켓을 새 UUID로 복제하고 보고서 링크를 매핑한다. 현행 CLI는 개별 일정을 등록하며 보고서 생성은 실제 UTC 시각을 사용한다. 근거는 app/handover/schedule.py:55–80, app/handover/service.py:105, app/knowledge/demo.py:120.
- **실행과 재실습.** 개인 가입 후 NOT_STARTED, 시작 시 ACTIVE, 다음 Bot 앞으로 발신 보고서 확정 시 COMPLETED. 실습 중 티켓·SOP·AI·업무 기록·보고서 사이 이동을 자유롭게 둔다. 기존 보고서 편집 상태는 보고서 내부에만 적용한다. 완료 데이터는 읽기 전용으로 보존하며 재실습은 새 실행이다. 근거는 frontend/src/main.tsx:305, app/handover/schemas.py:11.
- **호스트·RAG·행동 기록.** 현행은 service와 개별 티켓 이벤트가 있다. host_id로 발생 시점 이전 동일 호스트 장애를 찾고, 별도로 승인 SOP·유사 사례를 벡터 검색해 인용한다. AI 권장 조치는 agent 결과에, 사용자 수행 기록은 ticket_work와 감사 이력에 저장한다. 실제 장비 조작의 독립 검증은 UNKNOWN이다. 근거는 app/models.py:33, app/agent/tools.py:100·161, app/handover/ticket_routes.py:55.
- **평가.** 전체 실행 티켓·작업·감사 이벤트·확정 리비전을 고정한다. 인계에서 빠진 초기 티켓도 평가한다. 규칙별 증거와 판정에서 종합·티켓별 결과를 계산하고 Gemini가 설명한다. 예를 들어 사용자 진단 기록, 미해결 건의 후속 조치 또는 재확인, 실제 작업과 인계 문구의 일치를 평가할 수 있다. SOP 열람은 별도 행동 이벤트가 필요하다. 근거는 app/tickets.py:196, app/handover/snapshot.py:53, app/agent/evaluation.py:105.

## 4. Database Gap Analysis

| 현재 테이블 | 관계와 목표 처리 |
| --- | --- |
| users, sessions | 사용자 1:N 세션. 계정·암호 해시·세션 보존. 신규 등록용 권한·사용자명 유일성 검증. app/models.py:13, infra/postgres/init-runtime.sh. |
| tickets, ticket_events, ticket_work | 티켓은 생성자·담당자·교대 참조, 티켓 1:N 이벤트·작업, 티켓 버전별 이벤트 유일. 실행 ID·호스트 FK·초기 템플릿 키 추가. 자식 테이블에 실행 ID를 추가해 복합 FK 적용. 기존 티켓 번호·버전 유지. app/models.py:33·87, app/handover/models.py:37. |
| shift_teams, shift_memberships, shift_instances | 조와 사용자는 N:M, 조와 교대는 1:N. 운영 일정은 보존하고 교육 보고서는 별도의 불변 시나리오 슬롯을 참조한다. app/handover/models.py:11. |
| handovers, handover_revisions, handover_items | 보고서는 발신·수신 교대와 정정 원본 참조. 리비전은 보고서 ID·버전 복합 키, 항목은 리비전과 티켓 참조. 실행·교육 슬롯·참여자 유형 추가 및 운영/교육 FK 조합 제약. 운영용 발신 교대당 루트 유일성을 유지하고 교육용 실행당 발신 보고서 유일성을 추가. app/handover/models.py:51, migrations/versions/0004_handover.py:86. |
| sop_documents, index_jobs, knowledge_chunks | SOP 또는 티켓당 색인 작업 하나, 작업 1:N 청크. 원본·출처·프로필 보존. 승인 SOP·공유 합성 사례 공개 범위를 명시하고 티켓 청크는 티켓 조인에서 실행 범위를 확인한다. app/knowledge/models.py:26, app/knowledge/search.py:64. |
| agent_runs, agent_tool_calls | 작업이 요청자 및 선택 티켓·인계 리비전을 참조, 작업 1:N 도구 이력. 작업에 실행 ID를 추가하고 도구 trace는 부모 범위를 상속. 사용자 단위 활성 분석 제약의 적용 범위를 검토. app/agent/models.py:11. |

| 신규 테이블 | 핵심 설계 |
| --- | --- |
| simulation_scenarios, scenario_shift_slots | 시나리오 키·버전 UNIQUE, 고정 기준 시각·시간대·초기 티켓·이전 Bot 보고서·규칙 버전. 슬롯의 조 코드·순서·시작/종료는 시나리오 버전에 종속. |
| simulation_runs | id, user_id FK, scenario_id FK, status, started_at, completed_at, 수신 보고서 스냅샷, 확정 보고서 ID·리비전. NOT_STARTED는 첫 실행 행 부재로 표현. ACTIVE 상태의 user_id 부분 UNIQUE, 이력 인덱스 (user_id, started_at DESC, id), 시작 요청 키에는 사용자별 유일성. |
| scenario_hosts, scenario_incidents | 버전 내 host_key UNIQUE, 표시명, 과거 사례의 호스트 FK·발생 시각·해결 요약·공개 범위. 공유 사례를 벡터로 제공하면 승인 소스 유형 추가 또는 별도 코퍼스 연결. |
| simulation_evaluations, simulation_ticket_evaluations | 실행 및 확정 리비전 FK, 규칙 버전, 고정 입력, 상태·시도·lease·재시도 시각, 판정·자연어·오류. 실행당 평가 작업 유일. 상세는 평가와 실행 티켓을 참조하며 (evaluation_id, ticket_id) 유일. |

실행 요청마다 인증 사용자 ID와 실행 ID를 검증하고 변경에는 ACTIVE를 요구한다. (simulation_run_id, id) 복합 유일 키를 티켓·보고서·평가에 두고 복합 FK로 교차 실행 연결을 제한한다. 같은 시나리오 버전의 호스트·슬롯만 참조하게 한다. 티켓 목록은 (simulation_run_id, updated_at DESC, id), 호스트 이력은 (simulation_run_id, host_id, detected_at DESC, id), 공유 사례는 (host_id, occurred_at DESC), 평가 큐는 (status, available_at) 인덱스를 제안한다. 기존 인덱스 근거는 app/models.py:78.

기존 행을 임의 실행·호스트로 변환할 필요는 없다. 운영 행은 레거시 범위로 보존하고 교육에서 NULL 실행 범위를 제외한다. 승인 합성 데모만 템플릿 이관 대상으로 삼는다. 기존 보고서 JSON·리비전·SOP 원본·벡터를 보존하고 변경된 내용·출처·프로필만 재색인한다. 추가 revision → 승인 데이터 변환 → 제약 검증 → 교육 기능 활성화 순서로 진행한다. 구버전 API readiness와 새 head의 불일치, 교육 기록이 삭제되는 downgrade, DB와 SOP 볼륨 복원 불일치가 주요 위험이다. 근거는 app/knowledge/demo.py:154, app/db.py:42, tests/qa_persistence.py:58.

## 5. API Gap Analysis

아래는 업무 라우트 전체다. 루트 헬스와 내부 worker 외에는 /api/v1 상대 경로다. 프레임워크 자동 문서 라우트는 범위에서 제외했다.

| 분류 | 현재 API | 목표 조치와 근거 |
| --- | --- | --- |
| REUSE | GET /health, GET /ready | 응답과 공개 범위를 유지. main.py:23, app/db.py:47. |
| REUSE | POST /auth/login, GET /auth/session, POST /auth/logout | 기존 세션·CSRF 유지. app/auth.py:90. |
| MODIFY | GET·POST /tickets, GET·PATCH·DELETE /tickets/{ticket_id}, POST /tickets/{ticket_id}/transitions | 실행 범위·호스트·완료 읽기 전용. 기존 커서·버전·보관 의미 유지. app/tickets.py:231. |
| MODIFY | GET·PUT /tickets/{ticket_id}/operations, POST /tickets/{ticket_id}/work, PUT /tickets/{ticket_id}/work/{work_id} | 같은 실행의 자식·배정만 검증. app/handover/ticket_routes.py:38. |
| MODIFY | GET /sops, GET /sops/{sop_id}, GET /sops/{sop_id}/original, POST /knowledge/search | 승인 SOP와 실행별 티켓 검색·출처. app/knowledge/routes.py:127·202. |
| REMOVE | POST /sops, DELETE /sops/{sop_id}, POST /sops/{sop_id}/reindex, POST /knowledge/tickets/{ticket_id}/reindex | 교육에서 공유 자료 변경·수동 재색인 비활성화. app/knowledge/routes.py:112·173. |
| MODIFY | POST /agent/analyze, GET /agent/runs/{run_id} | 실행 범위 큐·도구·결과 및 호스트 근거, 202와 폴링 유지. app/agent/routes.py:34. |
| MODIFY | GET /shift-instances, GET /shift-instances/{shift_id}/handover-preview | 고정 일정·실행별 미리보기. app/handover/routes.py:27. |
| MODIFY | GET·POST /handovers, GET·PUT /handovers/{report_id}, GET /handovers/{report_id}/revisions/{version}, POST /handovers/{report_id}/confirm, POST /handovers/{report_id}/analyze | 실행별 초안·리비전, 확정과 종료·평가 연결. app/handover/routes.py:72. |
| REMOVE | GET /handover-operators, POST /handovers/{report_id}/acknowledge, POST /handovers/{report_id}/revisions | 교육에서 공용 명단·사람 확인·확정 후 정정 생성 비활성화. 기존 리비전 읽기 유지. app/handover/routes.py:51·154. |
| REUSE | worker 내부 GET /health, POST /query-embedding | 내부 계약 유지. app/knowledge/worker.py:54. |

| NEW API | 계약 제안 |
| --- | --- |
| POST /auth/register | 계정 등록, 사용자명 고유, 패스워드·Origin·요청 제한. 기존 로그인으로 연결. |
| GET /simulation, GET·POST /simulation-runs, GET /simulation-runs/{simulation_run_id} | 첫 시작 전/현재 상태, 이력, 멱등 시작·재실습, 소유자 상세. |
| GET /simulation-runs/{simulation_run_id}/incoming-handover | 이전 Bot 보고서와 실행별 티켓 링크. |
| GET /simulation-runs/{simulation_run_id}/hosts, GET /simulation-runs/{simulation_run_id}/hosts/{host_id}/incidents | 호스트 선택과 시점·공개 범위를 검사한 이력. |
| GET /simulation-runs/{simulation_run_id}/evaluation, GET /simulation-runs/{simulation_run_id}/evaluation/tickets/{ticket_id} | 종합·티켓별 결과와 평가 대기/처리/성공/실패. 기본 재시도는 서버가 처리. |

교육 MODIFY 경로는 /simulation-runs/{simulation_run_id} 아래 기존 suffix를 재사용한다. 운영 병행 시 교육 계정의 기존 공유 경로 접근을 서버에서 차단한다. 교육 전용 배포는 운영 라우트를 노출하지 않는다. 프론트의 API 접두사·근거 링크를 갱신하되 DTO 기존 필드, CSRF, 202 폴링과 오류 형식을 유지한다. 비소유 리소스는 404, 완료 후 변경·버전 충돌은 409 기본안이다. 근거는 frontend/src/api.ts:39, app/http.py:21.

## 6. Frontend Gap Analysis

| 화면 | 현재와 목표 변경 |
| --- | --- |
| /login, /about | 로그인 후 티켓으로 이동. 가입·실습 홈·진행 중 계속하기·완료 이력 추가. frontend/src/main.tsx:29·305. |
| /tickets, /tickets/new, /tickets/{id}, /tickets/{id}/edit | 검색·생성·상세·편집에 호스트, 과거 장애, 추천과 실제 작업의 분리, 완료 읽기 전용 추가. frontend/src/main.tsx:103·160·214, frontend/src/ticket-operations.tsx:25. |
| /knowledge, /knowledge/{id}, /knowledge/search | SOP 목록·원본·검색 결과를 재사용, 관리 컨트롤 비활성화, 출처 링크에 실행 문맥 유지. frontend/src/knowledge.tsx:92·156·208. |
| /agent | 분석 결과/근거/폴링을 유지하고 저장된 최근 작업과 링크를 계정·실행별로 격리. frontend/src/agent.tsx:58·107. |
| /handovers, /handovers/{id} | 고정 근무·이전 Bot 보고서 표시, 기존 초안·재수집·리비전 열람, 다음 Bot 앞 확정→완료 화면. 사람 확인·정정 추가 버튼 비활성화. frontend/src/handover.tsx:184·403. |
| 신규 실습·평가 화면 | 자유 이동, 종합·티켓별 피드백, 대기·오류·기록 보존·재실습. URL 직접 접근과 새로고침에서 동일 실행 표시. frontend/src/main.tsx:305. |

## 7. Reuse / Modify / New / Remove Matrix

REMOVE는 교육 모드 비활성화를 뜻한다. 운영 모드 폐기와 데이터 삭제는 별도 전환 결정이다.

| 분류 | 기능과 근거 | 현재 → 목표 및 이유 |
| --- | --- | --- |
| REUSE | 세션·CSRF, app/auth.py:39 | 기존 인증 계약을 사용하고 별도 계층에 실행 소유권을 둔다. |
| REUSE | 전이·버전, app/tickets.py:55·185 | 순수 상태 전이·버전 검사 유지. |
| REUSE | 추출·임베딩·공급자, app/knowledge/documents.py:136, app/knowledge/embedding.py:56, app/agent/provider.py:19 | 기존 변환과 출처 형식, 모델 어댑터 사용. |
| REUSE | 상태 점검, main.py:23, app/db.py:42 | 기존 헬스 응답과 revision 검사 유지. |
| MODIFY | 티켓·작업·감사, app/tickets.py:174, app/handover/ticket_routes.py:55 | 공유 업무 → 실행 단위 티켓·호스트와 완료 후 변경 차단. |
| MODIFY | RAG·AI, app/knowledge/search.py:50, app/agent/tools.py:100·161, app/agent/queue.py:97 | 공유 후보와 도구 → 순위 계산 전 실행/공유 자료 필터와 호스트 근거. |
| MODIFY | 보고서, app/handover/snapshot.py:53, app/handover/service.py:196 | 조와 공유 스냅샷 → 실행 소유권·고정 슬롯·종료 및 평가 예약의 원자화. |
| MODIFY | SPA·Nginx, frontend/src/main.tsx:305, frontend/nginx/static.conf:8 | 업무 진입점·로그인 제한 → 교육 화면·실행 경로·가입 제한. |
| NEW | 가입·실행·일정 템플릿·Bot, app/bootstrap.py:13, app/knowledge/demo.py:120 | CLI/공유 데이터 → 개인 가입·동일 초기값·Bot 인계·완료 이력과 재실습. 신규 app/simulation/{models,service,seed,routes}.py 후보. |
| NEW | 호스트 이력, app/models.py:33, app/agent/tools.py:120 | 서비스 및 개별 이벤트 → 호스트와 과거 장애 이력. 신규 app/simulation/history.py 후보. |
| NEW | 평가, app/agent/evaluation.py:105 | 검색 품질 측정 → 규칙·근거·Gemini 설명·실패 복구. 신규 app/simulation/evaluation.py 후보. |
| REMOVE | SOP 편집·수동 재색인·공용 작업자 목록, app/knowledge/routes.py:173, app/handover/routes.py:51 | 교육에서는 비활성화해 공유 자료와 개인정보 보호. |
| REMOVE | 인간 수신 확인·확정 후 정정·CLI 교육 일정, app/handover/routes.py:154, app/handover/schedule.py:85 | 교육은 Bot 수신과 확정 종료, 고정 일정으로 대체. |

## 8. Technical Risks

| 위험 | 대응과 검증 |
| --- | --- |
| 높음 · RAG 누출·품질 | 승인 범위를 top-K 전에 적용한다. 임베딩 프로필·티켓 버전·근거 링크를 유지한다. 교차 실행을 함께 색인해 직접 검색·AI 도구·출처 접근을 확인하고 Recall@5를 비교한다. app/knowledge/search.py:50, tests/test_knowledge.py:303. |
| 높음 · 인증과 데이터 격리 | 두 계정, 동일 계정의 다른 실행, 레거시 경로, worker를 검증한다. 특히 직접 ID, 수동 티켓 선택, 리비전, 작업 ID, 도구 allowlist의 후보 범위를 확인한다. app/tickets.py:174, app/handover/snapshot.py:53, app/agent/queue.py:97. |
| 중~높음 · 호스트 관계 변경 | 대소문자·별칭·누락 호스트·과거 시점 검증. 레거시 NULL과 신규 호스트 FK를 함께 테스트한다. 색인 텍스트와 링크 재생성을 확인한다. app/models.py:33, app/knowledge/indexing.py:52. |
| 높음 · 인계 호환성 | 기존 교대별 루트 유일성과 교육 실행별 유일성을 분리한다. 현재 및 이전 포맷의 리비전과 스냅샷, Bot 표시와 사용자 감사 이력을 검증한다. app/handover/models.py:51, tests/test_handover.py:179. |
| 높음 · 중복 실행 | 요청 키, 활성 실행 부분 UNIQUE, 복제 일괄 커밋을 사용한다. 동시 시작·응답 유실·복제 중 실패에서 중복 실행과 불완전 스냅샷이 생기지 않아야 한다. tests/test_tickets.py:183의 동시성 패턴 참고. |
| 높음 · 확정 일관성 | 기존 내부 커밋을 조정하고 실행 잠금→버전·스냅샷 확인→리비전·완료 상태·평가 예약을 함께 커밋한다. 티켓 변경도 같은 실행 잠금 순서에 참여한다. 기존 전역 advisory lock을 유지할 경우 실행 간 직렬화 비용을 검증한다. app/db.py:36, app/handover/service.py:196. |
| 높음 · 평가 실패 복구 | 영속 작업과 고정 입력, 규칙 결과를 보존하고 설명 생성만 재시도한다. lease 만료 재수거·백오프·최대 횟수·조건부 결과 발행 및 실패 표시. 완료 실행은 그대로 열려야 한다. app/agent/queue.py:55, app/knowledge/indexing.py:197. |
| 높음 · 시드/마이그레이션 | 공유 더미와 발행 템플릿을 분리한다. 빈 DB와 레거시 복제본에서 upgrade·readiness·기존 리비전·색인·DB 및 SOP 파일 복원을 검증한다. 실 운영 데이터 규모와 백업 상태는 UNKNOWN. app/knowledge/demo.py:120, app/db.py:42, tests/qa_persistence.py:58. |

## 9. Recommended Implementation Plan

테스트는 격리 Compose와 합성 데이터·Fake provider를 기본으로 한다.

| 단계 | 범위 | 수정 대상 | 신규 파일 후보 | 선행 조건 | 테스트 | 위험 | 완료 기준 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 실행·시나리오·슬롯·호스트·복합 FK·원자적 시작 | app/models.py, app/{handover,agent}/models.py, migrations/env.py | app/simulation/{models,service}.py, migrations/versions/0005_simulation_foundation.py, tests/test_simulation.py | 회차·시계·활성 정책 | 빈 DB와 레거시 복제 업그레이드, 동시 시작, FK, 롤백 | 기존 행 혼입, head 불일치 | 초기값 동일, ID 격리, 레거시 보존 |
| 2 | 가입, 실행 보호, 티켓·SOP·RAG·worker 경계 | main.py, app/auth.py, app/tickets.py, app/handover/ticket_routes.py, app/knowledge/{routes,search,indexing}.py, app/agent/{routes,tools,queue}.py, frontend/nginx/{local,static}.conf | app/simulation/{routes,scope}.py, tests/test_simulation_scope.py | 1 및 가입·공유 범위 | 두 계정, 같은 계정의 다른 실행, 레거시/API/worker, 검색 회귀 | 격리 우회, 검색 누락 | 모든 경로 격리 검증 후 가입 활성화 |
| 3 | 고정 교대·Bot 인수·호스트 이력·조치 분리 | app/handover/{routes,service,snapshot}.py, app/agent/{analysis,tools,schemas}.py, app/knowledge/demo.py, app/tickets.py | app/simulation/{seed,history}.py, tests/test_simulation_scenario.py | 1–2 및 합성 템플릿 승인 | 야간 경계·재실습·Bot 링크·미래 사례 제외·작업 기록 분리 | 시계·링크·근거 불일치 | 초기 조건 재현, 자유 실습, 읽기 전용 AI 근거 |
| 4 | 종료 원자화·규칙·Gemini 설명·복구 | app/handover/{service,routes}.py, app/knowledge/worker.py, migrations/env.py | app/simulation/{evaluation,evaluation_worker}.py, 평가 revision, tests/test_simulation_evaluation.py | 1–3 및 평가 규칙 | 확정/변경 경합·중복 확정·DB 실패·worker 중단·Gemini 실패 | 부분 커밋·평가 중복/유실 | 확정·완료·예약 단일 커밋, 재시도 시 결과 한 개 |
| 5 | 가입→인수→자유 실습→확정→결과→재실습, 배포 안정화 | frontend/src/{main,api,agent,knowledge,handover,ticket-operations}.*, docs/{API,DATABASE,DEPLOYMENT}.md, 필요 시 Compose | frontend/src/simulation.tsx, frontend/e2e/simulation.spec.ts | 1–4 및 고정 테스트 자료 | 두 계정 브라우저, 새로고침·링크·읽기 전용 이력·pytest·Playwright·/health·복원·별도 합성 Gemini 스모크 | UI/계약 호환성 | 전체 흐름, 기존 계약 회귀와 복원 검증 통과 |

## 10. Decisions Requiring Confirmation

| 결정 | 기본안과 확인할 내용 |
| --- | --- |
| 4조 로테이션·가상 시계 | 한국 시간대를 후보로 두고 실제 순환, 기준일, 대상 교대 및 시나리오 시간 진행을 확정한다. 로테이션은 UNKNOWN이다. |
| 가입·운영 병행 | 공개/제한 가입, 운영 경로 병행 기간과 관리 권한을 정한다. 교육 계정의 기존 공유 API는 차단한다. |
| 실행 정책 | 사용자당 활성 실행 하나와 새 실행으로 재실습을 기본으로 둔다. 중도 포기·초기화, 시나리오 개정 후 재실습 버전을 정한다. |
| Bot·확정 조건 | 이전 Bot은 버전 고정 보고서, 다음 Bot은 수신자로 가정했다. Bot 대화, 필수 보고서 항목 및 최종 확정 조건을 정한다. |
| 자료·호스트 | 승인 합성 자료만 공유하는 안이다. 기존 SOP·사례 공개, 호스트 별칭·신규 호스트, 실행 티켓 실시간 색인 여부를 정한다. |
| 평가·보존 | 규칙·배점·증거, 추가 및 보관 티켓, SOP 열람 이벤트, 평가 재시도·재채점 및 기록 보존 기간을 정한다. |
| 운영 이행 | 운영 스키마·행 수·벡터·SOP 원본·백업 상태는 UNKNOWN이다. 템플릿 이관과 공개 경로 전환 범위를 별도 확인한다. |

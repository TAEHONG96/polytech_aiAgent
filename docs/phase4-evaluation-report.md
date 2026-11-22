# Phase 4 인계 확정과 교육 평가 검증 보고서

검증일은 2026년 11월 22일이다. `codex/phase3-simulation-workflows`의 완료 변경을 로컬 커밋 `82a8a3d`에 보존하고 `codex/phase4-handover-evaluation`을 만들었다. [통합 PRD](PRD-merged-draft.md), [ERD](ERD.md), [API 명세](API_SPEC.md), [Phase 3 보고서](phase3-simulation-report.md)를 기준으로 작업했다. PostgreSQL과 브라우저 검증은 Windows Docker Desktop의 별도 합성 Compose 프로젝트에서 수행했다.

## 구현 및 호환성

- 교육용 `POST /api/v1/simulation-runs/{run_id}/outgoing-handover/finalize`가 실행을 종료하는 유일한 경로다. 소유자와 CSRF를 확인하고 실행 행을 잠근 뒤 확정 리비전, 경고, 실행 `COMPLETED`, 마지막 행동 순번, 평가 입력과 상세 대상, 규칙 작업을 한 PostgreSQL 트랜잭션에 저장한다. 티켓·후속 작업·조치·자료 행동·인계 편집도 같은 실행 행을 먼저 잠근다.
- `expected_revision`과 원본 티켓 버전을 비교한다. 구조 위반은 422, 초안과 원본 버전 충돌은 409다. 저장된 멱등 키와 동일 요청은 원래 응답을 반환하고 해당 키에 다른 요청을 사용하면 409다. 빈 요약, 미해결 항목, 담당자·다음 조치·재확인·미완료 작업의 업무상 누락은 저장된 경고로 표시한다. 조치·SOP·AI 분석의 사용과 티켓 해결을 완료 조건으로 두지 않는다.
- `GET .../evaluation`, `GET .../evaluation/tickets`, `GET .../evaluation/tickets/{ticket_id}`는 완료 실행의 소유자에게 요약, 평가 목록, 행동 타임라인, 규칙 결과·근거와 설명을 제공한다. 규칙이 준비되면 설명 상태와 관계없이 조회한다. 다른 사용자나 다른 실행의 개별 티켓·자료 ID는 거부한다. 조회할 때 자료 승인과 범위를 확인하며 승인이 철회된 근거와 종속 설명은 숨긴다.
- 완료 실행의 티켓·작업·조치·인계 원본과 새 분석은 변경할 수 없다. 확정 전 예약된 기존 분석은 결과를 분석 이력에만 남긴다. 재실습은 기존 시작 API에서 새 실행을 만들고 완료 이력과 평가를 보존한다. 운영 인계·검색·인증 및 `/health` 계약을 유지했다.
- 교육 화면에 초안 저장·자료 재수집·확정·경고·평가 단계·티켓별 결과를 추가했다. 교육생은 원하는 순서대로 작업할 수 있다. 자료 확인·활용 기록은 선택 사항이다. 기존 가상 시작 시각과 시나리오 자료 시각을 유지하며 동작마다 증가하는 가상 시계를 사용하지 않는다.

## 평가 규칙과 근거

확정된 `INITIAL`과 `CARRYOVER`를 대상으로 한다. 무조치·미해결·인계 누락·보관된 티켓도 포함한다. `USER_CREATED`는 고정 스냅샷에 작업과 인계 기록을 보존하며 상세 평가와 집계에서 제외한다. 규칙 판정은 `PASS`, `NOT_MET`, `NOT_OBSERVED`, `NOT_APPLICABLE`이다. 판정에 기간이나 경과 시간을 사용하지 않는다.

| 항목 | 서버의 근거 |
| --- | --- |
| 장애 이해 | 실행에 저장된 상태 확인 또는 BMC 진단 가상 조치 |
| 호스트 이력 확인·활용 | 발생 전 같은 호스트의 승인 이력 확인 이벤트. 이후 실제 가상 조치를 연결한 별도 활용 이벤트 |
| SOP 확인·활용 | 승인 SOP 확인 이벤트. 이후 실제 가상 조치를 연결한 별도 활용 이벤트 |
| 가상 조치 적절성 | 발행 시나리오가 지원하는 메모리 교체 또는 스토리지 점검의 서버 수신증. 가상 조치 서비스가 선행 조건을 확인 |
| 사후 재검증 | 대상 조치보다 나중에 기록된 재진단. 장애가 지속되는 결과도 재검증 증거로 사용 |
| 기록의 정확성 | 재검증과 함께 남은 작업, 해결 기록 또는 확정 인계의 다음 조치. 자유 문장의 의미상 정확성은 채점하지 않음 |
| 미해결 업무 인계 | 확정 스냅샷에 티켓과 다음 조치가 있으며 후속 작업의 근거를 보존하는지 확인. 관련 업무상 누락은 티켓별 평가 경고로도 표시 |

자유 메모와 AI 권고는 수행 조치의 근거로 사용하지 않는다. `POST .../tickets/{ticket_id}/learning-evidence`는 확인과 활용을 별도 기록한다. 활용에는 자료 확인 이후 같은 티켓에서 실행한 가상 조치 ID가 필요하다. 자료 GET이나 검색 결과 노출에 의해 행동이 자동으로 등록되지는 않는다.

Gemini는 고정 판정·근거 식별자·최종 상태·경고에 따라 피드백에서 강조할 항목과 순서를 고른다. 서버는 출력 스키마와 판정별 허용 항목을 검증한 후 고정 한국어 문장으로 렌더링한다. 사용자 메모, 초안 원문, 개인정보는 요청에 보내지 않는다. 수행 타임라인과 규칙 근거는 서버가 제공하므로 설명 생성 모델이 행동 사실이나 판정을 바꿀 수 없다.

## 상태, 설계 차이 및 데이터 변경

- 규칙은 `PENDING → PROCESSING → READY | FAILED`, 설명은 `NOT_STARTED → PENDING → PROCESSING → READY | FAILED`로 관리한다. 두 Worker가 `SKIP LOCKED`로 수거하고 120초 lease 및 토큰으로 결과 쓰기를 제한한다. 일시적 실패는 유예 후 최대 3회 시도한다. 규칙이 `READY`인 평가의 실패한 설명만 내부 Worker CLI로 재시도할 수 있다. 이때 이미 저장된 규칙 결과는 보존한다.
- ERD의 교육 `FINALIZED`와 기존 인계 모델의 `CONFIRMED`가 달랐다. 교육 응답은 `FINALIZED`로 제공하고 기존 저장 상태·확정 시각·확정자와 리비전을 재사용했다. 운영 API와 운영 워크플로는 기존 상태를 사용한다.
- Phase 3 초안은 전체 티켓 버전 목록을 저장하지 않았다. 기존 초안은 포함된 스냅샷의 버전으로 검증한다. 자료를 다시 수집하면 포함되지 않은 티켓까지 전체 버전을 기록한다. 초안이 없는 실행은 리비전 0으로 요청할 수 있고 초기 스냅샷을 같은 확정 트랜잭션에서 만든다.
- `0008_training_evaluation`은 실행의 최종 리비전 복합 FK와 평가 멱등 영수증·단계별 lease를 추가한다. 자료 확인·활용은 티켓 버전을 늘리지 않아 `simulation_learning_events`를 별도로 두며 실행 감사 순번을 공유한다. 기존 완료 행에는 과거 근거를 추정해 채우지 않는다. 복구에는 해당 시점의 DB와 SOP 백업을 사용한다.

| 변경 파일 | 이유 |
| --- | --- |
| `app/simulation/access.py`, `app/simulation/handover.py`, `app/simulation/evaluation_input.py` | 소유 실행 잠금, 초안·확정 충돌, 요청 영수증, 평가 입력 고정 |
| `app/simulation/models.py`, `migrations/versions/0008_training_evaluation.py` | 최종 리비전 참조, 평가 상태·lease, 학습 이벤트 스키마와 권한 |
| `app/simulation/evaluation_rules.py`, `app/simulation/evaluation_feedback.py`, `app/simulation/evaluation_worker.py`, `app/agent/provider.py`, `app/knowledge/worker.py` | 서버 판정, 검증 가능한 한국어 피드백, 독립 Worker와 Fake·Gemini Provider 재사용 |
| `app/simulation/evaluations.py`, `app/simulation/learning.py`, `app/simulation/actions.py`, `main.py` | 실행별 평가·근거 링크, 확인·활용 등록과 감사 타임라인 |
| `frontend/src/training-evaluation.tsx`, `frontend/src/training-workspace.tsx` | 교육 확정 화면·평가 조회·선택적 근거 등록 |
| `tests/test_training_evaluation.py`, `frontend/playwright.training.config.ts`, `frontend/e2e-training/phase4.spec.ts` | PostgreSQL 경합·원자성·격리·워커 회귀와 Fake Chromium E2E |
| `docs/API_SPEC.md`, `docs/ERD.md` | 실제 확정·평가·근거 계약과 추가 스키마 기록 |

## 재현 절차와 테스트 결과

실행 대상은 새 QA 프로젝트와 테스트 DB다. 공개 가입은 QA 루프백의 `APP_TRAINING_REGISTRATION=1`에서만 사용했다. 기본 주소 풀이 소진된 Windows 호스트에서는 QA Compose 네트워크에 전용 서브넷 오버레이를 사용했다. 운영 자격 증명을 사용하지 않는다.

| 검증 | 명령과 확인 결과 |
| --- | --- |
| 백엔드 PostgreSQL | `docker compose --env-file .env.example -p polytech-qa-phase4 run --build --rm -T test python -m pytest -p no:cacheprovider -q --tb=short` · **68개 통과**(기존 59개, Phase 4 9개). 무조치·미해결 완료, 보관 티켓, 경고·422·409, 동시 요청·재전송, 행동 경합, 실패 주입 롤백, 실행 격리, 만료 lease·오래된 토큰, Gemini 실패·설명 재시도와 이후 분석을 포함한다. |
| Fake 교육 Chromium | `npx playwright test -c playwright.training.config.ts` · **1개 통과**. 저장된 가상 조치·장애 지속·인계 경고, 규칙·AI 설명, 읽기 전용, 다른 사용자 직접 URL 차단, 재실습 및 완료 이력 다시 열기를 확인했다. |
| 운영 Chromium | 합성 새 회차를 발행한 뒤 `npm run test:e2e`와 동일한 Playwright 명령 · **8개 통과**. 로그인 후 화면 이동, 티켓, 근거·AI, 후속 작업, 기존 운영 인계 및 Nginx 로그인 제한을 포함한다. |
| Nginx 로그인 제한 | QA Nginx 네트워크 네임스페이스에서 `python -m tests.qa_rate_limit` · **통과**. 순차 401·200·401, 동시 18건 중 429 7건, 동일 계정의 별도 IP 200, 버킷 회복 후 401을 확인했다. |
| 빈 PostgreSQL | 신규 DB에서 `alembic upgrade head` · **통과**. `0008_training_evaluation`, 평가 행 0건을 확인했다. |
| 레거시 PostgreSQL | 별도 DB에서 `alembic upgrade 0007_simulation_workflows`, 합성 레코드 삽입, `alembic upgrade head` · **통과**. 운영 티켓 버전 1·감사 행 1건과 교육 완료 실행을 보존했다. 완료 실행의 기존 최종 리비전 링크는 NULL이다. 새 학습 테이블에 런타임 조회·삽입 권한이 있고 삭제 권한은 없음을 확인했다. |
| 표준 Fake RAG | 별도 합성 프로젝트의 최초 Worker에서 `python -m app.agent.evaluation --prepare --phase-five` · 질의 26건, Recall@5 96.67%, 트랜잭션 내 제외 및 롤백 후 노출 복구 확인. 무관 질의 11건 중 빈 결과는 0건이었다. 이미 준비된 프로젝트에서 `--prepare`를 재시도하면 기존 보호 로직이 거부한다. |
| 깨끗한 운영 프론트 빌드 | `docker build --no-cache --target production -t polytech-phase4-frontend:qa frontend` · **통과**. 잠금 파일 기반 `npm ci`와 TypeScript·Vite 빌드를 검증했다. |
| 실제 Gemini 합성 피드백 | 별도 일회성 Worker에서 합성 스토리지 티켓을 `grade` 후 `explain`으로 실행 · **통과**. `gemini-3.5-flash-lite`가 규칙 9개 중 선택한 응답을 서버가 검증했고, 강점 3개·권장 조치 2개를 반환했다. Gemini 키는 일회성 Worker의 환경에서만 읽었다. |

브라우저 환경은 QA Compose의 `compose.yaml`과 `compose.override.yaml`을 사용한다. `AGENT_PROVIDER=fake`, `APP_TRAINING_REGISTRATION=1`, `APP_ORIGIN=http://127.0.0.1:18167`, `APP_PORT=18167`을 지정한 뒤 `docker compose ... up --build --wait nginx worker`를 실행한다. 프론트 디렉터리에서 `npm ci`와 `npx playwright install chromium`을 진행하고 `QA_BASE_URL`에 같은 주소를 지정한다. 기존 운영 E2E를 실행할 때는 `python -m app.agent.evaluation --prepare --phase-five`로 QA 자료를 반입하고 `python -m tests.qa_roster --shifts 6`의 새 회차 0·2·4를 각각 `QA_SHIFT_ID`, `QA_APPROVAL_SHIFT_ID`, `QA_EDIT_SHIFT_ID`에 설정한다. 재실행할 때 회차를 새로 발행한다.

초기 검증에서 500 본문을 JSON으로 파싱하던 롤백 테스트와 필수 전이 사유가 빠진 보관 테스트를 바로잡았다. 교육 Chromium은 완료 화면의 확정 경고를 확인하도록 수정했다. 기존 운영 Chromium 재실행에서 사용한 합성 회차의 409와 Docker 빌드 로그가 합성 회차 JSON에 섞인 설정 오류를 진단했다. 새 합성 회차의 표준 출력에서 UUID를 읽어 운영 8개를 다시 검증했다.

## 남은 위험과 Phase 5 검증

- 평가 UI는 완료와 결과를 확인할 수 있는 최소 범위다. Phase 5에서 실습 작업공간과 확정 전후 화면 연결, 키보드·모바일 접근성, 경고에서 원본 근거로 이동, 느린 평가의 상태 표시, 설명 재시도의 사용자 노출 정책을 검증한다.
- E2E를 다중 탭의 확정 경합, 설명 실패와 재시도, 자료 승인 철회, 결과 링크 직접 접근과 교육·운영 브라우저 분리로 확장한다. 대량 시나리오에서 평가 스냅샷 크기, 설명 Worker 90초 처리 예산, lease 만료와 제공자 제한을 측정한다. 실제 Gemini 검증은 합성 티켓 피드백 호출 한 건이며 실제 Gemini RAG 색인·검색과 대량 동시 처리 검증은 남아 있다.
- Ubuntu VM에서 동일 이미지·버전·합성 DB·부하로 단계별 p50/p95를 재측정한다. 동일 요청 ID로 클라이언트와 Nginx, API, DB 및 외부 임베딩 구간을 대조한다. PRD의 티켓 조회 500ms 목표를 유지한다. 이 보고서에서 Ubuntu 검증은 수행하지 않았다. [Phase 1 환경 및 부하 재현 절차](test-stabilization-report.md)
- 새 배포 후보의 깨끗한 이미지 재현성, 과거의 일시적 `DB_UNAVAILABLE` 원인, PostgreSQL·SOP 실제 복원은 배포 전 확인한다. 복원 항목에는 최종 리비전·행동 순번·학습 이벤트·멱등 영수증·규칙/설명 결과 지문과 처리 중 lease 회수를 포함한다. 외부 회원가입과 공개 배포는 별도 게이트에서 결정한다.

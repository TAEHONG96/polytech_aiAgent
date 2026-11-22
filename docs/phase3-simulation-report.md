# 교육 시뮬레이터 Phase 3 실습 업무

작업 브랜치 `codex/phase3-simulation-workflows` · Phase 2 기준 커밋 `2aeecb2`

## 구현한 기능

- 기존 실행 생성과 템플릿 복제, 세 개 근무 슬롯, Bot 인수 스냅샷을 재사용했다. 교육 화면은 근무와 인계, 티켓, 동일 호스트 이력, SOP·유사 장애 검색, AI 분석, 가상 조치, 후속 작업 사이의 링크를 제공한다. 인계 확인, 검색, AI 분석과 티켓 해결 수는 실습 진입 조건에 포함되지 않는다.
- 서버가 시나리오 시작 시각과 기존 티켓 발생 시각을 고정한다. 교육 티켓의 발생 시각 변경 요청은 거부한다. 근무 종료 시각을 전이 조건으로 사용하지 않는다. 조치·메모와 티켓 변경·작업은 서버 기록 시각 및 실행별 순번을 가진다.
- 승인 상태, 시나리오와 발생 전 시점을 검사하는 Phase 2 검색·인용 정책을 사용한다. 호스트 이력에는 같은 호스트 조건을 추가한다. 유사 장애 검색은 대상 티켓이 있을 때 같은 시나리오의 승인 과거 사례를 포함하며, SOP 검색과 화면에서 구분한다. 인용은 기존 소유권·승인 재검사를 거치는 출처 링크를 사용한다.
- 조치 카탈로그가 호스트별 매개변수, 선행 상태와 결과를 정의한다. 수행 시 실행 내부 호스트 상태와 티켓 버전·이력만 DB 트랜잭션으로 저장한다. AI 권고, 자유 메모, 수행 조치는 별도로 표시한다. 교육생이 쓴 메모나 미해결 작업은 조치 실행 증거로 간주하지 않는다.
- 같은 실행에서 같은 키와 본문은 저장된 영수증을 반환한다. 동일 키의 다른 티켓이나 본문은 충돌로 처리한다. 완료 실행은 읽을 수 있으며 쓰기와 새 분석은 Phase 2 정책에 따라 거부한다.

## 시나리오 데이터

발행된 `dco-basic 1.0`과 기존 실행은 그대로 사용한다. 실습용 카탈로그와 과거 이력은 새 발행 버전 `dco-basic 1.1`에 저장했다. 두 버전 모두 주간 슬롯을 2026-11-20 08:00 KST부터 시작한다.

| 슬롯 | 배정 | 고정 근무 시간 (KST) |
| --- | --- | --- |
| 이전 | D조 Bot · 야간 | 11-19 20:00–11-20 08:00 |
| 현재 | A조 교육생 · 주간 | 11-20 08:00–20:00 |
| 다음 | B조 Bot · 야간 | 11-20 20:00–11-21 08:00 |

- 이전 Bot은 스토리지 응답 지연 이월 티켓을 가리킨다. 현재 슬롯에는 초기 메모리 진단 경보가 있다. 재실습은 각 티켓에 새 UUID를 부여하고 발행 버전의 호스트, 시작 상태와 발생 시각을 복제한다.
- 스토리지와 메모리 호스트의 승인 과거 사고 각 1건, 시점 경계에 해당하는 동시각과 미래 사고 각 1건, 미승인 과거 사고 1건을 넣었다. 이력 원본을 발행한 뒤 수정할 수 없고 승인과 임베딩 프로필은 운영자가 관리할 수 있다.
- 카탈로그에는 서버 상태 확인, BMC 진단, 메모리·스토리지 점검, 가상 메모리 교체와 재진단이 있다. 메모리 교체는 점검 후 수행할 수 있으며 재진단 뒤 호스트가 정상으로 바뀐다. 스토리지 재진단은 지연 지속 상태를 기록한다. 어느 조치도 티켓을 자동 해결하거나 완료하지 않는다.
- 합성 메모리 SOP는 `python -m app.simulation.learning_materials --operator demo`로 선택한 격리 스택의 worker에서 반입한다. 기존 파일·색인 흐름을 사용하며 저장한 내용과 제목이 일치하는 경우에만 교육 자료로 승인한다. 두 번 실행해도 같은 문서 ID를 반환한다.

## 코드·마이그레이션과 API

| 경계 | 주요 변경 |
| --- | --- |
| 교육 서비스 | `app/simulation/actions.py`와 `main.py`에서 교육 조치·메모·타임라인을 제공한다. `app/simulation/resources.py`는 호스트 이력 응답과 기존 근거 링크를 사용한다. `app/simulation/routes.py`는 새 버전을 목록 앞에 둔다. `app/simulation/learning_materials.py`로 합성 SOP를 반입한다. |
| 감사와 AI | `app/models.py`, `app/tickets.py`, `app/simulation/models.py`는 티켓 감사 행에 실행, 순번, 조치 키를 연결한다. `app/agent/tools.py`의 티켓 이력 출력은 메모와 수행 조치를 구분한다. |
| 물리 스키마 | `0007_simulation_workflows`는 기존 실행·티켓·티켓 감사·사고 테이블을 확장하고 시나리오 1.1을 발행한다. 기존 교육 이벤트 순번을 기록 시각과 티켓별 버전에 따라 채운다. 기존 운영 이벤트에는 교육 순번을 붙이지 않는다. |
| 교육 브라우저 | `frontend/src/training.tsx`, `training-workspace.tsx`는 근무·티켓 작업공간을 연다. 운영 화면의 `ticket-operations.tsx`와 `agent.tsx` 표시 컴포넌트를 교육 실행 경로에서 재사용하고 `api.ts`의 세션 사용자 ID로 본인 배정을 지원한다. 교육 사용자의 근무조/담당자 요청은 공유 운영 API를 사용하지 않는다. |
| 자동 검증 | `tests/test_simulation_workflows.py`는 새 버전 시드·권한·조치와 기존 계약을 검증한다. `tests/qa_persistence.py`의 복원 비교에 사고 코퍼스를 포함했다. |

- `POST /api/v1/simulation-runs/{run_id}/tickets/{ticket_id}/actions`는 `Idempotency-Key`를 받고 최초 201, 재전송 200, 버전 및 선행 조건 충돌 409를 반환한다. `expected_version`은 선택 입력이다. 기존 GET의 작업 목록 `items`에 `catalog`, `activity`를 추가했다.
- `GET /api/v1/simulation-runs/{run_id}/events`는 `after_sequence`, `limit`으로 감사 타임라인을 읽는다.
- 기존 `GET .../host-incidents`에 `host`, `incidents`, `limit`을 추가했다. Phase 2의 `items`를 유지한다. 교육 티켓 PATCH는 다른 발생 시각을 422로 거부한다. 운영 티켓 PATCH, 기존 인증·쿠키 및 `GET /health` 계약은 유지한다.

## 회귀 명령과 결과

검증은 Windows Docker Desktop의 별도 합성 Compose 프로젝트에서 수행했다. 기본 주소 풀을 사용하기 어려운 환경이라 QA 프로젝트 전용 브리지 서브넷을 지정했다.

| 검증 | 명령 또는 결과 |
| --- | --- |
| PostgreSQL 테스트 | `docker compose --env-file .env.example -p polytech-qa-phase3 -f compose.yaml -f <QA 네트워크 오버레이> run --build --rm -T test python -m pytest -p no:cacheprovider -q --tb=short` · 기존 55개와 Phase 3 4개를 합쳐 **59개 통과**. 고정 슬롯과 날짜 경계, Bot 링크, 재실습, 과거 사고 권한, 조치 조건, 서버 시각, 동시 재전송과 완료 실행을 포함한다. |
| Alembic | 빈 QA DB와 별도 0006 합성 DB의 `alembic upgrade head` 통과. 기존 운영 이벤트의 순번은 NULL, 교육 이벤트는 1·2로 확인했다. 원래 페이로드와 Bot 스냅샷은 그대로였으며 1.0과 1.1이 모두 조회됐다. |
| 표준 RAG | 새 worker 프로세스에서 `python -m app.agent.evaluation --prepare --phase-five` 통과. Fake 질의 26개, Recall@5 96.67%. |
| 운영 브라우저 | [통합 테스트 계획](TEST_PLAN.md)의 합성 회차와 `npm run test:e2e`와 동일한 Chromium runner로 기존 Playwright **8개 통과**. |
| 교육 브라우저 | 루프백 QA 가입 계정 두 개로 Chromium 검증 통과. 주야간 슬롯·Bot 이월 링크, 메모와 조치 분리, BMC→점검→교체→재진단, 후속 작업, 승인 SOP와 유사 장애 검색, AI 인용, 새로고침과 타인 직접 URL 차단을 확인했다. 조치와 작업을 검색·분석보다 먼저 수행했다. |
| 운영 프론트 빌드 | `docker build --no-cache --target production -t polytech-phase3-frontend:qa frontend` · 깨끗한 npm 설치, TypeScript 및 Vite 빌드 통과. |
| 로그인 제한 | QA Nginx 네임스페이스의 `python -m tests.qa_rate_limit` 통과. 순차 401·200·401, 동시 18건 중 429 7건, 별도 IP의 동일 계정과 버킷 회복을 확인했다. |
| 백업·복원 | `python -m tests.qa_persistence --project polytech-qa-phase3-browser --restore-project polytech-qa-phase3-restore --origin http://127.0.0.1:18163 --wait-timeout 240` 통과. 재시작과 DB 중단·readiness 회복, 액세스 로그, **19개 테이블의 행 지문과 SOP 원본 8개**를 대조했다. 사고 5건과 티켓 감사 41건을 포함했다. |

중간 검증에서 SQL 시드 문자열의 콜론이 SQLAlchemy 바인딩으로 해석된 문제를 수정했다. 교육 Chromium 재실행에서는 슬롯 데이터가 렌더링될 때까지 기다리고, 검색 선택 필드의 접근성 이름을 명시했다. 같은 범위를 다시 검증했다.

## 남은 위험과 Phase 4

- 합성 사고 벡터는 Fake 프로필로 준비했다. 실제 Gemini의 승인 합성 자료 색인·검색과 별도 배포 후보의 성능 검증이 남아 있다. Phase 2의 독립 Ubuntu 500ms 목표와 과거 로그인 503 조사도 배포 게이트로 유지한다.
- 이전 감사 순번은 남아 있는 서버 시각과 버전으로 복원했다. 과거에 같은 시각으로 기록된 다른 티켓의 실제 처리 순서는 알 수 없다. 마이그레이션은 발행 버전과 감사 데이터를 보존하기 위해 직접 downgrade를 거부한다. 배포 후보의 복구 계획에는 이전 이미지와 해당 시점 백업을 사용한다.
- Phase 4는 발신 인계 최종 확정만 실행 완료 원인으로 사용해야 한다. 소유 실행 행을 잠근 트랜잭션에서 확정 리비전·실행 상태·고정 평가 입력·평가 예약을 같이 저장하고 마지막 감사 순번을 고정한다. 초기·이월 티켓은 무조치나 보관된 경우에도 평가 대상에 포함하며, 자유 메모·AI 권고·실제로 수행한 가상 조치의 증거를 구분한다. 인수 확인 행동 로그와 교육 자료 열람의 평가 연동은 이 단계의 후속 설계 사항이다.

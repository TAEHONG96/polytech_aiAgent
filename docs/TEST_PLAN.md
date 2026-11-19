# 5단계 통합 테스트 계획

2026-11-19 (KST)

기준선은 `main`의 `79ec4f4`다. 실행 계약은 FastAPI·React·Alembic·Compose 소스를 기준으로 확인한다. `GET /health`의 200 및 `{"status":"ok"}`를 유지한다.

## 실행 순서

1. Git 상태, Docker 엔진, Compose 프로젝트·컨테이너·볼륨을 조회한다. 개발 프로젝트를 재시작하거나 변경하기 전에 테스트 프로젝트가 별도인지 확인한다.
2. 임의의 이름으로 만든 QA Compose 프로젝트에 전체 Alembic 마이그레이션과 테스트 계정을 적용한다. 기존 회귀를 베이스라인으로 실행한다.
3. FastAPI와 PostgreSQL을 연결해 장애 등록부터 인수 확인까지 실행한다. 의존성 경계에서 Fake 임베딩 및 모델을 사용한다.
4. 실제 Gemini 검증은 별도 QA 프로젝트에서 합성 자료로 한정한다. RAG 평가에는 문서·티켓별 라벨, 상위 5개 결과와 경과 시간을 기록한다. 장애 분석과 인수인계는 소규모 호출의 도구 순서·오류·최종 응답을 기록한다.
5. Playwright에서 Nginx를 통해 React와 실제 API를 연결하고 UI 상태 및 API 응답을 확인한다.
6. 인증, 권한, 실패 상태와 재시도를 검증한다. 고정된 합성 입력으로 동시 5개 이하의 요청을 보내고 지연 및 오류율을 관측한다.
7. 격리 DB에서 논리 백업·별도 대상 복원, 컨테이너 재시작 후 티켓·스냅샷·벡터·SOP 원본 유지 여부를 확인한다. 개발과 운영 설정을 별도로 검증한다.
8. 발견한 회귀는 재현과 최소 수정을 거쳐 전체 테스트를 재실행한다. 실행 명령과 결과는 [테스트 보고서](TEST_REPORT.md)에 기록한다.

## 수용 시나리오

| 범위 | 검증 |
| --- | --- |
| A · 장애 등록부터 인수 확인 | 로그인, 합성 장비 증상 등록, SOP·해결 사례 검색, 분석 근거, 작업 이력, 미해결 상태, 누락 탐지와 보완, 초안 편집, 인계조 확정, 인수조 확인, 원본 변경 후 스냅샷 확인 |
| B · 근거 부족 | 오류 코드 없는 티켓, 제한된/빈 검색 결과, 추가 확인 요청, 허위 출처 차단 |
| C · 여러 교대 이월 | 이전 회차 배정, 완료·미완료 및 유사 작업 기록, 조·담당자 변경, 여러 회차의 버전 및 원본 보존 |
| 보안·복구 | 쿠키 세션과 CSRF, 역할·조별 변경 권한과 분석 소유권, 입력·업로드, 프록시와 로그인 제한, 모델 및 DB 실패, 트랜잭션 롤백 |
| 영속성 | 전체 마이그레이션, FK, 768차원 및 색인 프로필, 시드 재실행, 재시작, 백업과 격리 복원 |
| 성능 | 일반 API p95 1초 및 RAG p95 2초를 초기 관측 목표로 사용. AI 완료 시간과 인수인계 티켓 수별 소요 시간을 별도 기록 |

단일 조직의 티켓·보고서 조회 정책을 따른다. 조 간 보고서 변경 제한과 장애 분석 요청 소유권을 확인한다. 테스트 DB와 SOP 볼륨은 개발·시연 프로젝트의 볼륨과 분리한다.

## 격리 환경 재현

저장소 루트의 PowerShell에서 실행한다. Docker Desktop CLI를 PATH에 추가하거나 명령의 `docker`를 설치된 CLI의 절대 경로로 바꾼다. 프로젝트 이름과 포트는 실행 환경에서 비어 있는 값을 사용한다.

```powershell
$env:AGENT_PROVIDER = 'fake'
$env:GEMINI_API_KEY = ''
docker compose -p polytech-qa-backend -f compose.yaml run --build --rm test
docker compose -p polytech-qa-rag-fake -f compose.yaml run --build --rm -T worker python -m app.agent.evaluation --prepare --phase-five

$env:APP_PORT = '18085'
$env:APP_ORIGIN = 'http://127.0.0.1:18085'
docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml up --build --wait nginx worker
docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml exec -T worker python -m app.knowledge.demo
$qaRoster = docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml run --build --rm -T test python -m tests.qa_roster --shifts 4 | Select-Object -Last 1 | ConvertFrom-Json
$env:QA_SHIFT_ID = $qaRoster[0]
Push-Location frontend
npm ci
npx playwright install chromium
npm run test:e2e
Pop-Location
```

브라우저 실행마다 `qa_roster`로 새 회차를 만든다. 같은 로그인 주소에서 제한 테스트를 실행한 직후 다시 로그인해야 하면 Nginx 버킷이 소진 상태에서 회복될 시간을 확보한다. Python 3.12 가상환경에 `requirements-dev.txt`를 설치하면 호스트에서 성능 및 복원 명령도 실행할 수 있다.

```powershell
$qaPerfRoster = docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml run --rm -T test python -m tests.qa_roster --shifts 4 | ConvertFrom-Json
python -m tests.qa_performance --shifts $qaPerfRoster[0] $qaPerfRoster[1] $qaPerfRoster[2]
python -m tests.qa_persistence --project polytech-qa-browser --restore-project polytech-qa-restored
```

`qa_persistence`는 지정한 QA 서비스만 중단·재기동하며, 비어 있는 별도 QA 볼륨에 DB 및 SOP 원본을 복원한다. Docker CLI가 PATH에 없다면 `QA_DOCKER_EXECUTABLE`에 경로를 지정한다. 검증 후 QA 프로젝트는 `docker compose -p <QA 프로젝트> ... stop`으로 정지할 수 있다.

## 실제 Gemini 실행

명령은 합성 자료가 없는 별도 프로젝트에서 실행한다. `.env`에 worker 키를 설정하고 환경에 남아 있는 Fake 설정을 해제한다. 출력으로 모델·프로필, 도구 순서, 호출 상태 및 검색 지표를 확인한다.

```powershell
Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
$env:AGENT_PROVIDER = 'gemini'
docker compose --env-file .env -p polytech-qa-rag-gemini -f compose.yaml run --build --rm -T worker python -m app.agent.evaluation --prepare --phase-five
docker compose --env-file .env -p polytech-qa-gemini-smoke -f compose.yaml run --build --rm --no-deps -T worker python -m app.handover.smoke
```

실제 장애 분석 스모크는 위 RAG 색인이 완료된 프로젝트를 사용한다. 아래 모델은 검증 당시 장애 분석과 인수인계에서 성공했다.

```powershell
$env:GEMINI_GENERATION_MODEL = 'gemini-3.5-flash-lite'
$env:APP_PORT = '18089'
$env:APP_ORIGIN = 'http://127.0.0.1:18089'
docker compose --env-file .env -p polytech-qa-rag-gemini -f compose.yaml -f compose.override.yaml up --build --wait nginx worker
docker compose --env-file .env -p polytech-qa-rag-gemini -f compose.yaml -f compose.override.yaml run --build --rm --no-deps -T test python -m tests.qa_roster
python -m tests.qa_gemini_incident
```

RAG 평가에는 합성 SOP 7개·해결 티켓 7개와 질의 26개가 들어간다. 양성 질의별 기대 UUID와 실제 상위 5개를 비교한다. Recall@5, Precision@5, MRR은 양성 질의 평균이며 Precision@5의 분모는 5다. 정답 자료가 없는 11개 질의는 결과가 비었는지 별도 집계한다. 지연은 질의 임베딩과 DB 검색 시간을 합산한다. 삭제 검증은 합성 SOP를 트랜잭션 안에서 제거해 검색 반영을 확인한 뒤 롤백한다.

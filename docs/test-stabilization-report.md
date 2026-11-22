# 교육 시뮬레이터 Phase 1 테스트 안정화

- 기준 커밋 `a56ae96`, 작업 브랜치 `codex/phase1-test-stabilization`
- 검증 범위는 Windows Docker Desktop의 격리된 Compose 프로젝트와 합성 자료다. Ubuntu VM 실측은 수행하지 않았다.

백엔드 51개와 Chromium E2E 8개가 통과했다. 표준 RAG 평가의 ORM 초기화 오류와 로그인 뒤 중복 문서 이동을 수정했다. Windows에서 티켓 조회 p95는 1,092.95ms로 PRD의 500ms 기준을 넘었다.

## 재현 결과와 변경

| 문제 | 원인과 확인 범위 | 변경 |
| --- | --- | --- |
| 표준 RAG 평가가 데모 시드에서 실패 | 독립 `python -m app.agent.evaluation --prepare --phase-five`의 전체 traceback에서 `evaluation.main → prepare → demo.seed → Session.flush → Mapper._sorted_tables → ForeignKey._resolve_column → NoReferencedTableError`를 확인했다. `tickets.scenario_id`가 참조하는 `scenario_ticket_templates`가 그 프로세스의 공통 `Base.metadata`에 없었다. DB에는 `0005` 테이블이 있었다. 기존 Alembic은 도메인 모듈을 개별로 불러왔고, FastAPI는 시뮬레이션 라우터를 불러왔다. 독립 CLI에선 그 경로가 실행되지 않았다. | `app/model_registry.py`에서 같은 `Base`를 쓰는 모델을 모아 등록하고 `app/db.py`, `migrations/env.py`가 공통 경로를 사용한다. 스키마와 revision은 유지한다. `tests/test_model_registry.py`는 데모, RAG CLI, worker, FastAPI 진입점을 각각 새 프로세스에서 불러와 FK 대상과 매퍼를 검증한다. |
| 로그인 후 Playwright가 5초에 실패 | 로그인 HTTP 200 뒤 전체 문서 이동을 기다리다 5초에 실패했다. Trace에서 `/tickets` 문서 200, 문서 전후 중복 목록 요청과 새 문서 세션 200을 확인했다. 문서 이동을 쓰던 표본은 클릭부터 화면 표시까지 9.33초 및 13.93초, 목록 응답은 각각 357ms 및 1,971ms였다. 두 번째 표본의 `/tickets` 문서 응답 완료는 4.14초였다. Trace 콘솔 오류는 인증 전 `/auth/session` 401로 확인됐다. 부팅 중 스크립트 예외는 관측되지 않았다. 긴 업무 케이스는 티켓 저장 뒤 재로딩의 전체 `load`를 기다리다 60초에 중단됐다. | `frontend/src/main.tsx`에서 직접 `/login` 성공 때 History API로 `/tickets` URL을 반영하고 로그인 응답의 세션을 사용한다. 초기화가 끝난 과거 요청이 세션 상태를 덮지 않게 했으며, 브라우저 뒤로 가기와 직접 URL·새로고침 세션 복원은 유지한다. `frontend/e2e/system.spec.ts`는 커밋·목록 200·화면·세션을 확인하고 시나리오를 나눴다. 공통 60초 제한은 유지한다. `frontend/playwright.config.ts`는 실패 Trace를 보존한다. 회차 지연 재현은 Vite 개발 모드를 사용하며, `docs/TEST_PLAN.md`에 초안·승인용 별도 회차 입력을 기록했다. |
| 로그인 순차 12회에서 429가 나오지 않음 | 로컬·운영 Nginx는 `$binary_remote_addr`를 키로 쓰는 leaky bucket이다. 터널 설정은 신뢰 경계의 클라이언트 IP 헤더를 사용하고 헤더가 없으면 연결 주소를 사용한다. `12r/m`, `burst=10`, `nodelay` 때문에 초과 용량은 약 5초마다 한 요청씩 감소한다. 고정된 1분 창이 없으며 요청은 업스트림 인증 전에 집계되므로 성공·실패 로그인이 같은 IP 버킷을 쓴다. 느린 순차 요청에 일률적으로 429를 기대할 수 없다. [Nginx limit_req 문서](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html) | Nginx 설정은 유지한다. Playwright는 짧은 동시 버스트에서 429 본문과 요청 ID를 확인한다. `tests/qa_rate_limit.py`는 실제 QA Nginx 네트워크 네임스페이스에서 순차 로그인, 버스트, 같은 계정의 독립 IP, 버킷 회복을 확인한다. |
| Fake 검색까지 느려짐 | 계층별 측정에서 SOP·해결 티켓 검색의 worker 왕복이 가장 큰 측정 구간이었다. 같은 worker를 대상으로 한 동시 요청 5개의 교차 측정에서 호출별 `httpx2.Client` 생성 자체가 p50 475.44ms와 p95 834.07ms를 차지했다. | `app/knowledge/search.py`는 프로세스 내 스레드가 worker용 HTTP 클라이언트 하나를 공유하고, 정상 종료에서 닫는다. 기존 timeout, 프록시 환경 무시, TLS 검증, API 오류 매핑을 유지한다. `tests/test_knowledge.py`에서 동시 검색의 초기화가 한 번인지와 429·503 경계를 검증한다. |
| 복구 스크립트가 60초에 중단 | Windows Docker에서 컨테이너 재시작 뒤 의존 서비스의 준비 확인이 60초 안에 끝나지 않았다. 제한을 240초로 높인 사전 재현에서는 준비 상태가 회복되고 복원이 가능했다. | `tests/qa_persistence.py`에 `--wait-timeout`을 추가했다. Compose health 검사와 내부 `/api/v1/ready`를 함께 사용한다. DB가 내려가면 공개 `/health` 200, 내부 readiness 503 `DB_NOT_READY`, 업무 API 503 `DB_UNAVAILABLE`을 기대한다. 복구 후 readiness 200을 확인하고 18개 테이블 및 SOP 원본을 비교한다. 입력한 루프백 주소가 소스 QA 프로젝트의 Nginx 포트와 일치할 때만 합성 자료에 접근한다. |

PRD·ERD·API 계약, Nginx 제한값, DB 인덱스와 운영 설정은 수정 범위에 포함하지 않았다. 새 마이그레이션 또는 Phase 2 기능도 추가하지 않았다.

## 성능 분석 방법

- Windows Docker Desktop에서 `docker-desktop` WSL 2, Linux `5.15.167.4-microsoft-standard-WSL2`, 논리 CPU 18개, Docker 메모리 7.52 GiB, `overlay2`, Compose 5.7.0을 확인했다. 한 표본에서 WSL 스왑 2 GiB 중 사용은 0이었다. 이전 QA API 컨테이너의 종료 코드 137은 관측됐으며 `OOMKilled=false`였다. 이 표본만으로 지속적인 스왑이나 디스크 병목을 판정할 수 없다.
- 개발 Compose는 Windows 파일의 `app`, `main.py`를 API에 읽기 전용으로 연결하고 Uvicorn `--reload`를 사용한다. Vite도 소스와 HTML을 연결한다. 운영 구성은 API 코드와 정적 프론트 파일을 이미지에서 읽으며 TLS 경로를 사용한다. PostgreSQL과 SOP 원본은 named volume을 사용한다. DB 초기화 스크립트는 bind mount다. worker에 512 MiB 메모리 제한이 있다. 이미지 다운로드, 빌드, 마이그레이션, Vite 초기 변환은 성능 표본에서 제외한다.
- 기준·모델 등록 수정 직전·직후의 `docker stats --no-stream` 표본에서 API 약 92–114 MiB, DB 46–92 MiB, worker 106–121 MiB, Nginx 14–15 MiB, Vite 315–335 MiB였다. 준비 단계에 worker CPU 105.12%가 한 번 관측됐다. 앞뒤 표본의 `BlockIO`는 0B였다. 이는 Windows 파일 공유나 순간 CPU 대기 시간에 대한 시계열 자료를 제공하지 않는다. Docker 내부의 깨끗한 프론트 이미지 빌드는 `npm ci` 네트워크 `ETIMEDOUT`을 재현했다.
- `tests/qa_performance.py`의 기존 부하는 최대 동시 요청 5개, 티켓 생성·조회와 SOP·해결 티켓 검색 각 20건, 분석 접수와 완료 각 5건, 인계 작성·확정·조회 각 3건이다. 완료 폴링은 100ms, 전체 표본은 99개다. 격리된 새 DB에 같은 합성 자료로 측정한다.
- `compose.qa-profile.yaml`, `tests/qa_profile.py`, `tests/qa-nginx-log-format.conf`는 QA에서만 계측한다. 요청 ID로 클라이언트, Nginx `$request_time` 및 `$upstream_response_time`, FastAPI 처리, SQL 드라이버 실행 시간, 풀 체크아웃 및 `pre_ping`, Fake worker 임베딩 왕복을 연결한다. `tests/qa_profile_summary.py`가 p50/p95와 미매칭 수를 출력한다. SQL 드라이버 시간은 DB 서버 실행과 소켓 왕복을 포함한다. 실제 Gemini 평가의 `embedding_ms`는 공급자 호출 시간에 해당하며 Fake worker 왕복과 구분한다. 토큰·요청 본문·SQL 바인딩 값은 계측 로그에 기록하지 않는다.

## Windows 수정 전후 결과

아래 역사적 측정은 개발 API의 bind mount 및 reload를 사용했다. 통제 재측정에서는 동시성, 요청 수, 데이터 유형과 보고서 크기를 유지했다. API는 이미지 코드를 쓰고 reload를 끈 동일 QA 구성으로 실행했다. 기준 프로젝트의 준비 재시도에서 합성 교대 4개가 더 만들어졌고, 측정용 각 보고서는 양쪽에서 1·5·20개 항목을 사용했다.

| API | 기존 Windows p50 / p95 (ms) | 동일 QA 기준 이미지 | 모델 등록 수정 후 | 최종, HTTP 클라이언트 재사용 |
| --- | ---: | ---: | ---: | ---: |
| 티켓 조회 | 432.23 / 1,612.96 | 143.24 / 564.45 | 421.84 / 849.43 | 783.55 / 1,092.95 |
| 인계 조회 | 1,169.58 / 1,757.87 | 79.41 / 81.45 | 79.68 / 88.12 | 211.07 / 389.14 |
| SOP 검색 | 2,255.42 / 3,656.83 | 778.88 / 1,204.16 | 784.62 / 1,814.07 | 564.14 / 1,601.20 |
| 해결 티켓 검색 | 1,913.43 / 2,881.26 | 699.02 / 961.94 | 910.49 / 1,076.75 | 658.41 / 1,242.03 |

기존 실행과 세 재측정에서 각각 99건의 오류는 0건이었다. 각 재측정에서 HTTP 표본 94개는 모두 계층 로그와 연결됐다. 분석 완료 5건은 큐의 시작부터 종료까지 직접 측정했다. 통합 PRD의 비AI 주요 읽기 p95 기준은 500ms다. 최종 인계 조회 표본 3건의 p95는 기준 이내였고, 티켓 조회 20건은 기준을 넘었다. 실제 외부 임베딩이 들어간 검색은 API 왕복과 임베딩 시간을 따로 기록한다. 모델 등록 변경은 경로 시작 시 적용된다. 요청의 실행 간 편차를 그 변경의 비용으로 귀속할 증거는 없다.

| 모델 등록 수정 후 | Nginx 전체 p50 / p95 | FastAPI p50 / p95 | SQL 실행 p50 / p95 | 풀 체크아웃 p50 / p95 | 임베딩 왕복 p50 / p95 | SQL 회수 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 티켓 조회 | 361 / 607.55 | 282.01 / 457.37 | 50.16 / 98.67 | 22.89 / 74.55 | 0 | 2 |
| 인계 조회 | 72 / 78.30 | 66.14 / 71.77 | 24.80 / 25.94 | 1.39 / 1.52 | 0 | 15 |
| SOP 검색 | 745 / 1,613 | 714.04 / 1,424.81 | 49.29 / 173.04 | 21.90 / 99.25 | 500.72 / 986.52 | 2 |
| 해결 티켓 검색 | 834 / 1,036.20 | 790.46 / 997.56 | 50.23 / 108.56 | 33.03 / 71.01 | 507.45 / 730.54 | 2 |

시간 단위는 ms다. 각 계층의 p95는 다른 요청에서 나올 수 있다. Nginx가 upstream 외부에서 쓴 시간의 p95는 위 네 요청에서 0–7.9ms였다. 애플리케이션 요청 중 SQL 수는 티켓·검색 2회, 인계 상세 1·5·20개 항목에서 15회로 일정했다. Fake 벡터 계산을 컨테이너 안에서 직접 실행한 p50/p95는 0.76/17.80ms였다. 동일 worker에 새 클라이언트로 요청한 전체 p50/p95는 524.01/1,203.95ms, 클라이언트 공유 시 111.81/191.20ms였다. 각 방식은 40개 표본으로 교차 실행했다.

API가 발행한 조회 문을 그대로 수집해 합성 DB에서 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`으로 확인했다. 세션·사용자·티켓은 PK를 사용했고 티켓 본문 조회의 서버 실행은 0.086ms였다. 인계 상세는 기존 PK와 `ix_agent_runs_handover`를 사용했다. 같은 교대 관련 조회가 반복됐으며 1·5·20개 항목에서 쿼리 수는 15회로 고정됐다. 검색은 `ix_knowledge_chunks_profile`과 티켓·SOP PK를 사용했고 서버 실행은 SOP 2.119ms, 해결 티켓 1.046ms였다. 관측한 계획의 디스크 읽기 블록은 0이었다. 이 자료 규모에서는 DB 인덱스 추가나 풀 설정 변경을 뒷받침하는 병목을 확인하지 못했다.

실제 Gemini 표준 평가에서 질의 26개의 외부 임베딩 p50/p95는 582.72/1,587.85ms, 임베딩과 DB 검색을 더한 p50/p95는 640.41/1,644.01ms였다. Recall@5는 100%였다. 이 값은 Fake 서버 왕복과 별도의 측정이다.

| 최종 | Nginx 전체 p50 / p95 | FastAPI p50 / p95 | SQL 실행 p50 / p95 | 풀 체크아웃 p50 / p95 | 임베딩 왕복 p50 / p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 티켓 조회 | 215.5 / 339.60 | 153.42 / 289.18 | 38.30 / 79.40 | 8.85 / 37.64 | 0 |
| 인계 조회 | 118 / 123.40 | 112.58 / 117.17 | 37.38 / 41.14 | 1.93 / 2.01 | 0 |
| SOP 검색 | 526.5 / 1,476.45 | 459.03 / 1,323.37 | 72.97 / 174.04 | 36.91 / 100.14 | 101.52 / 879.34 |
| 해결 티켓 검색 | 577.5 / 768.15 | 493.42 / 736.22 | 100.67 / 194.84 | 42.64 / 113.84 | 80.54 / 251.06 |

클라이언트 재사용 후 Fake 검색의 worker 왕복 중앙값은 두 경로 모두 감소했다. 해결 티켓 검색 전체 p95는 모델 등록만 반영한 실행의 1,076.75ms에서 1,242.03ms로 늘었다. 최종 티켓 조회의 Nginx 내부 p95는 339.60ms였고 Windows 호스트 클라이언트 p95는 1,092.95ms였다. 요청별로 연결해 클라이언트 전체 시간에서 Nginx 내부 시간을 뺀 구간의 p50/p95는 기준 12.66/31.16ms, 모델 등록 수정 후 34.00/312.78ms, 최종 572.14/876.14ms였다. 이 구간은 Windows 클라이언트·전송·스케줄링을 포함하며 개별 원인은 이 비교로 확정할 수 없다. 최종 표본 전후에는 API CPU 108–113%, DB 0.42–175.93%가 관측됐다. 표본 시점 차이가 있어 이 값을 지속 CPU 사용률로 해석할 수 없다.

## 테스트 실행과 결과

| 검증 | 명령 또는 결과 |
| --- | --- |
| 독립 RAG 표준 명령 | 깨끗한 격리 DB에서 `python -m app.agent.evaluation --prepare --phase-five`. 수정 전 `NoReferencedTableError`, 수정 후 선행 import 없이 정상 종료. Fake 질의 26개, Recall@5 96.67%, 삭제와 롤백 검증 포함. |
| 백엔드·시뮬레이션 전체 | `python -m pytest -p no:cacheprovider -q`. 모델 등록만 수정한 이미지 50개 통과, 최종 이미지 **51개 통과**. 임베디드 Windows Python을 수동 `sys.path`로 실행한 사전 진단은 자식 프로세스의 설치 경로가 상속되지 않아 4건이 실패했다. 의존성이 표준 경로에 설치된 격리 Docker pytest에서는 초기화 검증 4건이 모두 통과했다. |
| Alembic | 빈 DB에 `alembic upgrade head`로 `0005_simulation_foundation` 적용. 별도의 합성 DB에서 `alembic upgrade 0004_handover`, 이전 형식 티켓 삽입, `alembic upgrade head` 순으로 티켓 내용·상태·버전·실습 연결 없음, 발행 시드와 변경 방지 트리거를 확인했다. 최종 이미지에서 완료했다. |
| 프론트 빌드 | Windows Node 24에서 `tsc --noEmit`, `vite build` 통과. Docker에서 현재 잠금 파일로 개발 이미지 빌드는 npm `ETIMEDOUT`. 브라우저 재실행은 현재 소스 마운트와 기존 Vite 개발 이미지를 사용했다. 그 이미지의 React, React DOM, Vite, Vite React 플러그인, TypeScript 버전은 현재 명세와 일치했고 정규화한 잠금 파일에는 차이가 있었다. |
| 브라우저 중간 실행 | 첫 수정에서 Nginx 케이스는 통과했고, 묶인 업무 케이스는 티켓 등록 뒤 새로고침의 전체 `load`를 기다리다 60초에 실패했다. 이후 중간 실행에서는 업무 테스트가 화면 준비 단계에서 실패했고, Nginx 동시 로그인에 429 7건과 예상 밖 503 `DB_UNAVAILABLE` 2건이 있었다. Trace에서 오류 코드를 확인했다. DB 연결 오류가 발생한 구체적인 원인은 확인되지 않았다. |
| 최종 브라우저 회귀 | [통합 테스트 계획](TEST_PLAN.md)의 격리 회차로 `npm run test:e2e`와 같은 Playwright CLI를 실행해 Chromium **8개 통과**. 로그인 표본에서 `/login` 문서가 유지된 상태로 URL은 `/tickets`가 됐다. 티켓 목록 200(85ms), 후속 세션 200(152ms), 클릭부터 화면 준비까지 2.88초, 콘솔 오류와 스크립트 예외 0건이었다. 브라우저 뒤로 가기도 통과했다. 2.88초는 진단 표본 1건이며 5초 성능 기준의 통과 판정에는 사용하지 않았다. |
| 실제 Nginx 제한 | `python -m tests.qa_rate_limit`을 QA Nginx와 같은 Docker 네임스페이스에서 실행했다. 순차 401·200·401, 동시 18건에서 401 11건·429 7건, 동일 계정의 다른 소스 IP 200, 버킷 회복 후 동일 소스 IP 401을 확인했다. |
| 실제 Gemini 합성 | 독립 `python -m app.agent.evaluation --prepare --phase-five`와 `python -m app.handover.smoke`, 모두 종료 0. 평가 질의 26개와 `gemini-3.5-flash-lite` 인계 스모크를 별도 QA worker에서 실행했다. |
| 성능 | `python -m tests.qa_performance --origin "$APP_ORIGIN" --shifts <3개 QA 회차> --profile`. 기준·모델 등록·최종에서 각각 99개 표본과 오류 0건을 확인했다. `python -m tests.qa_profile_summary`로 각 실행의 HTTP 94개를 모두 대조했다. 500ms 목표 판정은 위 표에 기록했다. |
| 백업·복원·DB 복구 | `python -m tests.qa_persistence --project <실행 중인 QA 프로젝트> --restore-project <새 QA 프로젝트> --origin <QA 루프백 주소> --wait-timeout 240` 최종 종료 0. 잘못된 소스 포트 거부, 재시작, DB 중단과 readiness 회복, 복원, 액세스 로그 검증이 통과했다. 18개 테이블의 행 수·내용 지문과 SOP 원본 7개의 경로·다이제스트가 일치했다. 공개 `/api/v1/ready`는 404였다. 재실행 중 Docker 기본 주소 풀이 소진되어 새 복원 네트워크 생성이 실패했다. 최종 재실행은 전용 QA 브리지와 새 named volume을 사용했다. |

## Ubuntu VM 동일 부하 재측정 절차

Docker Engine, Compose 2.24.4 이상과 Python 3.12를 준비한다. 체크아웃은 VM의 Linux 파일시스템에 두고 새 QA 프로젝트 이름과 미사용 루프백 포트를 사용한다. 같은 VM, DB 이미지, 데이터 수, Fake 프로필, 동시성, API 실행 모드를 유지해 비교한다. Docker 자원, 파일시스템, 백그라운드 부하를 각 실행에 함께 기록한다. QA 계측 Compose는 API의 개발 소스 마운트와 reload를 제거하고 SOP named volume은 유지한다. 이 절차의 Ubuntu 결과는 아직 없다. [Compose `!override` 지원 버전](https://docs.docker.com/reference/compose-file/merge/#replace-value)

```bash
set -euo pipefail
export AGENT_PROVIDER=fake GEMINI_API_KEY='' APP_PORT=18085
export APP_ORIGIN="http://127.0.0.1:${APP_PORT}"
qa_project=polytech-qa-phase1-ubuntu
qa_output=$(mktemp -d)
python3.12 -m venv "$qa_output/venv"
qa_python="$qa_output/venv/bin/python"
"$qa_python" -m pip install -r requirements-dev.txt
qa=(docker compose --env-file .env.example -p "$qa_project" -f compose.yaml -f compose.override.yaml -f compose.qa-profile.yaml)

uname -a
docker info --format 'cpus={{.NCPU}} memory={{.MemTotal}} storage={{.Driver}}'
docker compose version
findmnt -T "$PWD" -o TARGET,FSTYPE,SOURCE
free -h
"${qa[@]}" up --build --wait --wait-timeout 240 nginx worker
"${qa[@]}" run --rm --no-deps -T worker python -m app.agent.evaluation --prepare --phase-five > "$qa_output/rag.json"
roster=$("${qa[@]}" run --build --rm -T test python -m tests.qa_roster --shifts 4)
mapfile -t shifts < <(printf '%s' "$roster" | "$qa_python" -c 'import json,sys; print(*json.load(sys.stdin),sep="\n")')
mapfile -t qa_containers < <("${qa[@]}" ps -q)

"$qa_python" -m tests.qa_performance --origin "$APP_ORIGIN" --shifts "${shifts[@]:0:3}" --profile > "$qa_output/performance.json" &
benchmark_pid=$!
while kill -0 "$benchmark_pid" 2>/dev/null; do
  docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.BlockIO}}' "${qa_containers[@]}" >> "$qa_output/containers.txt"
  sleep 2
done
wait "$benchmark_pid"
"${qa[@]}" logs --no-color --no-log-prefix nginx > "$qa_output/nginx.log"
"${qa[@]}" logs --no-color --no-log-prefix api > "$qa_output/api.log"
"$qa_python" -m tests.qa_profile_summary "$qa_output/performance.json" "$qa_output/nginx.log" "$qa_output/api.log" > "$qa_output/layers.json"
"${qa[@]}" stop
printf '%s\n' "$qa_output"
```

수정 전 이미지 재측정에는 기준 커밋으로 빌드한 API·worker 이미지를 사용하고, 같은 QA 계측 파일을 마운트한다. 독립 시드 명령의 수정 전 오류 때문에 고정 합성 자료 생성만 수정된 worker 이미지에서 한 번 수행한 뒤 기준 worker로 측정한다. 다음 비교에는 다른 새 QA 프로젝트를 써서 벤치마크가 남긴 티켓·보고서가 다음 부하에 섞이지 않게 한다. 두 실행에서 `uncorrelated_requests`와 표본 수를 대조한다. Gemini를 비교할 경우 별도 합성 스택에서 표준 RAG 평가와 스모크를 실행하고 공급자 지연의 p50/p95를 따로 보관한다.

## 남은 위험과 Phase 2 게이트

- Windows에서 로그인 후 티켓 목록 동작과 기능 회귀를 검증했다. 중간 로그인 부하 테스트의 503 두 건은 최종 실행에서 재현되지 않았다. 깨끗한 Docker 프론트 빌드는 npm 연결 타임아웃으로 확인하지 못했다.
- Windows 종단 간 티켓 조회 p95는 PRD 500ms를 넘었다. Ubuntu VM의 동일 부하와 배포 이미지 검증은 미실행이다. 얇은 합성 자료에서 조사한 SQL 계획만으로 상한 규모의 DB 성능을 판정할 수 없다.
- **Phase 2 진입 판정은 보류한다.** 기능 회귀는 통과했다. 배포 후보 이미지로 Ubuntu VM에서 비AI 읽기 p95와 전체 브라우저 회귀를 재검증한 뒤 진입을 판정한다. 성능 목표는 기존 PRD 기준을 적용한다.

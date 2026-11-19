# 5단계 통합 테스트 보고서

2026-11-19 (KST) · Windows 11, Docker Desktop Linux 엔진 29.9.3, Compose 5.7.0, WSL2 기반. 컨테이너는 Python 3.12, PostgreSQL 17 및 pgvector 0.8.7, Node 24, Nginx 1.30을 사용했다. 브라우저는 Playwright 1.57의 Chromium 143을 사용했다. 개발 브라우저 QA는 `127.0.0.1:18085`, 운영 조합 QA는 루프백 `18086/18443`을 사용했다.

작업 전 `main` 작업 트리는 깨끗했고 엔진에서 기존 Compose 프로젝트, 실행 컨테이너 또는 볼륨이 조회되지 않았다. 접근 가능한 Docker 컨텍스트에서 모든 검증을 새 QA 프로젝트와 합성 입력으로 시작했다. 기본 개발 주소는 `127.0.0.1:8080`이다. 검증 후 QA 컨테이너를 정지했고, QA named volume 17개를 보존했다.

## 업무와 장애 복구

| 항목 | 상태 | 실제 결과 |
| --- | --- | --- |
| 초기 PostgreSQL 베이스라인 | 통과 | 기존 테스트 31개. |
| 최종 백엔드 회귀 | 통과 | 41개. 접수부터 인수까지의 흐름, 근거 부족, 3회 교대 이월, 중복 작업과 재확인, FK·롤백, 권한과 만료 세션, 모의 Gemini 401·429·503을 포함한다. |
| 브라우저 전체 흐름 | 통과 | 로그인, 티켓·작업 등록, SOP와 해결 사례 검색, 검색 로딩 표시, 분석 결과 확인, 교대 회차 선택, 초안 생성·편집, 누락 안내, 확정 및 다른 세션의 인수 확인과 새로고침. |
| Nginx·브라우저 보안 경계 | 통과 | 외부 readiness 404, 다른 Origin의 로그인 403, CORS 응답 헤더 부재, 요청 크기 413, 로그인 제한 429, 티켓 제목의 일반 HTML 태그를 텍스트로 표기. |
| DB·파일 재시작과 복구 | 통과 | 실제 QA DB를 중단하면 인증된 티켓 조회가 503 `DB_UNAVAILABLE`, `GET /health`는 200이다. 시작 후 티켓 조회가 복구됐다. 재시작과 별도 QA 볼륨 복원 후 DB 테이블 12개에 대한 행 수·내용 지문과 원본 SOP 7개의 해시가 일치했다. 최종 복원에 티켓 32개, 스냅샷 항목 97개, 검색 청크 35개가 포함됐다. |
| 스키마와 시드 | 통과 | 빈 DB를 `0004_handover`로 마이그레이션했다. 시드 재실행은 SOP 0개, 티켓 0개를 추가했다. FK, 버전과 pgvector 768차원 확인을 통합 테스트에 포함했다. |
| 운영 Compose | 로컬 통과 | 명시적 운영 파일 조합 설정·빌드, 내부 DNS 및 컨테이너 준비 상태, Nginx TLS·HTTP 308, Secure/HttpOnly 쿠키, 정적 파일과 SPA 새로고침을 확인했다. 외부에 공개한 포트는 QA 루프백의 Nginx뿐이다. |
| 공개 데모 Nginx 이미지 | 로컬 통과 | `tunnel` 타깃 빌드와 네트워크를 분리한 컨테이너에서 `nginx -t`가 통과했다. |
| 실제 Ubuntu 호스트 | 미실행 | 로컬 Docker Desktop에서 운영 조합을 검증했다. Ubuntu 대상 호스트에서의 검증은 남아 있다. |

FastAPI 회귀 및 브라우저 워크플로는 Fake 모델을 사용했다. Git 추적 파일과 Git 이력에서 `.env`, `.env.prod`, `.env.tunnel`과 일반적인 Google API 키 패턴이 발견되지 않았다. 실제 Gemini 스택에서 키 설정 여부를 불리언으로 확인했다. API는 미설정, worker는 설정 상태였다. 도구 원문·시크릿 마스킹, 잘못된 토큰·Origin·CSRF, 따옴표와 와일드카드를 포함한 제목의 리터럴 검색, SOP 파일 형식·크기·파일명 분리, 분석 소유권 및 조별 변경 권한을 검증했다.

## 재현된 오류와 변경

| 재현 결과 | 수정과 회귀 |
| --- | --- |
| Nginx가 생성한 413 응답이 HTML이었다. 화면은 일반 오류 상태만 표시했고 요청 ID를 전달받지 못했다. | Nginx의 413과 로그인 429에 기존 JSON 오류 형식, 안내 문구 및 요청 ID를 적용했다. Playwright에서 두 분기를 확인했다. |
| 개발 React의 회차 목록 응답 두 건이 겹치면 느린 응답이 사용자가 고른 인계 회차를 기본 회차로 바꿨다. 이미 보고서가 있는 회차에 생성 요청이 들어가 409가 발생했다. | 정리된 effect의 응답을 무시한다. Playwright가 이전 응답을 의도적으로 지연시켜 선택값과 실제 요청의 인계 회차를 비교한다. |
| 합성 티켓 검색의 쿼리 문자열이 Uvicorn 및 Nginx 접근 로그에 보였다. | Nginx는 경로와 요청 ID를 기록하고 기본 Compose API의 중복 Uvicorn 접근 로그를 껐다. 최종 재실행에서 검색어가 사라지고 해당 요청 ID가 남는 것을 확인했다. |

## 실제 RAG 평가

동일한 합성 질의 26개와 별도 프로필·DB를 사용했다. 관련 질의 15개에는 SOP와 해결 티켓을 각 1건씩 기대했다. Precision@5는 검색 건수가 5보다 적은 경우에도 분모가 5다. 지연 시간은 임베딩과 pgvector 검색을 합산한 값이다.

| 방식 | Recall@5 | Precision@5 | MRR | 무관 질의 결과 없음 | p50 / p95 / 최대 |
| --- | ---: | ---: | ---: | ---: | --- |
| 실제 `gemini-embedding-2`, 768차원, 컷오프 0.62 | 1.0000 (30/30) | 0.4000 | 1.0000 | 10/11 | 433.71 / 465.77 / 483.75 ms |
| Fake `lexical-v1`, 768차원 | 0.9667 (29/30) | 0.3867 | 0.8722 | 0/11 | 4.68 / 7.37 / 7.75 ms |

실제 요청은 자료 임베딩 14회, 질의 임베딩 26회였다. 삭제한 SOP가 쿼리에서 빠지고 롤백 뒤 다시 조회되는 것을 두 프로필에서 확인했다. 존재하지 않는 오류 코드 `QZ-9741`에 무관한 SOP·티켓 5개가 반환됐다. 질의, 정답 UUID, 검색 결과·점수와 지연 원자료가 있다. [Gemini 결과](qa/rag-gemini-results.json) · [Fake 결과](qa/rag-fake-results.json)

## 실제 Gemini 생성과 도구

| 입력 | 모델 | 실제 결과 |
| --- | --- | --- |
| 합성 인수인계 티켓 한 건 | `gemini-3.8-flash` | 실패. 모델 요청 2회 모두 조사 단계에서 HTTP 504. 도구 0회. |
| 같은 합성 인수인계 | `gemini-3.7-flash` | 실패. 모델 요청 2회 모두 HTTP 504. 도구 0회. |
| 합성 장애 티켓 한 건, Nginx API | `gemini-3.7-flash` | 실패. 모델 요청 2회 후 `AI_UNAVAILABLE`. 도구 0회, 종료까지 27.41초. |
| 같은 합성 인수인계 | `gemini-3.5-flash-lite` | 통과. 실제 모델 요청 4회, 도구 3회, 공급자 토큰 5,369개, 중요 이슈 근거 3개와 후속 조치 1개가 포함된 검증된 JSON. |
| 합성 B101 장애 티켓 한 건, Nginx API | `gemini-3.5-flash-lite` | 통과. 실측 8.17초. 모델 요청 3회, 임베딩 1회, 공급자 토큰 5,372개와 인용 근거 4개. |

인수인계의 실제 도구 순서는 `get_shift_tickets` (회차 티켓 1개) → `get_ticket_details` (근거 4개) → `get_ticket_history` (근거 2개)였다. 도구 상태는 모두 `ok`였다. Flash-Lite 지정은 검증 프로젝트의 환경변수로 적용했다. [Gemini 모델 문서](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)

장애 분석의 도구 순서는 `get_ticket_details` (K1, 7ms) → `get_ticket_history` (K2, 7ms) → `search_sop` (K3, 629ms) → `search_similar_tickets` (K4, 12ms)였다. 각 도구의 상태는 `ok`이며 도구마다 근거 1개가 반환됐다. 분석 결과가 원본 티켓을 인용했다.

## 성능 실측

최종 구성의 Docker 엔진은 12 CPU와 15.44 GiB를 보고했다. Windows 호스트에서 Nginx를 경유해 티켓 생성·조회와 SOP·해결 티켓 검색을 각 20건, 최대 동시 5개 요청으로 실행했다. 각 분석은 앞선 분석 종료 후 시작했다. 100ms 간격으로 완료를 확인했다. 성능 표본 99건에서 오류는 0건이었다.

| 작업 | 표본 수 | p50 (ms) | p95 (ms) | 최대 (ms) |
| --- | ---: | ---: | ---: | ---: |
| 티켓 생성 | 20 | 53.80 | 77.87 | 89.59 |
| 티켓 조회 | 20 | 41.92 | 48.09 | 51.18 |
| SOP 검색 | 20 | 222.07 | 419.65 | 421.89 |
| 해결 티켓 검색 | 20 | 201.83 | 252.07 | 276.30 |
| Fake 장애 분석 접수 | 5 | 15.77 | 47.44 | 54.06 |
| Fake 장애 분석 완료 | 5 | 1076.70 | 1150.50 | 1162.29 |
| 인수인계 확정 | 3 | 55.54 | 55.85 | 55.88 |
| 인수인계 조회 | 3 | 22.39 | 24.00 | 24.18 |

인수인계 생성은 티켓 1·5·20개에서 각 1회씩 60.44·50.80·51.02ms였다. 단일 표본이라 p95를 계산하지 않았다. 측정 직후 `docker stats --no-stream`에서 API는 CPU 40.95%와 메모리 134.2 MiB, DB는 0.28%와 45.14 MiB, worker는 0.74%와 94.89 MiB였다. `pg_stat_activity`에 유휴 연결 7개와 측정 쿼리 1개가 확인됐다. 병목 위치와 부하 한계 판단에는 부하 중 시계열 측정이 필요하다.

## 실행 명령

환경변수와 QA 초기 자료는 [테스트 계획](TEST_PLAN.md)의 PowerShell 절차를 사용했다. Docker Desktop CLI는 설치 경로, Python은 로컬 3.12 가상환경, Playwright CLI는 Node 24로 실행했다.

```text
docker compose -p polytech-qa-20261119 -f compose.yaml run --build --rm test
docker compose -p polytech-qa-final -f compose.yaml run --build --rm test
docker compose -p polytech-qa-release -f compose.yaml run --build --rm test
docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml config --quiet
docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml up --build --wait --wait-timeout 120 nginx worker
docker compose -p polytech-qa-browser -f compose.yaml -f compose.override.yaml exec -T worker python -m app.knowledge.demo
node node_modules/@playwright/test/cli.js test
node node_modules/@playwright/test/cli.js test --grep '장애 접수'
node node_modules/@playwright/test/cli.js test --grep Nginx
python -m tests.qa_performance --shifts <합성 회차 UUID 3개>
python -m tests.qa_persistence --project polytech-qa-browser --restore-project polytech-qa-restored
python -m tests.qa_persistence --project polytech-qa-browser --restore-project polytech-qa-restored-final
docker compose -p polytech-qa-rag-fake -f compose.yaml run --build --rm worker python -m app.agent.evaluation --prepare --phase-five
docker compose --env-file .env -p polytech-qa-rag-gemini -f compose.yaml run --build --rm -T worker python -m app.agent.evaluation --prepare --phase-five
docker compose --env-file .env -p polytech-qa-gemini-smoke -f compose.yaml run --rm --no-deps -T worker python -m app.handover.smoke
python -m tests.qa_gemini_incident
docker compose -p polytech-qa-tls -f compose.yaml -f compose.prod.yaml config --quiet
docker compose -p polytech-qa-tls -f compose.yaml -f compose.prod.yaml -f .venv/compose.qa-tls.yaml up --build --wait --wait-timeout 120 nginx worker
docker exec polytech-qa-tls-nginx-1 nginx -t
docker build --target tunnel -t polytech-qa-tunnel-nginx ./frontend
docker run --rm --network none polytech-qa-tunnel-nginx nginx -t
```

TLS QA 오버라이드는 Nginx의 두 공개 바인딩을 `127.0.0.1:18086:80`, `127.0.0.1:18443:443`으로 바꿨다. IP SAN이 있는 자체 생성 인증서를 QA 클라이언트에 지정해 검증했다. 보안 및 운영 미검증 항목은 [알려진 문제](KNOWN_ISSUES.md)에 있다.

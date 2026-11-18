# AI 기반 IT 운영 지원 에이전트

## 서비스 소개

IT 운영자와 데이터센터 테크니션을 위한 웹 서비스입니다. 장애 발생부터 조치 기록과 교대 인수인계까지 한곳에서 관리할 수 있도록 설계했습니다. 가상 티켓과 표준 운영 절차(SOP)를 사용해 실제 인프라 연결 없이 운영 흐름을 시연하는 것이 목표입니다.

## 주요 기능

| 기능 | 제공 범위 |
| --- | --- |
| 장애 티켓 관리 | **구현 완료.** 로컬 운영자 로그인, 티켓 등록·검색·조회·수정·상태 전환, 종료 티켓 보관을 지원합니다. |
| 담당자·후속 작업 관리 | **예정.** 근무조와 담당자를 배정하고, 후속 작업과 운영 메모를 기록하며, 작업 이력을 확인합니다. |
| SOP 관리·장애 지식 검색 | **구현 완료.** PDF·TXT·Markdown을 등록하고, SOP와 해결 티켓을 검색해 관련 내용과 원본을 확인합니다. |
| 근거 기반 장애 분석 | **구현 완료.** Gemini가 조회 도구를 선택하고, 출처가 있는 장애 가설과 진단 단계를 제안합니다. |
| 교대 인수인계 | **예정.** 미해결 티켓, 미완료 작업, 누락 정보를 모아 보고서 초안을 만들고 근무조 책임자가 검토·승인합니다. |

## 에이전트 역할

Gemini 에이전트는 조회 전용 도구로 과거 해결 사례와 SOP를 읽고, 진단 순서와 추가 확인 질문을 제안합니다. 가능한 경우 제안마다 출처를 표시하고, 근거가 부족한 경우 불확실성과 확인 방법을 함께 제시합니다. 교대 인수인계 초안은 4단계에 계획되어 있습니다.

티켓 상태 변경과 실제 인프라 조치는 운영자가 수행하고, 인수인계 보고서는 근무조 책임자가 승인합니다. 담당자나 마감 시각처럼 조건이 명확한 누락 항목은 서버 규칙으로 검사합니다. [에이전트 설계](docs/AGENT.md)

## 현재 개발 단계

**3단계 · Gemini 장애 분석 에이전트**

1단계에서 Docker Compose, 기본 티켓 관리와 운영자 세션을 구성했습니다. 2단계에서 SOP 업로드와 색인, 해결 티켓의 자동 색인, 출처가 있는 pgvector 검색을 추가했습니다.

3단계에서 실제 Gemini 임베딩을 검증하고, 분석 화면과 조회 전용 Function Calling을 추가했습니다. 교대 운영과 인수인계는 4단계입니다. [단계별 개발 계획](docs/TASKS.md) · [제품 요구사항](docs/PRD.md)

## 개발 실행

Docker Desktop 또는 Docker Engine과 Compose가 필요하다. 저장소 루트에서 다음을 실행한다.

```sh
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/health
```

브라우저에서 [개발 화면](http://127.0.0.1:8080)을 열고 로컬용 `demo` / `local-demo-password`로 로그인한다. 포트를 바꾸려면 `APP_PORT`와 `APP_ORIGIN`을 함께 설정한다. 운영 세션 없이 호출한 티켓 API는 401을 반환한다. API 클라이언트에서는 로그인에 올바른 `Origin`을 지정하고 이후 변경 요청에 응답의 CSRF 토큰을 `X-CSRF-Token`으로 보낸다.

- Nginx는 브라우저에서 같은 출처로 React와 API를 제공한다. `GET /health`는 `{"status":"ok"}`를 반환한다.
- PostgreSQL은 내부 네트워크와 `db_data` 볼륨을 사용한다. `docker compose restart` 또는 별도 컨테이너 재생성 시 볼륨을 계속 사용한다.
- Alembic과 초기 운영자 등록은 일회성 `migrate` 서비스에서 수행한다. 기존 운영자 비밀번호는 다시 실행해도 바뀌지 않는다.
- API의 `GET /api/v1/ready`는 내부 전용이다. API 컨테이너 헬스체크는 DB 접속과 Alembic 리비전을 확인한다.
- 삭제 요청은 `closed` 티켓을 보관한다. 목록의 보관 필터로 찾을 수 있으며, 해당 티켓과 변경 이벤트는 DB에 남는다.

## SOP와 지식 검색 시연

```sh
docker compose exec -T api python -m app.knowledge.demo
docker compose exec -T api python -m app.knowledge.demo --evaluate
```

첫 명령은 7개 가상 시나리오의 SOP와 해결 티켓을 추가합니다. 반복 실행은 같은 자료를 추가하지 않습니다. 로그인 후 [SOP 문서](http://127.0.0.1:8080/knowledge)에서 처리 상태를 확인하고, 색인이 완료되면 [지식 검색](http://127.0.0.1:8080/knowledge/search)에서 ‘서버 부팅 실패와 메인보드 오류’를 검색하세요. 평가 명령은 색인 완료 뒤 시나리오별 적중 건수와 출처 단위 Recall@5를 출력합니다.

SOP만 등록하려면 `docker compose exec -T api python -m app.knowledge.demo --sop-only`를 실행합니다. 원본 파일은 API와 worker가 공유하는 `sop_files` 볼륨에, 문서 메타데이터와 검색 색인은 PostgreSQL의 `db_data` 볼륨에 저장됩니다. 컨테이너를 재생성해도 프로젝트 이름을 유지하면 두 볼륨을 다시 사용합니다. `docker compose down -v`는 두 볼륨을 삭제합니다.

`docker compose exec -T api python -m app.knowledge.demo --demo-incidents`는 네트워크, 스토리지, 메모리, 배치 처리의 유사 장애 티켓 12건을 추가합니다. 해결·종료 이력은 지식 검색의 출처가 됩니다. 진행 중인 `네트워크 링크 불안정과 패킷 손실` 티켓에서 AI 분석을 실행하면 관련 해결 이력과 SOP를 함께 확인할 수 있습니다.

개발 Compose는 기본값으로 합성 벡터를 사용합니다. Gemini를 사용하려면 [.env.example](.env.example)에 따라 로컬 `.env`에 `AGENT_PROVIDER=gemini`와 `GEMINI_API_KEY`를 설정하고 `docker compose up -d --build`로 설정을 적용합니다. 기존 자료를 새 프로필로 색인하려면 `docker compose exec -T api python -m app.knowledge.demo --reindex`를 실행합니다. API 키는 worker에 전달됩니다. [API 요청과 응답](docs/API.md)

## AI 장애 분석 시연

로그인 뒤 [AI 장애 분석](http://127.0.0.1:8080/agent)을 열거나 기존 티켓 상세 화면에서 **AI 분석**을 선택합니다. 질문을 제출하면 진행 상태, 검증된 출처와 개발자 패널의 도구 순서를 확인할 수 있습니다. 기본 개발 설정은 `fake`입니다.

실제 검증에는 Git에서 제외되는 로컬 `.env`에 API 키를 저장하고, 가상 자료 전용 Compose 프로젝트를 사용합니다. 아래는 Windows PowerShell 예시입니다.

```powershell
$env:AGENT_PROVIDER = 'gemini'
$env:APP_PORT = '18081'
$env:APP_ORIGIN = 'http://127.0.0.1:18081'
docker compose -p polytech-agent-eval up -d --build db migrate
docker compose -p polytech-agent-eval wait migrate
docker compose -p polytech-agent-eval run --build --rm --no-deps worker python -m app.agent.evaluation --prepare
docker compose -p polytech-agent-eval run --rm --no-deps worker python -m app.agent.evaluation --challenge-only
docker compose -p polytech-agent-eval up -d --build --wait
```

`--prepare`는 티켓과 SOP가 비어 있는 격리 DB를 대상으로 한다. 이후에는 `--prepare` 없이 평가만 재실행할 수 있다. 실제 Gemini 시연은 별도 포트 [18081](http://127.0.0.1:18081/agent)에서 가상 자료로 진행한다. [에이전트 동작과 품질 평가](docs/AGENT.md)

## 통합 테스트와 빌드

개발 데이터와 구분되는 Compose 프로젝트에서 PostgreSQL을 실제로 사용해 실행한다. 아래 테스트 프로젝트에는 Nginx 포트를 발행하지 않는다.

```sh
docker compose -p polytech-ticket-tests run --build --rm test
docker compose -p polytech-ticket-tests down
docker build --target build ./frontend
```

자동 테스트는 PostgreSQL 기반 기존 기능과 모의 Gemini의 다단계 선택, 근거 검증, 권한, 오류 처리를 검증한다. 실제 Gemini 임베딩과 도구 호출은 격리된 가상 자료로 검증한다. 테스트 볼륨은 반복 실행에 재사용할 수 있다.

## 운영 Compose

별도 프로젝트 이름과 환경 파일을 사용한다. `APP_ORIGIN`에 HTTPS 출처, `APP_TLS_DIR`에 `fullchain.pem`과 `privkey.pem`이 있는 디렉터리를 지정한다. `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `APP_CSRF_SECRET`(32자 이상), `APP_BOOTSTRAP_PASSWORD`(16자 이상)를 배포 환경에서 설정한다. `APP_AUTH_USERNAME`의 기본값은 `operator`다.

```sh
docker compose --env-file .env.prod -p polytech-prod -f compose.yaml -f compose.prod.yaml config --quiet
docker compose --env-file .env.prod -p polytech-prod -f compose.yaml -f compose.prod.yaml up -d --build
```

운영 Nginx는 정적 React 빌드를 제공한다. 외부 포트는 80과 443이다. 방화벽과 TLS 인증서·갱신은 대상 호스트에서 관리한다. 원본 파일은 `sop_files` 볼륨, 메타데이터와 추출 텍스트는 `db_data` 볼륨에 보존된다. 운영 설정의 임베딩 공급자 기본값은 `gemini`다. 백업 시 두 볼륨을 함께 보존한다.

## 공개 프로토타입

고정 HTTPS 주소로 체험판을 공유하려면 [Cloudflare Tunnel 배포 안내](docs/PUBLIC_DEMO.md)를 따른다. 별도 Compose 프로젝트가 전용 DB와 파일 볼륨을 만들고, 장애 분석과 지식 검색은 Fake provider를 사용한다.

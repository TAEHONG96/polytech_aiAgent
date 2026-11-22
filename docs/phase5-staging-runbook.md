# Phase 5 Ubuntu 스테이징 검증 절차

별도 배포 PC에서 실행한다. 운영 프로젝트와 다른 이름, 포트, DB·SOP 볼륨과 합성 계정을 사용한다. 문서의 예시 포트는 루프백에만 바인딩한다. 배포 서버의 운영 프로젝트를 중지하거나 변경하지 않는다.

## 커밋과 이미지 확인

배포 후보 브랜치에서 커밋을 고정한다. Windows에서 검증한 Linux 이미지와 같은 아키텍처일 때는 빌드한 하나의 이미지 번들을 Ubuntu에도 전달한다. 태그나 소스가 같아도 새 빌드의 이미지 ID를 같다고 가정하지 않는다. 아래 예시는 Ubuntu의 후보 코드와 이미지를 받기 전후 양쪽에서 이름과 이미지 ID를 확인하는 방법이다. Windows에서도 WSL이나 Git Bash(설치된 경우)에서 같은 명령을 실행할 수 있다.

```bash
set -euo pipefail
candidate=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
printf '%s\n' "$candidate"
uname -m
docker info --format 'cpus={{.NCPU}} memory={{.MemTotal}} storage={{.Driver}}'
docker compose version

# 최초 배포 후보 빌드. 이미지를 Ubuntu에서 다시 빌드한 결과는 별도 기록한다.
docker build --no-cache --target runtime -t "polytech-stage-backend:$candidate" .
docker build --no-cache --target production -t "polytech-stage-nginx:$candidate" frontend
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
  "polytech-stage-backend:$candidate" "polytech-stage-nginx:$candidate"
docker save -o phase5-images.tar "polytech-stage-backend:$candidate" "polytech-stage-nginx:$candidate"
sha256sum phase5-images.tar > phase5-images.tar.sha256

# Ubuntu에서 후보 커밋 확인 후. 전송한 번들만 사용한다.
sha256sum -c phase5-images.tar.sha256
docker load -i phase5-images.tar
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
  "polytech-stage-backend:$candidate" "polytech-stage-nginx:$candidate"
```

## 운영 구성의 TLS·가입·인증 검증

`127.0.0.1` IP SAN을 가진 스테이징 전용 테스트 인증서를 사용한다. 사용한 신뢰 루트, TLS 인증서 지문, 테스트 시각을 기록한다. 인증서의 개인 키와 환경 파일은 프로젝트 소스 및 테스트 로그에서 제외한다.

```bash
umask 077
stage_dir=$(mktemp -d)
export APP_TLS_DIR="$stage_dir/tls"
mkdir "$APP_TLS_DIR"
openssl req -x509 -newkey rsa:2048 -sha256 -days 3 -nodes -subj '/CN=Phase 5 staging' \
  -addext 'subjectAltName=IP:127.0.0.1' -keyout "$APP_TLS_DIR/privkey.pem" -out "$APP_TLS_DIR/fullchain.pem"
export APP_ORIGIN=https://127.0.0.1:18443 AGENT_PROVIDER=fake APP_TRAINING_REGISTRATION=0
export POSTGRES_PASSWORD=$(openssl rand -hex 24) APP_DB_PASSWORD=$(openssl rand -hex 24)
export APP_CSRF_SECRET=$(openssl rand -hex 32) APP_BOOTSTRAP_PASSWORD=$(openssl rand -hex 24)
export APP_AUTH_USERNAME=staging-operator

cat > "$stage_dir/stage.yaml" <<EOF
services:
  api:
    image: polytech-stage-backend:$candidate
  worker:
    image: polytech-stage-backend:$candidate
  migrate:
    image: polytech-stage-backend:$candidate
  nginx:
    image: polytech-stage-nginx:$candidate
    ports: !override
      - "127.0.0.1:18443:443"
      - "127.0.0.1:18088:80"
EOF

stage=(docker compose --env-file .env.example -p polytech-qa-phase5-stage -f compose.yaml -f compose.prod.yaml -f "$stage_dir/stage.yaml")
"${stage[@]}" config --quiet
"${stage[@]}" up --no-build --wait --wait-timeout 240 nginx worker
"${stage[@]}" exec -T nginx nginx -t
"${stage[@]}" exec -T db psql -U postgres -d polytech -Atc 'SELECT version_num FROM alembic_version;'
"${stage[@]}" exec -T api python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8000/api/v1/ready").read().decode())'
curl --silent --show-error --output /dev/null --write-out '%{http_code}\n' http://127.0.0.1:18088/health
export QA_TLS_OPERATOR="$APP_AUTH_USERNAME" QA_TLS_PASSWORD="$APP_BOOTSTRAP_PASSWORD"
python -m tests.qa_release_tls --origin "$APP_ORIGIN" --ca "$APP_TLS_DIR/fullchain.pem"
unset QA_TLS_PASSWORD
```

내부 readiness 200, 외부 readiness 404, 외부 liveness 200, HTTP 리다이렉트 308, TLS 인증서 검증, `Secure`·`HttpOnly`·`SameSite=Lax`, Origin/CSRF 거부 및 정상 처리, 외부 가입 404를 확인한다. 실제 DNS·인증서와 공개 80→443 리다이렉트의 호스트 및 경로는 도메인 전환 전 별도로 대조한다.

빈 DB의 Alembic 리비전은 `0008_training_evaluation`이다. 마이그레이션 리허설은 스테이징 내 별도 `polytech_phase5_legacy` DB 또는 합성 복제본에서 수행한다. `createdb` 후 같은 migrate 이미지에 `-e APP_DB_NAME=polytech_phase5_legacy`를 지정하고 `alembic upgrade 0007_simulation_workflows`를 실행한다. 운영 티켓·감사 행과 교육 완료 행의 대조값을 기록한 후 `alembic upgrade head` 결과와 비교한다. 운영 DB 덤프를 쓰지 않고 0007 레거시 시험용 데이터를 사용할 수 있다.

## 동일 부하 성능과 503 진단

TLS 인증 후 동일한 후보 이미지·합성 데이터로 동시성 5의 부하를 측정한다. 기존 Windows 개발 스택과 정확히 비교하려면 이 절차와 병행하여 별도 Fake 계층 프로파일에서 기존 부하를 반복한다. 단계별 측정은 [기존 프로파일 절차](test-stabilization-report.md)의 `compose.qa-profile.yaml`과 같은 명령을 쓴다. 대시보드 브라우저 시험과 백업 리허설은 성능 측정 전후에 실행한다.

```bash
"${stage[@]}" run --rm --no-deps -T worker python -m app.agent.evaluation --username "$APP_AUTH_USERNAME" --prepare --phase-five > "$stage_dir/rag.json"
qa_roster=$("${stage[@]}" run --build --rm -T test python -m tests.qa_roster --shifts 4)
mapfile -t qa_shifts < <(printf '%s' "$qa_roster" | python -c 'import json,sys; print(*json.load(sys.stdin),sep="\n")')
python -m tests.qa_performance --origin "$APP_ORIGIN" --ca "$APP_TLS_DIR/fullchain.pem" \
  --shifts "${qa_shifts[@]:0:3}" --profile --extra-handover-reads 20 > "$stage_dir/performance.json"
"${stage[@]}" logs --no-color --no-log-prefix nginx > "$stage_dir/nginx.log"
"${stage[@]}" logs --no-color --no-log-prefix api > "$stage_dir/api.log"
```

- 99개 기존 표본과 추가 인계 읽기 20개를 별도 집계한다. 비AI 티켓·인계 읽기 p95가 각 500ms 이하인지 확인하고 p50/p95, 오류 건수, 요청 ID와 컨테이너 자원 사용량을 저장한다. 별도 계층 프로파일에서는 `python -m tests.qa_profile_summary <performance.json> <nginx.log> <api.log>`로 같은 요청의 클라이언트·Nginx·FastAPI·DB·풀 대기·임베딩 시간을 비교한다.
- 운영 설정과 같은 스테이징에서 로그인 순차·동시 테스트를 정상 DB로 한 번 더 수행한다. 503을 받으면 응답의 요청 ID, `docker events`의 재시작/OOM, API의 드라이버 종류·SQLSTATE, PostgreSQL 연결 로그와 동시간대 DB 상태를 보존한다. 통제된 DB 중단으로 확인한 `DB_UNAVAILABLE`과 정상 상태의 간헐 503을 구분한다.
- 임계값을 바꾸기 전에는 승인된 양성·무관 질의의 Recall@5, 무관 후보 제거율과 인용 적합성을 함께 비교한다. 외부 임베딩 호출의 왕복 시간은 비AI 읽기와 분리한다.

## Fake 브라우저·복원과 실제 Gemini

Fake 시나리오를 새 스테이징 QA 프로젝트 아래 같은 합성 코퍼스로 준비한다. 가입은 Fake 루프백 스택에서만 허용한다. 아래 명령은 저장소 루트에서 실행하며 Python 3.12 `requirements-dev.txt`, Node 및 Chromium은 미리 준비한다.

```bash
(
  export APP_ORIGIN=http://127.0.0.1:18169 APP_PORT=18169 APP_TRAINING_REGISTRATION=1 AGENT_PROVIDER=fake
  unset GEMINI_API_KEY POSTGRES_PASSWORD APP_DB_PASSWORD APP_AUTH_USERNAME APP_BOOTSTRAP_PASSWORD APP_CSRF_SECRET
  qa=(docker compose --env-file .env.example -p polytech-qa-phase5-browser -f compose.yaml -f compose.override.yaml)
  "${qa[@]}" up --build --wait --wait-timeout 240 nginx worker
  "${qa[@]}" run --rm --no-deps -T worker python -m app.agent.evaluation --prepare --phase-five
  "${qa[@]}" run --rm --no-deps -T worker python -m app.simulation.learning_materials --operator demo
  "${qa[@]}" build test
  export QA_PHASE5_PROJECT=polytech-qa-phase5-browser QA_BASE_URL="$APP_ORIGIN"
  qa_roster=$("${qa[@]}" run --rm --no-deps -T test python -m tests.qa_roster --shifts 6)
  mapfile -t qa_shifts < <(printf '%s' "$qa_roster" | python -c 'import json,sys; print(*json.load(sys.stdin),sep="\n")')
  export QA_SHIFT_ID=${qa_shifts[0]} QA_APPROVAL_SHIFT_ID=${qa_shifts[2]} QA_EDIT_SHIFT_ID=${qa_shifts[4]}
  (cd frontend && npm ci && npx playwright install chromium && npx playwright test -c playwright.training.config.ts && npm run test:e2e)
  python -m tests.qa_persistence --project polytech-qa-phase5-browser --restore-project polytech-qa-phase5-restore \
    --origin "$APP_ORIGIN" --restore-origin http://127.0.0.1:18175 --wait-timeout 240
)
```

실제 Gemini는 새 `polytech-qa-phase5-gemini` 개발 프로젝트의 워커에서 처리한다. `APP_ORIGIN=http://127.0.0.1:18171`, `APP_PORT=18171`, `APP_TRAINING_REGISTRATION=1`, `AGENT_PROVIDER=gemini`, 승인한 모델과 worker용 키를 준비한다. 키가 들어 있는 파일이나 Compose의 전체 환경을 출력하지 않는다. 배포 대상 모델과 아래 검증에 사용한 모델이 다르면 양쪽의 결과를 별도로 기록한다.

```bash
# 별도 Gemini QA 환경 변수를 새 셸에 설정한 뒤 실행한다.
gem=(docker compose --env-file .env.example -p polytech-qa-phase5-gemini -f compose.yaml -f compose.override.yaml)
"${gem[@]}" up --build --wait --wait-timeout 240 nginx worker
export QA_GEMINI_DB_OWNER=local-owner-password  # .env.example의 합성 DB 전용
for mode in retrieval feedback; do
  "${gem[@]}" run --rm --no-deps -T -e QA_GEMINI_DB_OWNER -e PYTHONPATH=/app \
    -v "$PWD/tests/qa_phase5_gemini.py:/qa_smoke.py:ro" worker python /qa_smoke.py \
    --project polytech-qa-phase5-gemini --origin "$APP_ORIGIN" --mode "$mode"
done
unset GEMINI_API_KEY QA_GEMINI_DB_OWNER
```

복원은 기존 운영 볼륨을 건드리지 않는 새 프로젝트에서 완료 이력, 확정본, 작업·조치·학습 증거, 평가 입력·규칙·설명, 동일 멱등 응답, 만료 lease 회수, SOP 원본과 사고 벡터를 모두 확인한다. 스테이징 검증이 끝나면 브라우저 Trace, 결과 요약, 이미지 ID, 백업 지문, 성능·TLS 증빙과 함께 승인 여부를 기록한다. 운영 전환 전에 운영 자료의 백업과 복원 절차, 승인된 합성 코퍼스의 배포 임베딩 프로필 준비, 운영 DNS/TLS와 로그인 503 진단을 마친다.

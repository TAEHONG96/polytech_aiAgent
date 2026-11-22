# Phase 5 Ubuntu 스테이징 Runbook

별도 Ubuntu 배포 PC의 **합성 스테이징**에서 실행한다. 운영 서비스의 Compose·DB·DNS·TLS·방화벽을 변경하지 않는다. 예시는 `polytech-qa-phase5-stage` 프로젝트의 HTTPS만 양쪽 루프백 `127.0.0.1:18443`에 연결한다. 모든 검증 결과를 기록한 뒤 운영 배포 여부를 별도 결정한다.

## 실행 체크리스트

| 순서 | 정상 기준 | 실패 시 확인 |
| --- | --- | --- |
| 사전 점검 | 대상·로컬 Docker 컨텍스트·운영 기준선 확인, 자원 여유, 이름과 포트 충돌 없음 | `docker context inspect`, `ss`, `docker ps -a`, 네트워크·볼륨 소유 라벨, 자원 지표 |
| 후보·설정 확인 | Git 커밋, 번들 체크섬·세 이미지 ID·플랫폼 일치. 저장소 밖의 기존 `stage.env`와 TLS 재사용 | `sha256sum -c`, `git rev-parse`, `docker image inspect`, `stat`. 파일이 없고 기존 볼륨이 있으면 파일부터 복구 |
| 스테이징 기동 | Nginx만 루프백에 게시, migrate 종료 0, DB·API·worker healthy, 실행 이미지와 승인 ID 일치 | `stage ps -a`, `stage logs --tail 120 db migrate api worker nginx`, `stage port nginx 443` |
| HTTPS 및 Windows 접근 | CA 검증, HTTPS liveness 200, 내부 readiness 200·외부 404, 로그인·쿠키·CSRF 정상, 외부 가입 차단 | Nginx/TLS, DB/API 로그, SSH 호스트 키·포트포워딩 정책, URL·CA 지문 |
| 재검증 및 종료 | 동일 볼륨·기존 평가 보존. 성능·503·Gemini·복원 결과와 운영 영향 기록 | 요청 ID·DB 로그·OOM·자원 추이. 운영 영향이 생기면 부하를 끄고 스테이징만 정지 |

### 담당자가 직접 확인할 정보

- Ubuntu 주소, SSH 사용자·포트·서버 호스트 키 지문, VPN/점프호스트 필요 여부, 로컬 포트포워딩 허용 여부, 로컬 Docker 컨텍스트와 접근 권한.
- 실제 운영 Compose 프로젝트명·컨테이너·공개 포트·네트워크/서브넷·볼륨, 상태 점검 URL과 담당자, 검증 시간대 및 부하 중단 기준. 기존 스테이징이 있으면 기존 프로젝트명·환경 파일 위치·볼륨·CA 지문도 확인한다.
- 승인한 전체 Git 커밋, 후보 이미지 ID·Ubuntu CPU 아키텍처·번들 SHA-256, Ubuntu와 Windows 양쪽의 동일 HTTPS 포트 사용 가능 여부, 신뢰할 스테이징 CA와 안전한 전달 경로. 운영 최종 승인에는 공개 DNS/TLS, Gemini 모델·쿼터, 백업 담당자 정보도 필요하다. 비밀번호·API 키·개인 키 내용은 채팅에 공유하지 않는다.

## 1. Ubuntu 사전 점검

전용 Bash 셸에서 승인된 스테이징 체크아웃을 사용한다. Git, Docker CLI와 Compose, OpenSSL, curl, jq를 준비한다. Compose 2.24.4 이상이 `!override`를 지원한다. Docker Engine 28 미만은 같은 L2 네트워크에서 루프백 게시에 접근할 수 있어 아래 구성을 그대로 진행하지 않는다. 운영 호스트의 Docker 버전·방화벽·공개 프록시는 이 작업에서 바꾸지 않는다. [Docker Compose 병합](https://docs.docker.com/reference/compose-file/merge/) · [Docker 게시 포트](https://docs.docker.com/engine/network/port-publishing/)

```bash
set -euo pipefail
set +x
umask 077
repo=$(git rev-parse --show-toplevel)
stage_dir=$(realpath -m "$HOME/.local/state/polytech-phase5-stage")
case "$stage_dir/" in "$repo/"*) echo 'Git 밖의 경로를 지정하세요.' >&2; exit 1;; esac
project=polytech-qa-phase5-stage
production_project=CONFIRM_PRODUCTION_PROJECT  # 실제 운영 프로젝트명. 운영이 없는 호스트에서만 none
context=default  # 담당자가 확인한 로컬 Docker 컨텍스트
port=18443
origin="https://127.0.0.1:$port"
stage_env="$stage_dir/stage.env"
overlay="$stage_dir/compose.stage.yaml"
tls="$stage_dir/tls"
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
engine=(docker --context "$context")
test "$production_project" != CONFIRM_PRODUCTION_PROJECT && test "$production_project" != "$project" || exit 1
endpoint=$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')
[[ "$endpoint" == unix:///* ]] || { echo '이 Ubuntu 호스트의 로컬 Docker 컨텍스트가 필요합니다.' >&2; exit 1; }
engine_version=$("${engine[@]}" version --format '{{.Server.Version}}')
test "${engine_version%%.*}" -ge 28
compose_version=$("${engine[@]}" compose version --short)
compose_version=${compose_version#v}
test "$(printf '%s\n' 2.24.4 "$compose_version" | sort -V | head -n 1)" = 2.24.4

hostname; uname -m
printf 'docker_endpoint=%s engine=%s compose=%s\n' "$endpoint" "$engine_version" "$compose_version"
uptime; free -h; swapon --show
docker_root=$("${engine[@]}" info --format '{{.DockerRootDir}}')
df -h "$HOME" "$docker_root"
df -ih "$HOME" "$docker_root"
"${engine[@]}" info --format 'cpus={{.NCPU}} memory={{.MemTotal}} arch={{.Architecture}}'
"${engine[@]}" system df
"${engine[@]}" stats --no-stream
"${engine[@]}" compose ls --all
"${engine[@]}" ps -a --format '{{.Names}} {{.Status}} {{.Ports}} {{.Label "com.docker.compose.project"}}'
ss -ltn "sport = :$port"
"${engine[@]}" ps --filter "publish=$port" --format '{{.Names}} {{.Ports}}'
ip -4 route
"${engine[@]}" network ls
"${engine[@]}" volume ls
```

포트가 비어 있거나 **기존의 같은 스테이징 Nginx**가 루프백에만 게시한 상태인지 확인한다. 변경이 필요하면 Ubuntu 포트·Windows 포트·`APP_ORIGIN`을 같은 값으로 맞춘다. 같은 프로젝트 이름으로 컨테이너나 리소스가 이미 있으면 재검증 대상인지 담당자가 확인한다. Docker 기본 IPAM을 사용하고, 호스트/VPN 경로와 기존 서브넷의 충돌이 있으면 승인된 스테이징 전용 범위를 적용한 뒤 확인한다. 운영 Docker 네트워크는 변경하지 않는다.

```bash
for n in db_data sop_files; do
  if owner=$("${engine[@]}" volume inspect --format '{{index .Labels "com.docker.compose.project"}}' "${project}_$n" 2>/dev/null); then
    test "$owner" = "$project" || exit 1
  fi
done
for n in data web ingress embedding_egress; do
  if detail=$("${engine[@]}" network inspect \
    --format '{{index .Labels "com.docker.compose.project"}} {{range .IPAM.Config}}{{.Subnet}} {{end}}' \
    "${project}_$n" 2>/dev/null); then
    [[ "$detail" == "$project "* ]] || exit 1
    printf '%s %s\n' "$n" "$detail"
  fi
done
while IFS='|' read -r name label; do
  if [[ "$name" == "$project"-* || "$name" == "$project"_* ]]; then
    test "$label" = "$project" || exit 1
  fi
done < <("${engine[@]}" ps -a --format '{{.Names}}|{{.Label "com.docker.compose.project"}}')
install -d -m 0700 "$stage_dir" "$tls" "$stage_dir/evidence" "$stage_dir/bundles"
```

운영의 평시 응답·오류·Docker 자원을 기록한다. 같은 방법으로 작업 중과 이후를 비교한다. 합의한 중단 기준을 넘거나 운영 지연, 스왑 증가, OOM, 디스크 부족이 관측되면 스테이징 부하부터 중단한다.

## 2. Git·이미지 검증과 번들

승인한 전체 커밋을 사용한다. 빌드 PC의 Bash에서 깨끗한 체크아웃으로 실행한다. Windows Git Bash/WSL에서는 번들 디렉터리가 Git 밖인지 확인하고 Windows ACL도 사용자 전용으로 제한한다. 동일한 커밋을 다시 빌드하면 이미지 ID가 달라질 수 있으므로 실제 전달한 결과를 승인한다. 예시는 `linux/amd64`다. [Docker 이미지 inspect](https://docs.docker.com/reference/cli/docker/image/inspect/) · [Docker 이미지 load](https://docs.docker.com/reference/cli/docker/image/load/)

```bash
set -euo pipefail
umask 077
candidate=$(git rev-parse HEAD)  # 승인된 전체 커밋과 대조
test -z "$(git status --porcelain)"
platform=linux/amd64
bundle="$HOME/.local/state/polytech-phase5-bundles/$candidate"
repo=$(git rev-parse --show-toplevel)
case "$bundle/" in "$repo/"*) exit 1;; esac
install -d -m 0700 "$bundle"
backend="polytech-stage-backend:$candidate"
nginx="polytech-stage-nginx:$candidate"
db="polytech-stage-db:$candidate"
db_source=$(tr -d '\r' < compose.yaml | sed -n 's/^    image: \(pgvector\/pgvector:.*@sha256:[a-f0-9]\{64\}\)$/\1/p')
test -n "$db_source"
docker build --platform "$platform" --no-cache --target runtime --label "org.opencontainers.image.revision=$candidate" -t "$backend" .
docker build --platform "$platform" --no-cache --target production --label "org.opencontainers.image.revision=$candidate" -t "$nginx" frontend
docker pull --platform "$platform" "$db_source"
docker image tag "$db_source" "$db"
printf '%s\n' "$candidate" > "$bundle/git-commit.txt"
printf '%s\n' "$db_source" > "$bundle/db-source.txt"
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$backend" "$nginx" "$db" > "$bundle/images.txt"
docker image save -o "$bundle/phase5-images.tar" "$backend" "$nginx" "$db"
(cd "$bundle" && sha256sum phase5-images.tar git-commit.txt db-source.txt images.txt > SHA256SUMS)
```

호스트 키를 확인한 SSH 경로로 번들 5개 파일을 Ubuntu의 `$stage_dir/bundles/<전체 커밋>/`에 전달한다. 예시는 1절의 기본 경로를 사용한다. `SHA256SUMS`의 지문은 승인한 별도 경로로 대조한다.

```bash
ssh_target=stage-user@ubuntu.example
ssh_port=22
remote_bundle=".local/state/polytech-phase5-stage/bundles/$candidate"
ssh -p "$ssh_port" -o StrictHostKeyChecking=yes -o ForwardAgent=no "$ssh_target" \
  "umask 077; install -d -m 0700 ~/$remote_bundle"
scp -P "$ssh_port" -o StrictHostKeyChecking=yes \
  "$bundle/phase5-images.tar" "$bundle/git-commit.txt" "$bundle/db-source.txt" \
  "$bundle/images.txt" "$bundle/SHA256SUMS" "$ssh_target:$remote_bundle/"
```

Ubuntu에서는 아래를 실행한다. 같은 태그에 다른 ID가 이미 있으면 로드 전에 중단한다.

```bash
candidate=$(git -C "$repo" rev-parse HEAD)
test -z "$(git -C "$repo" status --porcelain)"
bundle="$stage_dir/bundles/$candidate"
(cd "$bundle" && sha256sum -c SHA256SUMS)
test "$(cat "$bundle/git-commit.txt")" = "$candidate"
test "$(tr -d '\r' < "$repo/compose.yaml" | sed -n 's/^    image: \(pgvector\/pgvector:.*@sha256:[a-f0-9]\{64\}\)$/\1/p')" = "$(cat "$bundle/db-source.txt")"
backend="polytech-stage-backend:$candidate"
nginx="polytech-stage-nginx:$candidate"
db="polytech-stage-db:$candidate"
platform="linux/$(case "$(uname -m)" in x86_64) echo amd64;; aarch64) echo arm64;; *) exit 1;; esac)"
test "$(wc -l < "$bundle/images.txt")" -eq 3
i=0
for image in "$backend" "$nginx" "$db"; do
  i=$((i+1))
  expected=$(sed -n "${i}p" "$bundle/images.txt")
  [[ "$expected" =~ ^sha256:[0-9a-f]{64}\ $platform$ ]]
  if current=$("${engine[@]}" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$image" 2>/dev/null); then
    test "$current" = "$expected" || { echo "후보 태그 충돌: $image" >&2; exit 1; }
  fi
done
"${engine[@]}" image load -i "$bundle/phase5-images.tar"
"${engine[@]}" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$backend" "$nginx" "$db" | diff -u "$bundle/images.txt" -
for image in "$backend" "$nginx"; do
  test "$("${engine[@]}" image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" = "$candidate"
done
```

정상은 체크섬 성공, `diff` 출력 없음, 승인 커밋과 네이티브 플랫폼 일치다. 불일치하면 다른 이미지로 대체하거나 에뮬레이션으로 성능을 판정하지 않는다.

## 3. 비밀번호와 스테이징 TLS의 영구 보관

아래 생성은 **최초 한 번**만 한다. `stage.env`가 없는데 컨테이너나 볼륨이 이미 있으면 기존 값을 안전한 백업에서 복구한다. PostgreSQL의 `POSTGRES_PASSWORD` 및 초기화 스크립트의 `APP_DB_PASSWORD`는 빈 볼륨을 초기화할 때 적용된다. 환경 파일을 다시 생성해도 기존 역할 암호는 바뀌지 않는다. `APP_BOOTSTRAP_PASSWORD`도 기존 운영자 계정의 비밀번호를 덮어쓰지 않는다.

```bash
if [[ ! -e "$stage_env" && ! -L "$stage_env" ]]; then
  leftovers=$({
    "${engine[@]}" ps -aq --filter "label=com.docker.compose.project=$project"
    "${engine[@]}" volume ls -q --filter "label=com.docker.compose.project=$project"
  })
  test -z "$leftovers" || { echo '기존 stage.env를 먼저 복구하세요.' >&2; exit 1; }
  owner_pw=$(openssl rand -hex 24)
  runtime_pw=$(openssl rand -hex 24)
  operator_pw=$(openssl rand -hex 24)
  csrf_secret=$(openssl rand -hex 32)
  env_tmp=$(mktemp "$stage_dir/.stage.env.XXXXXX")
  printf '%s\n' \
    "POSTGRES_PASSWORD=$owner_pw" "APP_DB_PASSWORD=$runtime_pw" \
    "APP_BOOTSTRAP_PASSWORD=$operator_pw" "APP_CSRF_SECRET=$csrf_secret" \
    'APP_AUTH_USERNAME=staging-operator' 'AGENT_PROVIDER=fake' 'GEMINI_API_KEY=' \
    'APP_TRAINING_REGISTRATION=0' "APP_ORIGIN=$origin" "APP_TLS_DIR=$tls" > "$env_tmp"
  ln "$env_tmp" "$stage_env"                 # 경합 시 기존 파일을 덮어쓰지 않는다
  rm -- "$env_tmp"
  unset owner_pw runtime_pw operator_pw csrf_secret
fi
test -f "$stage_env" && test ! -L "$stage_env" && test -O "$stage_env"
test "$(stat -c %a "$stage_env")" = 600
grep -Fqx "APP_ORIGIN=$origin" "$stage_env"
grep -Fqx "APP_TLS_DIR=$tls" "$stage_env"
```

영구 디렉터리는 700, 환경 파일과 개인 키는 600으로 관리한다. 비밀값을 승인된 암호화 보관소에 백업한다. 셸의 동명 변수가 환경 파일보다 우선하므로 4절 래퍼를 사용한다. 전체 `docker inspect`·`docker compose config`·환경 덤프를 로그에 남기지 않는다. Docker 소켓 사용자는 컨테이너 환경을 읽을 수 있으니 권한을 제한한다. [Compose 보간 우선순위](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/) · [Compose config](https://docs.docker.com/reference/cli/docker/compose/config/)

승인된 스테이징 CA와 `IP:127.0.0.1` SAN 서버 인증서를 설치한다. 테스트 CA 생성이 허용되고 기존 인증서가 없는 경우에만 다음 블록을 한 번 실행한다. 루트 개인 키는 Nginx 마운트 경로 밖에 둔다.

```bash
test -z "$(find "$tls" -mindepth 1 -maxdepth 1 -print -quit)"
test ! -e "$stage_dir/qa-ca.key"
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 90 \
  -subj '/CN=Polytech Phase 5 Staging QA CA' \
  -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -keyout "$stage_dir/qa-ca.key" -out "$tls/qa-ca.pem"
openssl req -newkey rsa:2048 -nodes -subj '/CN=127.0.0.1' -keyout "$tls/privkey.pem" -out "$tls/leaf.csr"
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'subjectAltName=IP:127.0.0.1' \
  'keyUsage=critical,digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' > "$tls/leaf.ext"
openssl x509 -req -in "$tls/leaf.csr" -CA "$tls/qa-ca.pem" -CAkey "$stage_dir/qa-ca.key" -CAcreateserial \
  -sha256 -days 30 -extfile "$tls/leaf.ext" -out "$tls/fullchain.pem"
```

기동과 재검증 전에 아래를 실행한다. 정상은 인증서 `OK`, 인증서와 개인 키의 공개키 일치, 유효기간 이내다. 인증서나 키가 불일치하면 해당 스테이징 TLS 파일만 갱신한다. Windows로 전달할 파일은 공개 `qa-ca.pem`뿐이다. 출력된 SHA-256은 별도 신뢰 경로로 전달한다.

```bash
test "$(stat -c %a "$tls/privkey.pem")" = 600
openssl verify -purpose sslserver -verify_ip 127.0.0.1 -CAfile "$tls/qa-ca.pem" "$tls/fullchain.pem"
test "$(openssl x509 -in "$tls/fullchain.pem" -pubkey -noout)" = "$(openssl pkey -in "$tls/privkey.pem" -pubout)"
openssl x509 -in "$tls/fullchain.pem" -noout -dates -fingerprint -sha256
sha256sum "$tls/qa-ca.pem"
```

## 4. 격리 설정 검사와 기동

오버레이도 저장소 밖에 유지한다. 이미 있으면 해당 파일을 검토하고 승인된 후보가 바뀐 경우에만 이미지 태그를 갱신한다. `container_name`, 공유 네트워크·볼륨, 운영 포트를 추가하지 않는다. 스테이징은 HTTPS만 게시한다. 운영 Nginx HTTP 리다이렉트는 포트를 포함하지 않아 비표준 스테이징 포트에는 그대로 사용할 수 없다.

```bash
if [[ ! -e "$overlay" && ! -L "$overlay" ]]; then
  (set -o noclobber; cat > "$overlay" <<EOF
services:
  db:
    image: $db
  api:
    image: $backend
    build: !reset null
  worker:
    image: $backend
    build: !reset null
  migrate:
    image: $backend
    build: !reset null
  nginx:
    image: $nginx
    build: !reset null
    ports: !override
      - "127.0.0.1:$port:443"
EOF
  )
fi
test -f "$overlay" && test ! -L "$overlay"
stage() {
  env -u COMPOSE_FILE -u COMPOSE_PROJECT_NAME -u COMPOSE_PROFILES -u COMPOSE_ENV_FILES \
    -u COMPOSE_REMOVE_ORPHANS -u COMPOSE_IGNORE_ORPHANS \
    -u POSTGRES_PASSWORD -u APP_DB_PASSWORD -u APP_CSRF_SECRET -u APP_BOOTSTRAP_PASSWORD \
    -u APP_AUTH_USERNAME -u APP_TRAINING_REGISTRATION -u APP_ORIGIN -u APP_TLS_DIR \
    -u AGENT_PROVIDER -u GEMINI_API_KEY -u GEMINI_EMBEDDING_MODEL -u GEMINI_GENERATION_MODEL \
    "${engine[@]}" compose --project-directory "$repo" --env-file "$stage_env" -p "$project" \
    -f "$repo/compose.yaml" -f "$repo/compose.prod.yaml" -f "$overlay" "$@"
}
stage config --quiet
stage config --format json | jq -e --arg p "$project" --arg port "$port" \
  --arg backend "$backend" --arg nginx "$nginx" --arg db "$db" --arg origin "$origin" '
  .name == $p
  and .services.db.image == $db and .services.nginx.image == $nginx
  and ([.services.api, .services.worker, .services.migrate] | all(.image == $backend))
  and (.services.nginx.ports | length == 1 and
       .[0].host_ip == "127.0.0.1" and (.[0].published | tostring) == $port and .[0].target == 443)
  and ([.services.db, .services.api, .services.worker, .services.migrate] | all((.ports // []) | length == 0))
  and ([.services.db, .services.api, .services.worker, .services.migrate, .services.nginx]
       | all(.container_name == null and .network_mode == null and .build == null))
  and (.services.db.volumes | any(.type == "volume" and .source == "db_data" and .target == "/var/lib/postgresql/data"))
  and ([.services.api, .services.worker] | all(.volumes | length == 1 and
       .[0].type == "volume" and .[0].source == "sop_files" and .[0].target == "/var/lib/polytech/sops"))
  and .services.api.environment.APP_ORIGIN == $origin
  and (.services.api.environment.APP_TRAINING_REGISTRATION | tostring) == "0"
  and .services.worker.environment.AGENT_PROVIDER == "fake"
  and ((.services.worker.environment.GEMINI_API_KEY // "") == "")
  and (.volumes | to_entries | all(.value.name == ($p + "_" + .key) and (.value.external != true)))
  and (.networks | to_entries | all(.value.name == ($p + "_" + .key) and (.value.external != true)))
'
stage config --images
stage up --no-build --pull never --wait --wait-timeout 240 nginx worker
stage ps -a
stage port nginx 443
```

정상은 설정 검사 `true`, `127.0.0.1:18443` 한 줄, DB·API·worker healthy, migrate 종료 코드 0이다. 실패 시 `stage logs --tail 120 db migrate api worker nginx`를 확인한다. 포트는 `ss`와 게시 라벨, 네트워크 할당은 호스트 라우팅과 Docker IPAM을 대조한다. `--build` 또는 `pull`로 후보 이미지 불일치를 우회하지 않는다.

```bash
for svc in db migrate api worker nginx; do
  cid=$(stage ps --all --quiet "$svc")
  [[ "$cid" =~ ^[a-f0-9]{12,64}$ ]]
  test "$("${engine[@]}" container inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")" = "$project"
  case "$svc" in db) line=3;; nginx) line=2;; *) line=1;; esac
  expected=$(sed -n "${line}p" "$bundle/images.txt")
  actual=$("${engine[@]}" container inspect --format '{{.Image}}' "$cid")
  test "$actual" = "${expected%% *}"
  test "$("${engine[@]}" image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' "$actual")" = "$expected"
  printf '%s %s\n' "$svc" "$expected"
  "${engine[@]}" container inspect --format '{{.Name}} {{range $name, $net := .NetworkSettings.Networks}}{{$name}} {{end}}{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}:{{.Destination}} {{end}}{{end}}' "$cid"
done | tee "$stage_dir/evidence/running-images-and-resources.txt"
```

자동 이름의 컨테이너와 스테이징 접두사 네트워크, `polytech-qa-phase5-stage_db_data` 및 `polytech-qa-phase5-stage_sop_files`를 확인한다. 운영 리소스가 보이면 중단한다.

## 5. Ubuntu HTTPS와 Windows SSH 포트포워딩

```bash
stage exec -T nginx nginx -t
stage exec -T db sh -ec 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U postgres -d polytech -Atc "SELECT version_num FROM alembic_version"'
stage exec -T api python -c 'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8000/api/v1/ready", timeout=5).read().decode())'
curl --fail --silent --show-error --cacert "$tls/qa-ca.pem" "$origin/health"
curl --silent --show-error --cacert "$tls/qa-ca.pem" -o /dev/null -w '%{http_code}\n' "$origin/api/v1/ready"
curl --fail --silent --show-error --cacert "$tls/qa-ca.pem" "$origin/api/v1/auth/registration"
```

정상은 `0008_training_evaluation`, 내부 `{"status":"ready"}`, HTTPS `{"status":"ok"}`, 외부 readiness 404, 가입 `{"enabled":false}`다. DB 암호 불일치는 `stage logs --tail 100 db migrate api`에서 확인한다. `pg_isready` 단독 결과는 인증 검증이 아니다. TLS 실패는 `stage logs --tail 100 nginx`, 마운트 파일 권한, CA 및 SAN 검증 결과를 확인한다.

Windows PowerShell에서 승인된 Ubuntu 호스트 키 지문을 `known_hosts`와 대조한다. 처음 연결은 담당자가 확인해 준 지문과 일치할 때만 등록한다. Ubuntu와 Windows가 같은 HTTPS 포트를 써야 `APP_ORIGIN`과 브라우저 URL이 일치한다. `localhost` 대신 `127.0.0.1`을 사용한다. [OpenSSH 포워딩](https://man.openbsd.org/ssh) · [OpenSSH 포워딩 오류 처리](https://man.openbsd.org/ssh_config#ExitOnForwardFailure)

```powershell
$StageSsh = 'stage-user@ubuntu.example'
$StageSshPort = 22
$StageHttpsPort = 18443
Get-NetTCPConnection -State Listen -LocalPort $StageHttpsPort -ErrorAction SilentlyContinue
ssh.exe -p $StageSshPort -N -T -o StrictHostKeyChecking=yes -o ExitOnForwardFailure=yes -o ForwardAgent=no -o ServerAliveInterval=30 -L "127.0.0.1:${StageHttpsPort}:127.0.0.1:${StageHttpsPort}" $StageSsh
```

창을 연 채 별도 PowerShell에서 공개 CA의 SHA-256을 Ubuntu에서 받은 값과 대조하고 접속한다. 브라우저 테스트에 필요한 경우에만 승인된 스테이징 CA를 현재 사용자 인증서 저장소에 등록한다. `curl.exe --cacert`는 시스템 신뢰를 변경하지 않는다. [Windows 인증서 가져오기](https://learn.microsoft.com/ko-kr/powershell/module/pki/import-certificate)

```powershell
$StageOrigin = 'https://127.0.0.1:18443'
$StageCA = Join-Path $env:LOCALAPPDATA 'PolytechPhase5\qa-ca.pem'
# 필요 시 Ubuntu에서 공개 CA만 복사한다. 실제 SSH 대상과 포트로 바꾼다.
# New-Item -ItemType Directory -Force (Split-Path -Parent $StageCA) | Out-Null
# scp.exe -P 22 -o StrictHostKeyChecking=yes 'stage-user@ubuntu.example:.local/state/polytech-phase5-stage/tls/qa-ca.pem' $StageCA
Get-FileHash -Algorithm SHA256 -LiteralPath $StageCA
curl.exe --fail --silent --show-error --cacert $StageCA "$StageOrigin/health"
# 지문을 확인했고 브라우저 신뢰 정책이 허용한 경우에만 실행한다.
# $StageDer = [IO.Path]::ChangeExtension($StageCA, '.cer')
# $StageBase64 = (Get-Content -LiteralPath $StageCA -Raw) -replace '-----BEGIN CERTIFICATE-----|-----END CERTIFICATE-----|\s', ''
# [IO.File]::WriteAllBytes($StageDer, [Convert]::FromBase64String($StageBase64))
# $StageImportedCA = Import-Certificate -FilePath $StageDer -CertStoreLocation Cert:\CurrentUser\Root
Start-Process "$StageOrigin/login"
```

정상은 `curl.exe`가 `{"status":"ok"}`를 반환하고 브라우저가 TLS 경고 없이 로그인하는 상태다. 로컬 bind 실패는 `Get-NetTCPConnection`, `administratively prohibited`는 SSH 정책, `connection refused`는 Ubuntu의 `stage port`와 liveness를 확인한다. 호스트 키 변경이나 인증서 오류를 우회하지 않는다. 테스트용으로 새로 가져온 CA는 종료 시 그 지문으로만 제거하거나 담당자 정책을 따른다. `Ctrl+C`는 터널만 종료한다.

Ubuntu 저장소의 Python 3.12 가상환경에 `requirements-dev.txt`를 설치하고 다음을 실행한다. 초기 계정의 암호가 바뀌었다면 현재 스테이징 전용 암호를 입력한다.

```bash
(
  export QA_TLS_OPERATOR=staging-operator
  read -r -s -p '스테이징 운영 계정 비밀번호: ' QA_TLS_PASSWORD; printf '\n'
  export QA_TLS_PASSWORD
  python -m tests.qa_release_tls --origin "$origin" --ca "$tls/qa-ca.pem"
)
```

정상은 가입 차단, 로그인, `Secure`·`HttpOnly`·`SameSite=Lax`, Origin과 CSRF 결과가 모두 `passed`인 상태다. 403은 Origin·CSRF, 429는 터널에서 공유되는 로그인 IP 제한, 503은 요청 ID와 DB 로그로 확인한다. 공개 DNS·운영 CA·80→443 리다이렉트는 별도 승인 항목이다.

## 6. 재시작·재검증·배포 게이트

- 새 셸에서는 기존 경로와 래퍼를 다시 사용하고 후보·포트·볼륨을 검증한다. `stage up --no-build --pull never --wait --wait-timeout 240 nginx worker`로 기존 상태를 재사용한다. 같은 설정의 단순 재시작은 `stage restart api worker nginx` 뒤 `up --wait`로 확인한다. 장애 시 `stage stop`으로 스테이징만 멈춘다. `down -v`와 `volume prune`은 사용하지 않는다.
- 재시작 전후 동일 DB·SOP 볼륨과 `simulation_runs` 완료 건수·`simulation_evaluations` 건수를 대조한다. 완료 이력이 있는 스테이징에서는 같은 계정으로 대표 완료 평가 API도 비교한다. 변경 전에는 스테이징을 백업한다. 레거시 0007→0008 업그레이드와 백업 복원은 별도의 합성 QA DB 및 새 복원 볼륨에서 수행한다. [레거시 검증](phase4-evaluation-report.md) · [합성 복원 결과](phase5-release-readiness-report.md)
- 부하는 승인 시간대에 수행한다. 이미 준비된 코퍼스는 재사용하고 매 실행마다 새 합성 근무 회차를 만든다. `--prepare`는 깨끗한 합성 DB에서 처음 한 번만 사용한다. 99개 기존 표본과 추가 20개 인계 읽기를 분리해 기록한다. 티켓·인계 p95가 각각 500ms 이하인지 판정하고 시험 전후 운영 지표를 비교한다. [동일 부하와 계층 계측](test-stabilization-report.md)

```bash
# 재시작 전후 같은 명령으로 수치를 대조한다.
stage exec -T db psql -U postgres -d polytech -Atc "SELECT count(*) FROM simulation_runs WHERE status='COMPLETED'; SELECT count(*) FROM simulation_evaluations;"

# 최초 빈 합성 DB에서만 실행한다. 기존 코퍼스가 있으면 생략한다.
stage run --rm --no-deps --pull never -T worker python -m app.agent.evaluation \
  --username staging-operator --prepare --phase-five > "$stage_dir/evidence/rag.json"

roster=$(stage run --build --rm --no-deps -T test python -m tests.qa_roster --shifts 4)
mapfile -t shifts < <(printf '%s' "$roster" | jq -er '.[]')
test "${#shifts[@]}" -eq 4
python -m tests.qa_performance --origin "$origin" --ca "$tls/qa-ca.pem" \
  --shifts "${shifts[@]:0:3}" --profile --extra-handover-reads 20 > "$stage_dir/evidence/performance.json"
stage logs --since 20m --no-color --no-log-prefix nginx api db > "$stage_dir/evidence/staging-requests.log"
```

정상 DB에서 간헐 503이 나오면 UTC 시각·요청 ID로 `stage logs --since 15m nginx api db`의 API 드라이버 오류, SQLSTATE 및 DB 접속 기록을 대조한다. `docker events --since <UTC 시작> --until <UTC 종료> --filter label=com.docker.compose.project=polytech-qa-phase5-stage`와 컨테이너의 `.State.OOMKilled`도 확인한다. 통제 DB 중단과 `tests.qa_rate_limit`은 별도 HTTP Fake QA에서 재현한다.

가입이 필요한 교육 Playwright, 기존 운영 기능 브라우저 회귀, DB/SOP 복원 리허설은 Windows 개발 PC 또는 승인된 별도 Fake QA에서 수행한다. 루프백 QA에서만 가입을 켜고 `QA_PHASE5_PROJECT`에 해당 QA 프로젝트명, `QA_BASE_URL`에 QA HTTP 출처를 지정한다. 새 합성 회차를 할당한 후 프론트 디렉터리에서 `npx playwright test -c playwright.training.config.ts`와 `npm run test:e2e`를 실행한다. 새 복원 프로젝트를 지정한 `python -m tests.qa_persistence --project <합성 프로젝트> --restore-project <새 프로젝트> --origin <HTTP 루프백> --restore-origin <별도 HTTP 루프백> --wait-timeout 240`으로 완료 API, 확정 멱등 응답, Worker lease와 SOP 원본을 확인한다. [브라우저 QA 환경·합성 회차](TEST_PLAN.md) · [Phase 5 시험 결과](phase5-release-readiness-report.md)

실제 Gemini는 별도 `polytech-qa-phase5-gemini` 계열 프로젝트에서 검증한다. 별도 QA 환경 파일은 저장소 밖 600으로 관리하고 키는 worker에만 전달한다. 승인 SOP와 사고를 목표 임베딩 프로필로 색인하고 `tests/qa_phase5_gemini.py`의 `retrieval`과 `feedback` 결과, 모델, 실제 인용을 기록한다. 이 QA 도구는 합성 fixture의 벡터를 반입하므로 스테이징이나 운영 DB에 사용하지 않는다. [Gemini 결과 및 근거](phase5-release-readiness-report.md)

운영 배포 승인에는 실제 Ubuntu의 500ms 판정, 공개 DNS/TLS, 간헐 503 조사, 별도 복원 결과와 운영 영향 검토의 증빙이 필요하다.

# Ubuntu 배포와 복구

운영 조합은 `compose.yaml`과 `compose.prod.yaml`이다. Ubuntu 호스트에 Docker Engine과 Compose plugin을 설치하고 Docker 서비스 및 운영 도메인의 DNS를 확인한다. Nginx에만 80/443을 허용한다. 80은 HTTPS로 연결을 넘기고, PostgreSQL·FastAPI·worker는 Compose 내부 네트워크를 사용한다. 개발 조합은 `compose.override.yaml`을 사용하며 기본 접속 주소는 `http://127.0.0.1:8080`이다. [Ubuntu Docker Engine 설치](https://docs.docker.com/engine/install/ubuntu/)

## 초기 배포 및 업데이트

1. 승인된 리비전을 Ubuntu 배포 경로에 준비한다. DNS와 TLS 인증서의 호스트 이름을 맞춘다.
2. `.env.prod`를 배포 계정만 읽을 수 있도록 만들고 아래 설정을 채운다. `APP_TLS_DIR`에는 `fullchain.pem`과 `privkey.pem`이 필요하다. 운영 초기값을 기존 DB의 비밀번호와 다르게 바꾸면 DB 내 역할 비밀번호도 별도로 갱신해야 한다.
3. 기존 배포를 갱신할 때는 아래 절차로 쓰기를 멈추고 백업한다. 같은 프로젝트 이름과 볼륨을 사용해 새 리비전을 시작한다.

| 변수 | 값 |
| --- | --- |
| `APP_ORIGIN` | HTTPS 운영 출처. 예를 들어 `https://ops.example.org` |
| `APP_TLS_DIR` | 호스트의 인증서 디렉터리 절대 경로 |
| `POSTGRES_PASSWORD`, `APP_DB_PASSWORD` | 서로 다른 PostgreSQL 소유자 및 런타임 암호 |
| `APP_CSRF_SECRET` | 무작위 32자 이상 |
| `APP_AUTH_USERNAME`, `APP_BOOTSTRAP_PASSWORD` | 초기 계정 이름과 16자 이상 암호. 초기 계정 이름의 기본값은 `operator` |
| `AGENT_PROVIDER`, `GEMINI_API_KEY` | 운영은 `gemini`와 worker용 키. 격리 검증은 `fake` |
| `GEMINI_GENERATION_MODEL`, `GEMINI_EMBEDDING_MODEL` | 사용할 수 있는 모델 이름을 배포 계정에서 확인한다. 임베딩 모델을 변경하면 기존 자료를 다시 색인한다. |

```bash
umask 077
chmod 600 .env.prod
dc() { docker compose --env-file .env.prod -p polytech-prod -f compose.yaml -f compose.prod.yaml "$@"; }
dc config --quiet
dc up --build --wait --wait-timeout 180
dc ps -a
dc exec -T nginx nginx -t
dc exec -T api python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/api/v1/ready', timeout=3).read().decode())"
dc exec -T worker python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8001/health', timeout=3).read().decode())"
curl --fail --silent --show-error https://ops.example.org/health
```

`migrate`의 종료 코드 0과 DB·API·worker의 health를 확인한다. 외부 `/health`의 본문은 `{"status":"ok"}`이고 외부 `/api/v1/ready`는 404다. 브라우저에서 로그인 후 직접 URL 새로고침, 티켓 조회, 검색, 인수인계 화면을 확인한다. Gemini 검증에는 합성 입력 한 건을 사용한다. 인증서 갱신 후에는 `dc exec -T nginx nginx -s reload`로 재로드한다. 로그는 `dc logs --since 15m nginx api worker`에서 요청 ID와 상태를 확인한다.

## 일관된 백업

서비스 중단 시간에 API와 worker를 정지하면 파일과 DB의 시점을 맞출 수 있다. 백업 디렉터리는 배포 계정만 접근하도록 하고, 보관소에 이중으로 저장한다. 다음 명령은 Ubuntu Bash에서 실행한다. [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)

```bash
umask 077
backup_dir="backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
dc stop api worker
dc exec -T db pg_dump --format=custom --username=postgres --dbname=polytech > "$backup_dir/polytech.dump"
dc run --rm --no-deps -T worker tar -C /var/lib/polytech/sops -cf - . > "$backup_dir/sops.tar"
dc start api worker
(cd "$backup_dir" && sha256sum polytech.dump sops.tar > SHA256SUMS)
```

두 백업 명령의 종료 코드가 0인지 확인하고 실패한 파일은 폐기한다. 새 백업은 복구 리허설 뒤 복구에 사용할 수 있는 상태로 표시한다.

## 별도 볼륨 복원 리허설

같은 이미지와 인증서를 사용하는 `.env.recovery`를 만들고 별도 Compose 프로젝트와 테스트용 도메인을 지정한다. 검증은 비어 있는 프로젝트·볼륨에서 시작한다. 운영 포트가 이미 사용 중이면 별도 호스트 또는 테스트용 포트 바인딩을 사용한다. [Compose 프로젝트 이름](https://docs.docker.com/compose/how-tos/project-name/) · [PostgreSQL pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

```bash
recovery() { docker compose --env-file .env.recovery -p polytech-recovery -f compose.yaml -f compose.prod.yaml "$@"; }
(cd "$backup_dir" && sha256sum -c SHA256SUMS)
recovery up -d --wait db
recovery exec -T db pg_restore --exit-on-error --single-transaction \
  --username=postgres --dbname=polytech < "$backup_dir/polytech.dump"
recovery run --rm --no-deps -T worker tar -C /var/lib/polytech/sops -xf - < "$backup_dir/sops.tar"
recovery up --build --wait --wait-timeout 180
```

초기 PostgreSQL 컨테이너는 `app_runtime` 역할을 만든다. 복원은 DB 객체 소유권과 권한을 함께 적용한다. 복구 스택에서 마이그레이션 헤드, 대표 티켓·작업 이력·확정 보고서 및 과거 버전, 768차원 벡터 검색, SOP 다운로드를 확인한다. Ubuntu 실제 호스트 검증 및 이력이 있는 데이터에서의 복원 범위는 [테스트 보고서](TEST_REPORT.md)에 기록한다.

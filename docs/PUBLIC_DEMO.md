# Cloudflare Tunnel 공개 프로토타입

Cloudflare에 등록된 도메인과 인터넷에 연결된 Docker 호스트를 사용한다. Cloudflare가 브라우저 HTTPS를 처리하고, 터널 컨테이너는 전용 Compose 네트워크에서 Nginx로 전달한다. 모든 컨테이너의 호스트 포트는 미발행 상태다. [Cloudflare Tunnel 설정](https://developers.cloudflare.com/tunnel/get-started/)

1. Cloudflare의 **Networking → Tunnels**에서 원격 관리 터널을 만든다. Published application route에 공개 호스트 이름(예 `demo.example.com`)을 등록하고 서비스 URL을 `http://nginx:80`으로 설정한다. 브라우저의 HTTP 요청은 Cloudflare에서 HTTPS로 리디렉션한다.
2. `install -m 600 .env.tunnel.example .env.tunnel`로 배포 환경 파일을 처음 한 번 만든다. `APP_ORIGIN`에는 슬래시로 끝나지 않는 실제 HTTPS 주소를, `TUNNEL_TOKEN`에는 생성한 터널의 실행 토큰을 넣는다. `APP_BOOTSTRAP_PASSWORD`, `APP_CSRF_SECRET`, `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`는 각각 `openssl rand -hex 32`로 생성한 서로 다른 값으로 설정한다.
3. 다음 명령을 배포 호스트의 저장소 루트에서 실행한다.

   ```sh
   docker compose --env-file .env.tunnel -p polytech-demo -f compose.yaml -f compose.tunnel.yaml config --quiet
   docker compose --env-file .env.tunnel -p polytech-demo -f compose.yaml -f compose.tunnel.yaml up -d --build --wait
   docker compose --env-file .env.tunnel -p polytech-demo -f compose.yaml -f compose.tunnel.yaml exec -T api python -m app.knowledge.demo
   ```

4. 외부 네트워크에서 공개 HTTPS 주소의 로그인 화면과 `/health`를 확인한다. 브라우저에서 데모 계정으로 로그인하고 지식 검색 및 장애 첨부 없는 합성 티켓 생성·분석을 확인한다. 계정 없이 티켓 API에 접근하면 401, `/api/v1/ready`에 접근하면 404를 반환한다. 터널 연결 상태는 Cloudflare 대시보드에서 확인한다.

## 체험 계정

첫 공개 프로토타입에는 기존 로그인에 연결된 데모 계정을 권장한다. HTTPS 주소를 공유하고 임의 생성한 비밀번호는 승인된 검토자에게 개별 전달한다. 이 계정으로 티켓·문서를 수정하고 보고서를 승인할 수 있으므로 합성 데이터만 사용한다. 같은 계정을 쓰는 검토자는 데이터와 작업 이력을 공유한다. 기존 사용자의 비밀번호는 환경 파일에서 값을 바꾸고 재시작해도 갱신되지 않는다.

검토자별 작업 이력이 필요해지면 초대 기반 개별 계정을 도입한다. 불특정 방문자가 직접 체험해야 하는 경우에는 읽기 전용 둘러보기나 방문자별 샌드박스를 먼저 제공한다.

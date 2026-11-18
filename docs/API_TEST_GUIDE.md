# API 테스트 가이드

1단계에서 제공하는 FastAPI를 로컬 Docker Compose 환경에서 확인하는 문서입니다. 상태 확인, 운영자 세션, 티켓 CRUD와 상태 전이를 다룹니다. SOP 검색, Gemini 분석, 교대 인수인계의 API 설계는 [REST API 계약](API.md)에 있습니다.

## API 소개

기본 주소는 `http://127.0.0.1:8080`입니다. 업무 API는 `/api/v1` 아래에 있으며 요청과 응답은 JSON을 사용합니다. 티켓 상세 경로에는 응답의 UUID `id`를 사용합니다. 티켓 번호 `number`는 목록 검색에 사용할 수 있습니다.

| 요청 | 용도 | 성공 응답 |
| --- | --- | --- |
| `GET /health` | API 프로세스 상태 확인 | `200`, `{"status":"ok"}` |
| `GET /api/v1/ready` | DB 연결과 Alembic 리비전 확인 | API 컨테이너 내부에서 `200`, `{"status":"ready"}` |
| `POST /api/v1/auth/login` | 로컬 운영자 로그인 | `200`, 사용자 정보와 `csrf_token`, 세션 쿠키 |
| `GET /api/v1/auth/session` | 현재 세션 조회 | `200`, 사용자 정보와 `csrf_token` |
| `POST /api/v1/auth/logout` | 세션 폐기 | `204`, 빈 본문 |
| `POST /api/v1/tickets` | 티켓 생성 | `201`, 생성된 티켓과 `Location` 헤더 |
| `GET /api/v1/tickets` | 티켓 목록·검색 | `200`, `items`와 `next_cursor` |
| `GET /api/v1/tickets/{id}` | 티켓 상세 조회 | `200`, 티켓과 현재 버전 |
| `PATCH /api/v1/tickets/{id}` | 티켓 필드 수정 | `200`, 수정된 티켓 |
| `POST /api/v1/tickets/{id}/transitions` | 티켓 상태 변경 | `200`, 상태와 버전이 갱신된 티켓 |
| `DELETE /api/v1/tickets/{id}?expected_version={version}` | 종료된 티켓 보관 | `204`, 빈 본문 |

## 테스트 방법

아래 PowerShell 예제를 저장소 루트에서 순서대로 실행합니다. 가상 티켓 한 건을 생성하고 마지막에 보관합니다. 개발 포트를 바꾼 경우 `$base`에 설정된 `APP_ORIGIN` 값을 사용합니다.

### 1. 실행과 상태 확인

```powershell
docker compose up -d --build
$base = 'http://127.0.0.1:8080'
Invoke-RestMethod "$base/health"
```

예상 응답은 HTTP `200`과 `{"status":"ok"}`입니다. DB까지 준비됐는지 확인하려면 API 컨테이너에서 다음 명령을 실행합니다. Nginx를 통해 `/api/v1/ready`에 접근하면 `404`가 반환됩니다.

```powershell
docker compose exec -T api python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/api/v1/ready').read().decode())"
```

### 2. 로그인과 세션 확인

로컬 기본 계정은 `demo` / `local-demo-password`입니다. 로그인에서는 `Origin`을 보내고, 이후 조회에는 로그인 쿠키를 사용합니다. 데이터 변경과 로그아웃에는 같은 쿠키에 더해 `Origin`과 `X-CSRF-Token`이 필요합니다.

```powershell
$loginBody = @{ username = 'demo'; password = 'local-demo-password' } | ConvertTo-Json
$login = Invoke-RestMethod "$base/api/v1/auth/login" -Method Post `
  -ContentType 'application/json; charset=utf-8' -Body $loginBody `
  -Headers @{ Origin = $base } -SessionVariable apiSession

$writeHeaders = @{ Origin = $base; 'X-CSRF-Token' = $login.csrf_token }
Invoke-RestMethod "$base/api/v1/auth/session" -WebSession $apiSession

function Send-ApiJson {
    param([string]$Method, [string]$Path, [hashtable]$Body)
    Invoke-WebRequest "$base$Path" -UseBasicParsing -Method $Method `
      -WebSession $apiSession -Headers $writeHeaders `
      -ContentType 'application/json; charset=utf-8' -Body ($Body | ConvertTo-Json) `
      -TimeoutSec 10 -ErrorAction Stop
}
```

두 인증 요청의 예상 상태는 `200`입니다. 응답에서 `user.username`은 `demo`, `roles`에는 `operator`가 들어 있습니다. 로그인 응답의 `Set-Cookie`가 `$apiSession`에 저장됩니다.

### 3. 티켓 생성, 조회, 수정

```powershell
$createdResponse = Send-ApiJson POST '/api/v1/tickets' @{
    title = 'API test ' + [guid]::NewGuid().ToString('N')
    description = 'Synthetic storage latency'
    service = 'storage'
    severity = 'P2'
    detected_at = '2026-11-18T09:00:00+09:00'
}
$createdResponse.StatusCode
$createdResponse.Headers['Location']
$ticket = $createdResponse.Content | ConvertFrom-Json
$ticketPath = "/api/v1/tickets/$($ticket.id)"

Invoke-RestMethod "$base$ticketPath" -WebSession $apiSession
Invoke-RestMethod "$base/api/v1/tickets?q=$($ticket.number)&limit=10" -WebSession $apiSession

$updated = (Send-ApiJson PATCH $ticketPath @{
    expected_version = $ticket.version
    severity = 'P1'
}).Content | ConvertFrom-Json
$updated | Select-Object number, severity, status, version
```

- 생성은 `201`을 반환합니다. 응답에 UUID `id`와 `INC-000001` 형식의 `number`가 있으며, 초기 `status`는 `open`, `version`은 `1`입니다. `detected_at`은 `2026-11-18T00:00:00Z`로 반환됩니다. 실제 티켓 번호는 DB의 발급 순서에 따라 달라집니다.
- 상세와 목록은 `200`을 반환합니다. 목록은 `q`, `status`, `severity`, `scope`로 필터링할 수 있습니다. 다음 페이지가 있으면 같은 필터와 함께 `next_cursor`를 `cursor`로 전달합니다.
- 수정은 `200`을 반환합니다. 예제의 `severity`는 `P1`, `version`은 `2`가 됩니다. 이후 변경에는 최신 버전을 사용합니다.

같은 티켓의 이전 버전으로 한 번 더 수정해 충돌 응답을 확인할 수 있습니다.

```powershell
try {
    Send-ApiJson PATCH $ticketPath @{
        expected_version = $ticket.version
        severity = 'P3'
    } | Out-Null
    throw 'Expected HTTP 409'
} catch {
    if ($null -eq $_.Exception.Response -or [int]$_.Exception.Response.StatusCode -ne 409) { throw }
    $_.ErrorDetails.Message
}
```

예상 응답은 `409 VERSION_CONFLICT`입니다. 다음은 오류 본문 예시입니다. `request_id`는 요청마다 생성되며 응답의 `X-Request-ID` 헤더와 일치합니다.

```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "티켓이 변경되었습니다.",
    "details": {"current_version": 2},
    "request_id": "d7d3c658-5725-47e7-8440-3bfda83c9b6e"
  }
}
```

### 4. 해결, 종료, 보관

```powershell
$resolved = (Send-ApiJson POST "$ticketPath/transitions" @{
    expected_version = $updated.version
    to_status = 'resolved'
    reason = 'Test review'
    resolution = 'Synthetic incident resolved'
}).Content | ConvertFrom-Json

$closed = (Send-ApiJson POST "$ticketPath/transitions" @{
    expected_version = $resolved.version
    to_status = 'closed'
    reason = 'Test complete'
}).Content | ConvertFrom-Json

$archive = Invoke-WebRequest "${base}${ticketPath}?expected_version=$($closed.version)" `
  -UseBasicParsing -Method Delete -WebSession $apiSession -Headers $writeHeaders
$archive.StatusCode

Invoke-RestMethod "$base/api/v1/tickets?q=$($ticket.number)" -WebSession $apiSession
Invoke-RestMethod "$base/api/v1/tickets?scope=archived&q=$($ticket.number)" -WebSession $apiSession

(Invoke-WebRequest "$base/api/v1/auth/logout" -UseBasicParsing -Method Post `
  -WebSession $apiSession -Headers $writeHeaders).StatusCode
```

상태 전이는 각각 `200`입니다. 보관은 `204`이며 기본 목록의 `items`는 비고, `scope=archived` 목록에서 `archived_at`이 채워진 티켓을 찾을 수 있습니다. 보관 뒤에도 UUID 상세 조회는 가능합니다. 로그아웃은 `204`이며 같은 세션으로 다시 상세를 조회하면 `401`이 반환됩니다.

## 예상 오류

| 상태·오류 코드 | 재현 조건 | 확인할 항목 |
| --- | --- | --- |
| `401 AUTH_REQUIRED` | 로그인 쿠키 없이 티켓 조회 또는 로그아웃 후 재조회 | 로그인 및 같은 웹 세션 사용 |
| `401 INVALID_CREDENTIALS` | 올바른 `Origin`과 틀린 사용자명 또는 비밀번호로 로그인 | 로컬 계정 정보 |
| `403 ORIGIN_DENIED` | 로그인 또는 인증된 변경 요청에서 `Origin` 누락·불일치 | 스킴·호스트·포트가 포함된 `APP_ORIGIN` |
| `403 CSRF_INVALID` | 올바른 쿠키와 `Origin`으로 변경하면서 토큰 생략 | 로그인 또는 세션 조회의 `csrf_token` |
| `404 TICKET_NOT_FOUND` | 존재하지 않는 유효한 UUID로 조회 | 등록 응답의 `id` |
| `409 VERSION_CONFLICT` | 현재 티켓에 이전 버전으로 수정 요청 | `details.current_version` 또는 상세 재조회 |
| `409 INVALID_TRANSITION` | `open`에서 바로 `closed`로 전환 | 상세 응답의 `allowed_transitions` |
| `409 TICKET_NOT_CLOSED` | 현재 버전인 미종료 티켓을 삭제 요청 | 해결·종료 전이 후 보관 |
| `409 TICKET_ARCHIVED` | 보관된 티켓 수정 또는 상태 전환 | `archived_at` 값 |
| `422 VALIDATION_ERROR` | 필수 필드 누락, 잘못된 UUID, 오프셋 없는 시각, 해결 요약 누락 | `details`의 필드 위치와 사유 |
| `422 INVALID_CURSOR` | 기존 커서를 다른 필터에서 재사용 | 기존 필터로 요청하거나 첫 페이지부터 조회 |
| `429` | 로그인 요청이 짧은 시간에 반복됨 | Nginx가 제한한 응답에는 FastAPI JSON 오류 형식이 없을 수 있음 |
| `503 DB_UNAVAILABLE` | API가 요청 처리 중 DB에 연결하지 못함 | `docker compose ps` 및 `docker compose logs db api` |

## 자동 통합 테스트

격리된 Compose 프로젝트에서 실제 PostgreSQL을 사용합니다. 세션과 CSRF, 티켓 CRUD와 상태 전이, 보관, 변경 이력, 입력 검증, 커서, 동시 수정, DB 권한을 확인합니다.

```powershell
docker compose -p polytech-ticket-tests run --build --rm test python -m pytest -p no:cacheprovider -q tests/test_health.py tests/test_tickets.py
```

예상 결과는 기존 health 테스트를 포함한 `6 passed`입니다. 추가 실행·배포 안내는 [README](../README.md)에 있습니다. 2단계 업로드와 검색은 [SOP와 지식 검색](API.md#sop와-지식-검색)을 참고하세요.

# FastAPI 상태 확인 API

`GET /health`는 HTTP 200과 JSON `{"status":"ok"}`를 반환합니다.

## 로컬 실행

Python 3.10 이상이 필요합니다. 아래 명령은 프로젝트 루트에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

## Docker Compose 실행

```bash
docker compose up --build
```

## 응답 확인

서버를 실행한 뒤 다른 터미널에서 확인합니다.

```bash
curl -fsS http://127.0.0.1:8000/health
```

```json
{"status":"ok"}
```

## 테스트

로컬 가상 환경을 활성화한 뒤 실행합니다. 서버를 별도로 실행할 필요는 없습니다.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

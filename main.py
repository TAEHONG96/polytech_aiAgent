from fastapi import FastAPI

from app.auth import router as auth_router
from app.db import router as readiness_router
from app.http import install_http_handlers
from app.tickets import router as ticket_router

app = FastAPI(title="IT 운영 지원", version="0.1.0")
install_http_handlers(app)
app.include_router(readiness_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(ticket_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

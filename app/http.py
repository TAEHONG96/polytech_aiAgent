import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | list | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def install_http_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request: Request, error: ApiError, headers=None):
        return JSONResponse(
            status_code=error.status,
            headers=headers,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                    "request_id": request.state.request_id,
                }
            },
        )

    @app.exception_handler(ApiError)
    async def application_error(request: Request, error: ApiError):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        details = [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return error_response(request, ApiError(422, "VALIDATION_ERROR", "입력을 확인하세요.", details))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException):
        status = error.status_code
        return error_response(
            request, ApiError(status, HTTP_CODES.get(status, "HTTP_ERROR"), str(error.detail)), error.headers
        )

    @app.exception_handler(OperationalError)
    async def database_error(request: Request, error: OperationalError):
        route = getattr(request.scope.get("route"), "path", "-")
        logger.warning("db_unavailable request_id=%s route=%s driver_type=%s sqlstate=%s invalidated=%s",
                       request.state.request_id, route, type(error.orig).__name__,
                       getattr(error.orig, "sqlstate", None), error.connection_invalidated)
        return error_response(request, ApiError(503, "DB_UNAVAILABLE", "DB에 연결할 수 없습니다."))

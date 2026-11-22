import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from secrets import token_urlsafe
from typing import Annotated
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import delete, func, select

from app.db import Database
from app.http import ApiError
from app.models import LoginSession, User
from app.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_COOKIE = "polytech_session"
SESSION_TTL = timedelta(hours=8)
password_hasher = PasswordHasher()


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$",
    )]
    password: Annotated[str, StringConstraints(min_length=12, max_length=128)]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_value(token: str) -> str:
    return hmac.digest(settings().csrf_secret.encode(), token.encode(), "sha256").hex()


def require_origin(request: Request) -> None:
    if request.headers.get("origin") != settings().origin:
        raise ApiError(403, "ORIGIN_DENIED", "요청 출처를 확인하세요.")


def require_user(request: Request, db: Database) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = db.scalar(
            select(User)
            .join(LoginSession, LoginSession.user_id == User.id)
            .where(
                LoginSession.token_hash == token_hash(token),
                LoginSession.expires_at > datetime.now(timezone.utc),
                User.is_active.is_(True),
            )
        )
        if user is not None:
            return user
    raise ApiError(401, "AUTH_REQUIRED", "로그인이 필요합니다.")


CurrentUser = Annotated[User, Depends(require_user)]


def require_operator(user: CurrentUser) -> None:
    if user.role != "operator":
        raise ApiError(403, "OPERATOR_REQUIRED", "운영 계정으로 로그인하세요.")


def require_write_user(request: Request, user: CurrentUser) -> User:
    require_origin(request)
    expected = csrf_value(request.cookies[SESSION_COOKIE]).encode()
    provided = request.headers.get("X-CSRF-Token", "").encode()
    if not hmac.compare_digest(expected, provided):
        raise ApiError(403, "CSRF_INVALID", "세션을 새로 고친 뒤 다시 시도하세요.")
    return user


WriteUser = Annotated[User, Depends(require_write_user)]


def session_payload(user: User, token: str) -> dict:
    return {
        "user": {"id": user.id, "username": user.username, "display_name": user.display_name},
        "roles": [user.role],
        "memberships": [],
        "csrf_token": csrf_value(token),
    }


@lru_cache
def dummy_password_hash() -> str:
    return password_hasher.hash(token_urlsafe(32))


@router.get("/registration")
def registration_status(response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": settings().training_registration}


@router.post("/register", status_code=201)
def register(body: RegistrationInput, request: Request, response: Response, db: Database) -> dict:
    if not settings().training_registration:
        raise ApiError(404, "NOT_FOUND", "요청한 경로를 찾을 수 없습니다.")
    require_origin(request)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie and db.scalar(select(LoginSession.token_hash).where(
        LoginSession.token_hash == token_hash(cookie), LoginSession.expires_at > datetime.now(timezone.utc),
    )):
        raise ApiError(409, "ALREADY_AUTHENTICATED", "로그아웃 후 가입하세요.")
    username = body.username.casefold()
    user_id = db.scalar(select(func.register_training_user(uuid4(), username, body.username,
                                                           password_hasher.hash(body.password))))
    if user_id is None:
        raise ApiError(409, "USERNAME_TAKEN", "이미 사용 중인 사용자명입니다.")
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"id": user_id, "username": username, "display_name": body.username}


@router.post("/login")
def login(body: LoginInput, request: Request, response: Response, db: Database) -> dict:
    require_origin(request)
    user = db.scalar(select(User).where(User.username == body.username.casefold(), User.is_active.is_(True)))
    stored_hash = user.password_hash if user is not None else dummy_password_hash()
    try:
        password_hasher.verify(stored_hash, body.password)
    except (VerificationError, InvalidHashError) as exc:
        raise ApiError(401, "INVALID_CREDENTIALS", "사용자명이나 비밀번호를 확인하세요.") from exc
    if user is None:
        raise ApiError(401, "INVALID_CREDENTIALS", "사용자명이나 비밀번호를 확인하세요.")

    previous_token = request.cookies.get(SESSION_COOKIE)
    if previous_token:
        db.execute(
            delete(LoginSession).where(LoginSession.token_hash == token_hash(previous_token))
        )
    now = datetime.now(timezone.utc)
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
    token = token_urlsafe(32)
    db.add(LoginSession(token_hash=token_hash(token), user_id=user.id, created_at=now, expires_at=now + SESSION_TTL))
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        SESSION_COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True,
        secure=settings().secure_cookie, samesite="lax",
    )
    return session_payload(user, token)


@router.get("/session")
def session(request: Request, response: Response, user: CurrentUser) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return session_payload(user, request.cookies[SESSION_COOKIE])


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Database, user: WriteUser) -> None:
    db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash(request.cookies[SESSION_COOKIE])))
    db.commit()
    response.delete_cookie(SESSION_COOKIE, secure=settings().secure_cookie, httponly=True, samesite="lax")

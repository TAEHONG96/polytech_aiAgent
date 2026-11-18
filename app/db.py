from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.http import ApiError
from app.settings import ROOT, settings

router = APIRouter()


@lru_cache
def engine():
    return create_engine(
        settings().database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"connect_timeout": 3, "options": "-c timezone=UTC"},
    )


def get_db() -> Iterator[Session]:
    with Session(engine(), expire_on_commit=False) as session:
        yield session


Database = Annotated[Session, Depends(get_db)]


def lock_ticket_snapshot(db: Session) -> None:
    # Ticket and schedule writes share this transaction lock with handover capture.
    db.execute(text("SELECT pg_advisory_xact_lock(821437602134)"))


@lru_cache
def expected_schema() -> frozenset[str]:
    config = Config(ROOT / "alembic.ini")
    return frozenset(ScriptDirectory.from_config(config).get_heads())


@router.get("/ready")
def ready(db: Database) -> dict[str, str]:
    try:
        versions = frozenset(db.scalars(text("SELECT version_num FROM alembic_version")))
    except SQLAlchemyError as exc:
        raise ApiError(503, "DB_NOT_READY", "데이터베이스 준비 상태를 확인하세요.") from exc
    if versions != expected_schema():
        raise ApiError(503, "SCHEMA_NOT_READY", "데이터베이스 스키마 버전이 일치하지 않습니다.")
    return {"status": "ready"}

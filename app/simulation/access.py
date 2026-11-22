from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, WriteUser
from app.db import Database
from app.http import ApiError
from app.models import User
from app.simulation.models import SimulationRun


def find_run(db: Session, run_id: UUID, user_id: UUID, *, write: bool = False) -> SimulationRun:
    statement = select(SimulationRun).where(SimulationRun.id == run_id, SimulationRun.user_id == user_id)
    if write:
        statement = statement.with_for_update()
    run = db.scalar(statement)
    if run is None:
        raise ApiError(404, "SIMULATION_RUN_NOT_FOUND", "실습 실행을 찾을 수 없습니다.")
    if write and run.status != "ACTIVE":
        raise ApiError(409, "SIMULATION_COMPLETED", "완료된 실습은 수정할 수 없습니다.")
    return run


def owned_run(run_id: UUID, db: Database, user: CurrentUser) -> SimulationRun:
    return find_run(db, run_id, user.id)


def writable_run(run_id: UUID, db: Database, user: WriteUser) -> SimulationRun:
    return find_run(db, run_id, user.id, write=True)


OwnedRun = Annotated[SimulationRun, Depends(owned_run)]
WritableRun = Annotated[SimulationRun, Depends(writable_run)]


def worker_scope(db: Session, user_id: UUID, run_id: UUID | None) -> None:
    role = db.scalar(select(User.role).where(User.id == user_id, User.is_active.is_(True)))
    if role is None or (run_id is None and role != "operator"):
        raise PermissionError("Invalid analysis requester")
    if run_id is not None:
        try:
            find_run(db, run_id, user_id)
        except ApiError as exc:
            raise PermissionError("Invalid analysis scope") from exc

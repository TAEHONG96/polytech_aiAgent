"""Provision synthetic accounts and contiguous shifts in a disposable QA database."""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auth import password_hasher
from app.db import engine, lock_ticket_snapshot
from app.handover.models import ShiftInstance, ShiftMembership, ShiftTeam
from app.models import User

PASSWORD = "synthetic-qa-password"
USERNAMES = ("qa-outgoing", "qa-incoming")


def provision(count: int) -> list[str]:
    if os.environ.get("RUN_POSTGRES_TESTS") != "1" or os.environ.get("AGENT_PROVIDER") != "fake":
        raise SystemExit("Use the test service of an isolated Fake QA Compose project.")
    owner_url = engine().url.set(username="postgres", password=os.environ["TEST_DB_OWNER_PASSWORD"])
    admin = create_engine(owner_url, hide_parameters=True)
    try:
        with Session(admin) as db, db.begin():
            lock_ticket_snapshot(db)
            now = datetime.now(timezone.utc)
            earliest = db.scalar(select(func.min(ShiftInstance.starts_at)))
            start = min(earliest or now, now) - timedelta(hours=8 * count + 1)
            teams = [ShiftTeam(name=f"QA {role} {uuid4().hex[:8]}") for role in ("인계조", "인수조")]
            db.add_all(teams)
            password_hash = password_hasher.hash(PASSWORD)
            for username, team in zip(USERNAMES, teams):
                user = db.scalar(select(User).where(User.username == username))
                if user is None:
                    user = User(username=username, display_name=username, is_active=True,
                                password_hash=password_hash, created_at=now)
                    db.add(user)
                db.flush()
                db.add(ShiftMembership(team_id=team.id, user_id=user.id, is_lead=True))
            shifts = [ShiftInstance(team_id=teams[index % 2].id, timezone="Asia/Seoul",
                                    starts_at=start + timedelta(hours=8 * index),
                                    ends_at=start + timedelta(hours=8 * (index + 1))) for index in range(count)]
            db.add_all(shifts)
            db.flush()
            return [str(shift.id) for shift in shifts]
    finally:
        admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shifts", type=int, choices=range(2, 10), default=2)
    args = parser.parse_args()
    print(json.dumps(provision(args.shifts)))

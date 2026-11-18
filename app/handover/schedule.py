"""Provision shift rosters and explicit periods with the migration database role."""

import argparse
from datetime import datetime, timezone
from getpass import getpass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import password_hasher
from app.db import engine, lock_ticket_snapshot
from app.handover.models import ShiftInstance, ShiftMembership, ShiftTeam
from app.models import User
from app.tickets import require_timestamp


def add_user(db: Session, args) -> User:
    username = args.username.strip().casefold()
    if not username or len(username) > 64 or not 1 <= len(args.name.strip()) <= 100:
        raise ValueError("사용자명과 표시 이름의 길이를 확인하세요.")
    user = db.scalar(select(User).where(User.username == username))
    if user is not None:
        if not user.is_active:
            raise ValueError("비활성 계정은 근무조에 배정할 수 없습니다.")
        return user
    password = getpass("운영자 비밀번호: ")
    if not 16 <= len(password) <= 256:
        raise ValueError("비밀번호는 16~256자로 입력하세요.")
    user = User(username=username, display_name=args.name.strip(), password_hash=password_hasher.hash(password),
                is_active=True, created_at=datetime.now(timezone.utc))
    db.add(user)
    return user


def add_team(db: Session, args) -> ShiftTeam:
    name = args.name.strip()
    if not 1 <= len(name) <= 80:
        raise ValueError("근무조 이름을 1~80자로 입력하세요.")
    users = {name.casefold(): lead for lead, names in ((False, args.member), (True, args.lead)) for name in names}
    team = db.scalar(select(ShiftTeam).where(ShiftTeam.name == name))
    if team is None:
        team = ShiftTeam(name=name)
        db.add(team)
        db.flush()
    for username, is_lead in users.items():
        user = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if user is None:
            raise ValueError(f"활성 운영자를 찾을 수 없습니다. {username}")
        db.merge(ShiftMembership(team_id=team.id, user_id=user.id, is_lead=is_lead))
    return team


def parse_period(start: str, end: str, zone_name: str) -> tuple[datetime, datetime]:
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("IANA 시간대 이름을 확인하세요.") from exc
    times = [datetime.fromisoformat(require_timestamp(value).upper()) for value in (start, end)]
    if any(point.utcoffset() != point.astimezone(zone).utcoffset() for point in times):
        raise ValueError("입력 시간의 UTC 오프셋이 근무 회차 시간대와 다릅니다.")
    starts_at, ends_at = (point.astimezone(timezone.utc) for point in times)
    if starts_at >= ends_at:
        raise ValueError("종료 시각은 시작 시각보다 늦어야 합니다.")
    return starts_at, ends_at


def add_shift(db: Session, args) -> ShiftInstance:
    team = db.scalar(select(ShiftTeam).where(ShiftTeam.name == args.team))
    if team is None:
        raise ValueError("먼저 근무조를 등록하세요.")
    start, end = parse_period(args.start, args.end, args.timezone)
    existing = db.scalar(select(ShiftInstance).where(ShiftInstance.starts_at == start))
    if existing and (existing.team_id, existing.ends_at, existing.timezone) == (team.id, end, args.timezone):
        return existing
    overlaps = db.scalar(select(ShiftInstance.id).where(ShiftInstance.starts_at < end, ShiftInstance.ends_at > start).limit(1))
    if overlaps:
        raise ValueError("등록된 근무 회차와 시간이 겹칩니다.")
    shift = ShiftInstance(team_id=team.id, starts_at=start, ends_at=end, timezone=args.timezone)
    db.add(shift)
    return shift


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    user = commands.add_parser("add-user")
    user.add_argument("username")
    user.add_argument("name")
    user.set_defaults(handler=add_user)
    team = commands.add_parser("add-team")
    team.add_argument("name")
    team.add_argument("--member", action="append", default=[])
    team.add_argument("--lead", action="append", required=True)
    team.set_defaults(handler=add_team)
    shift = commands.add_parser("add-shift")
    shift.add_argument("team")
    shift.add_argument("start")
    shift.add_argument("end")
    shift.add_argument("--timezone", default="Asia/Seoul")
    shift.set_defaults(handler=add_shift)
    args = parser.parse_args()
    try:
        with Session(engine()) as db, db.begin():
            if args.handler != add_user:
                lock_ticket_snapshot(db)
            item = args.handler(db, args)
            db.flush()
            identifier = item.id
    except (ValueError, IntegrityError) as exc:
        parser.error(str(exc) if isinstance(exc, ValueError) else "등록한 데이터가 근무 일정 제약에 위배됩니다.")
    print(identifier)


if __name__ == "__main__":
    main()

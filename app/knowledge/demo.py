"""Synthetic RAG fixtures, reindexing, and source-level Recall@5."""

import argparse
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine, lock_ticket_snapshot
from app.knowledge.documents import register_sop
from app.knowledge.embedding import embedding_profile
from app.knowledge.indexing import RESOLVED_STATUSES, enqueue_sop, now_utc, sync_ticket_index
from app.knowledge.models import SopDocument, SourceType
from app.knowledge.search import search_index, worker_query_vector
from app.models import Ticket, User
from app.tickets import TicketStatus, apply_changes, record_event


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    symptoms: str
    checks: str
    resolution: str
    query: str

    @property
    def sop_title(self) -> str:
        return f"[가상 SOP] {self.title}"

    @property
    def ticket_title(self) -> str:
        return f"[가상 티켓] {self.title}"


SCENARIOS = [
    Scenario(
        "boot", "서버 부팅 실패와 메인보드 오류",
        "모의 서버에서 POST 단계 부팅 실패와 시스템 보드 진단 오류 B101이 표시됐다.",
        "메인보드 오류 코드와 POST 진단 결과를 시각 순으로 기록한다. 같은 증상을 재현한 가상 이력과 비교한다.",
        "가상 시스템 보드 교체 후 POST 진단과 두 번의 부팅 시험이 정상으로 기록됐다.",
        "서버 부팅 실패와 메인보드 오류, POST 진단 테스트 실패",
    ),
    Scenario(
        "memory", "메모리 DIMM ECC 오류",
        "모의 작업 중 DIMM 슬롯 B2에서 ECC 교정 오류가 연달아 증가했다.",
        "ECC 카운터, DIMM 슬롯, 메모리 진단 결과를 비교하고 같은 슬롯에 집중되는지 확인한다.",
        "가상 DIMM 교체 후 ECC 오류 카운터가 더 증가하지 않았고 메모리 진단을 통과했다.",
        "메모리 DIMM 슬롯에서 반복되는 ECC 교정 오류",
    ),
    Scenario(
        "disk", "디스크 SMART 장애와 RAID 성능 저하",
        "가상 스토리지에서 SMART 경고가 발생하고 RAID 배열이 저하 상태로 전환됐다.",
        "SMART 상태, RAID 구성원, 오류 발생 디스크 번호와 백업 상태를 확인한다.",
        "가상 디스크 교체와 RAID 재구축 완료 뒤 배열 상태와 읽기 검증이 정상으로 돌아왔다.",
        "RAID 배열 저하와 디스크 SMART 경고 발생",
    ),
    Scenario(
        "network", "네트워크 링크 불량과 패킷 손실",
        "가상 서비스 포트에서 링크가 반복해서 끊기고 패킷 손실 카운터가 증가했다.",
        "스위치 포트 링크 상태, CRC 오류와 패킷 손실을 기록한 뒤 모의 케이블 교체 기록과 비교한다.",
        "가상 연결 경로 교체 뒤 네트워크 링크 유지와 패킷 손실 감소를 확인했다.",
        "네트워크 링크가 끊기고 CRC 오류와 패킷 손실 발생",
    ),
    Scenario(
        "bmc", "BMC 관리 인터페이스 접근 실패",
        "가상 관리 화면의 BMC 응답 지연이 반복되고 모의 관리 포트의 상태가 비활성으로 표시됐다.",
        "BMC 관리 포트와 가상 관리망 구성값을 대조하고 승인된 점검 기록에서 연결 상태를 비교한다.",
        "모의 관리망 구성 교정 후 BMC 관리 화면 상태를 정상으로 확인했다.",
        "BMC 관리 인터페이스 접근 실패와 관리 포트 비활성",
    ),
    Scenario(
        "cpu", "CPU 사용률 급증과 프로세스 과부하",
        "가상 관측 데이터에서 CPU 사용률이 높아지고 배치 프로세스 처리 시간이 증가했다.",
        "CPU 사용률과 상위 프로세스, 대기열 및 배치 시작 시각을 함께 비교한다.",
        "모의 배치의 동시 실행량을 조정한 뒤 CPU 사용률과 대기열이 안정화됐다.",
        "CPU 사용률 급증과 배치 프로세스 처리 지연",
    ),
    Scenario(
        "power", "전원 공급 장치 PSU 이중화 경고",
        "가상 장비에서 전원 공급 장치 PSU B가 비활성 상태로 표시되고 이중화 경고가 발생했다.",
        "PSU 상태, 전원 입력 이력, 이중화 경고 시각과 영향 범위를 확인한다.",
        "가상 전원 공급 장치 복구 뒤 양쪽 PSU 정상 상태와 이중화 재개를 기록했다.",
        "전원 공급 장치 PSU 비활성과 이중화 경고",
    ),
]


@dataclass(frozen=True)
class DemoIncident:
    key: str
    service: str
    title: str
    symptoms: str
    next_action: str
    severity: str
    status: TicketStatus
    resolution: str | None = None


DEMO_INCIDENTS = [
    DemoIncident("net-01", "SIM-EDGE", "네트워크 링크 재연결과 CRC 오류", "가상 스위치 포트의 CRC 카운터가 증가하고 5분 간격으로 링크가 끊겼다.", "교체 후 포트 카운터와 링크 유지 시간을 확인한다.", "P2", TicketStatus.RESOLVED, "모의 케이블 교체 후 링크가 유지됐고 CRC 카운터 증가가 멈췄다."),
    DemoIncident("net-02", "SIM-EDGE", "네트워크 링크 불안정과 패킷 손실", "가상 API 요청 일부가 시간 초과됐고 스위치에서 링크 다운과 CRC 오류가 함께 관측됐다.", "스위치 포트와 연결 경로별 CRC 카운터를 대조한다.", "P1", TicketStatus.IN_PROGRESS),
    DemoIncident("net-03", "SIM-EDGE", "업링크 패킷 손실과 큐 드롭", "부하가 높은 시간에 패킷 손실이 발생했다. 링크 다운이나 CRC 오류는 관측되지 않았다.", "피크 시간의 송신 큐 점유율과 드롭 카운터를 수집한다.", "P3", TicketStatus.OPEN),
    DemoIncident("disk-01", "SIM-STORAGE", "디스크 SMART 경고와 RAID 배열 성능 저하", "모의 백업 작업 중 SMART 경고가 반복되고 RAID 배열이 저하 상태로 전환됐다.", "배열 상태와 백업 읽기 검증 결과를 확인한다.", "P2", TicketStatus.CLOSED, "가상 디스크 교체와 RAID 재구축을 끝냈고 백업 읽기 검증을 통과했다."),
    DemoIncident("disk-02", "SIM-STORAGE", "RAID 저하와 SMART 불량 섹터 증가", "가상 디스크 3번에서 불량 섹터가 늘고 배열 읽기 지연이 증가했다.", "모의 교체 디스크 준비 상태와 검증된 백업 시점을 확인한다.", "P2", TicketStatus.WAITING),
    DemoIncident("disk-03", "SIM-STORAGE", "RAID 읽기 지연과 백업 처리량 저하", "가상 백업 구간에 읽기 지연이 늘었으며 SMART 경고와 배열 구성원 이탈은 관측되지 않았다.", "백업 동시 실행 수와 디스크 큐 길이를 비교한다.", "P3", TicketStatus.IN_PROGRESS),
    DemoIncident("mem-01", "SIM-COMPUTE", "DIMM 슬롯 B2 ECC 교정 오류 누적", "가상 시스템에서 ECC 교정 오류가 같은 DIMM 슬롯에 집중됐다.", "부하 시험 후 슬롯별 카운터를 기록한다.", "P2", TicketStatus.RESOLVED, "모의 DIMM 교체 뒤 메모리 진단을 통과했고 ECC 카운터 증가가 멈췄다."),
    DemoIncident("mem-02", "SIM-COMPUTE", "DIMM 슬롯 A1 ECC 오류와 재부팅", "메모리 부하가 높은 동안 가상 호스트가 재부팅됐다. A1 슬롯에 ECC 오류가 기록됐다.", "재부팅 직전 로그와 슬롯별 메모리 진단 결과를 대조한다.", "P1", TicketStatus.IN_PROGRESS),
    DemoIncident("mem-03", "SIM-COMPUTE", "간헐적인 ECC 교정 오류 알림", "가상 장비에서 ECC 교정 오류가 한 번 발생했으며 서비스 응답 시간은 정상이다.", "다음 모의 점검 창에 슬롯별 카운터를 다시 측정한다.", "P4", TicketStatus.WAITING),
    DemoIncident("cpu-01", "SIM-BATCH", "CPU 사용률 급증과 배치 대기열 적체", "가상 배치를 동시에 실행하자 CPU 사용률과 처리 대기열이 함께 증가했다.", "동시 실행량 변경 후 대기열 해소 시간을 측정한다.", "P3", TicketStatus.RESOLVED, "모의 배치 동시 실행량을 줄인 뒤 CPU 사용률과 대기열이 정상 범위로 돌아왔다."),
    DemoIncident("cpu-02", "SIM-BATCH", "CPU 포화와 배치 처리 지연", "가상 배치 시작 직후 CPU 사용률이 95%를 넘었고 대기열이 계속 늘었다.", "프로세스별 CPU 사용량과 배치 동시 실행 수를 비교한다.", "P2", TicketStatus.OPEN),
    DemoIncident("cpu-03", "SIM-BATCH", "배치 처리 지연과 CPU 대기 시간 증가", "처리 시간이 늘었지만 CPU 실행 시간보다 디스크 I/O 대기 시간이 크게 증가했다.", "디스크 지연과 배치 읽기 요청량이 함께 증가하는지 확인한다.", "P3", TicketStatus.OPEN),
]


def seed_demo_incidents(username: str) -> int:
    added = 0
    with Session(engine(), expire_on_commit=False) as db:
        actor = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if actor is None:
            raise SystemExit("Active seed operator not found. Use --username.")
        for incident in DEMO_INCIDENTS:
            lock_ticket_snapshot(db)
            ticket_id = uuid5(NAMESPACE_URL, f"polytech-ai-agent/demo/incidents/{incident.key}")
            if db.get(Ticket, ticket_id) is not None:
                continue
            detected = now_utc() - timedelta(days=2)
            ticket = Ticket(
                id=ticket_id, title=f"[가상 장애] {incident.title}", description=incident.symptoms,
                service=incident.service, severity=incident.severity, status=TicketStatus.OPEN,
                detected_at=detected, next_action=incident.next_action, created_by=actor.id,
                version=1, created_at=detected, updated_at=detected,
            )
            db.add(ticket)
            db.flush()
            record_event(db, ticket, actor.id, "created", {"synthetic": True})
            if incident.resolution:
                apply_changes(db, ticket, actor.id, "transitioned", {
                    "status": TicketStatus.RESOLVED, "resolution": incident.resolution,
                    "resolved_at": detected + timedelta(hours=1),
                }, reason="가상 복구 검증 완료")
            if ticket.status != incident.status:
                lock_ticket_snapshot(db)
                apply_changes(db, ticket, actor.id, "transitioned", {"status": incident.status}, reason="가상 장애 처리 단계 반영")
            db.commit()
            added += 1
    return added


def seed(username: str, *, sop_only: bool = False) -> tuple[int, int]:
    tickets = 0
    documents = 0
    with Session(engine(), expire_on_commit=False) as db:
        actor = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if actor is None:
            raise SystemExit("Active seed operator not found. Use --username.")
        for case in SCENARIOS:
            ticket_id = uuid5(NAMESPACE_URL, f"polytech-ai-agent/demo/tickets/{case.key}")
            if not sop_only:
                lock_ticket_snapshot(db)
            if not sop_only and db.get(Ticket, ticket_id) is None:
                past = now_utc() - timedelta(days=7)
                ticket = Ticket(
                    id=ticket_id, title=case.ticket_title, description=case.symptoms, service=f"SIM-{case.key}",
                    severity="P2", status="open", detected_at=past, next_action=case.checks, created_by=actor.id,
                    version=1, created_at=past, updated_at=past,
                )
                db.add(ticket)
                db.flush()
                record_event(db, ticket, actor.id, "created", {"synthetic": True})
                apply_changes(db, ticket, actor.id, "transitioned", {
                    "status": "resolved", "resolution": case.resolution, "resolved_at": past + timedelta(hours=1),
                }, reason="가상 사례 검증")
                tickets += 1

            if db.scalar(select(SopDocument.id).where(SopDocument.title == case.sop_title)) is None:
                payload = (
                    f"# {case.sop_title}\n\n가상 데이터. 운영 훈련 자료입니다.\n\n"
                    f"## 증상\n{case.symptoms}\n\n## 진단\n{case.checks}\n\n## 완료 확인\n{case.resolution}\n"
                ).encode()
                register_sop(
                    db, actor_id=actor.id, filename=f"demo-{case.key}.md", content_type="text/markdown",
                    payload=payload, title=case.sop_title,
                )
                documents += 1
        db.commit()
    return documents, tickets


def reindex_existing() -> int:
    with Session(engine()) as db, db.begin():
        documents = list(db.scalars(select(SopDocument).order_by(SopDocument.id).with_for_update()))
        tickets = list(db.scalars(
            select(Ticket).where(
                Ticket.simulation_run_id.is_(None), Ticket.status.in_(RESOLVED_STATUSES),
                Ticket.archived_at.is_(None), Ticket.resolution.is_not(None)
            ).order_by(Ticket.id).with_for_update()
        ))
        for document in documents:
            enqueue_sop(db, document)
        for ticket in tickets:
            sync_ticket_index(db, ticket, explicitly=True)
        return len(documents) + len(tickets)


def evaluate() -> float:
    hits = 0
    profile = embedding_profile().fingerprint
    for case in SCENARIOS:
        vector = worker_query_vector(case.query, profile)
        with Session(engine()) as db:
            results = search_index(db, vector=vector, profile=profile, source_types=list(SourceType), limit=5)
        expected = {(SourceType.SOP, case.sop_title), (SourceType.TICKET, case.ticket_title)}
        match = len(expected & {(hit.source_type, hit.title) for hit in results})
        hits += match
        print(f"{case.key}: {match}/{len(expected)}")
    total = len(SCENARIOS) * 2
    recall = hits / total
    print(f"Recall@5={recall:.3f} ({hits}/{total}) profile={profile}")
    return recall


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--reindex", action="store_true", help="Queue all existing SOPs and resolved tickets")
    actions.add_argument("--evaluate", action="store_true", help="Measure Recall@5 after the demo data is indexed")
    actions.add_argument("--sop-only", action="store_true", help="Add synthetic SOPs without creating demo tickets")
    actions.add_argument("--demo-incidents", action="store_true", help="Add 12 related incidents across multiple ticket statuses")
    parser.add_argument("--username", default="demo", help="An existing operator for synthetic records")
    args = parser.parse_args()
    if args.reindex:
        print(f"Sources queued or in progress: {reindex_existing()}")
    elif args.evaluate:
        evaluate()
    elif args.demo_incidents:
        print(f"Synthetic demo incidents added: {seed_demo_incidents(args.username.strip().casefold())}")
    else:
        documents, tickets = seed(args.username.strip().casefold(), sop_only=args.sop_only)
        print(f"Synthetic records added: SOP={documents}, tickets={tickets}")


if __name__ == "__main__":
    main()

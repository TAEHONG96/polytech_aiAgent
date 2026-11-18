"""Synthetic RAG fixtures, reindexing, and source-level Recall@5."""

import argparse
from dataclasses import dataclass
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.knowledge.documents import register_sop
from app.knowledge.embedding import embedding_profile
from app.knowledge.indexing import RESOLVED_STATUSES, enqueue_sop, now_utc, sync_ticket_index
from app.knowledge.models import SopDocument, SourceType
from app.knowledge.search import search_index, worker_query_vector
from app.models import Ticket, User
from app.tickets import apply_changes, record_event


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


def seed(username: str) -> tuple[int, int]:
    tickets = 0
    documents = 0
    with Session(engine(), expire_on_commit=False) as db:
        actor = db.scalar(select(User).where(User.username == username, User.is_active.is_(True)))
        if actor is None:
            raise SystemExit("Active seed operator not found. Use --username.")
        for case in SCENARIOS:
            ticket_id = uuid5(NAMESPACE_URL, f"polytech-ai-agent/demo/tickets/{case.key}")
            if db.get(Ticket, ticket_id) is None:
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
    return documents, tickets


def reindex_existing() -> int:
    with Session(engine()) as db, db.begin():
        documents = list(db.scalars(select(SopDocument).order_by(SopDocument.id).with_for_update()))
        tickets = list(db.scalars(
            select(Ticket).where(
                Ticket.status.in_(RESOLVED_STATUSES), Ticket.archived_at.is_(None), Ticket.resolution.is_not(None)
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
    parser.add_argument("--username", default="demo", help="An existing operator for synthetic records")
    args = parser.parse_args()
    if args.reindex:
        print(f"Sources queued or in progress: {reindex_existing()}")
    elif args.evaluate:
        evaluate()
    else:
        documents, tickets = seed(args.username.strip().casefold())
        print(f"Synthetic records added: SOP={documents}, tickets={tickets}")


if __name__ == "__main__":
    main()

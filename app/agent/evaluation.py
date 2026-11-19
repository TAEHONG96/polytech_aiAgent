"""Measure source-level Recall@5 and score separation on isolated synthetic data."""

import argparse
import json
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from app.db import engine
from app.knowledge.demo import SCENARIOS, seed
from app.knowledge.embedding import Embedder, EmbeddingFailure, get_embedder
from app.knowledge.indexing import ACTIVE_STATES, process_next
from app.knowledge.models import EMBEDDING_DIM, IndexJob, IndexStatus, KnowledgeChunk, SopDocument, SourceType
from app.knowledge.search import calibrated_min_score, search_index
from app.models import Ticket


@dataclass(frozen=True)
class Query:
    label: str
    text: str
    scenario: str | None
    split: str


QUERIES = [Query(case.key, case.query, case.key, "calibration") for case in SCENARIOS] + [
    Query("boot-paraphrase", "전원을 켜면 초기 하드웨어 자체 검사에서 멈추고 운영체제가 시작되지 않는다", "boot", "holdout"),
    Query("memory-paraphrase", "특정 메모리 모듈에서 보정 가능한 오류가 반복된다", "memory", "holdout"),
    Query("disk-paraphrase", "저장장치 자체 진단이 경고를 내고 디스크 배열에서 구성원 한 개가 빠졌다", "disk", "holdout"),
    Query("network-paraphrase", "연결이 간헐적으로 끊기고 전송한 데이터 일부가 목적지에 도달하지 않는다", "network", "holdout"),
    Query("power-paraphrase", "이중화 전원 중 한쪽 입력이 사라지고 남은 공급 장치로 작동한다", "power", "holdout"),
    Query("negative-coffee", "회의실 커피머신 원두를 구매하려면 어떻게 하나요?", None, "calibration"),
    Query("negative-leave", "연차 신청과 승인 일정을 알려줘", None, "calibration"),
    Query("negative-furniture", "구내식당 의자 배치와 책상 주문", None, "calibration"),
    Query("negative-invoice", "지난달 세금계산서 발행과 정산 문의", None, "calibration"),
    Query("negative-training", "사내 발표 교육의 장소와 교재를 알려주세요", None, "calibration"),
    Query("negative-book", "이번 달 독서 모임에서 읽을 책을 추천해줘", None, "holdout"),
    Query("negative-delivery", "회의실에 간식을 배달할 주문 번호를 확인해줘", None, "holdout"),
    Query("negative-rain", "비가 올 때 가죽 신발을 보관하는 방법", None, "holdout"),
    Query("negative-travel", "출근 버스 운행표와 환승 시간을 알고 싶어요", None, "holdout"),
    Query("negative-printer", "사무실 프린터에서 용지가 걸립니다", None, "holdout"),
]

CHALLENGE_QUERIES = [
    Query("boot-startup", "장비 기동 직후 자체 진단 코드 B101이 표시되고 운영체제 화면 전 검사에서 실패합니다", "boot", "challenge"),
    Query("memory-rate", "한 메모리 슬롯에서 교정 가능 경고가 점점 자주 나타납니다", "memory", "challenge"),
    Query("network-errors", "서비스 인터페이스 수신 오류가 증가하고 전송 패킷 일부가 사라집니다", "network", "challenge"),
    Query("bmc-port", "관리 화면이 열리지 않고 BMC 전용 포트가 비활성 상태입니다", "bmc", "challenge"),
    Query("negative-cache", "Redis 캐시 만료 정책과 세션 정리 방법", None, "challenge"),
    Query("negative-react", "배포 버전이 맞지 않아 React 컴파일 오류가 나요", None, "challenge"),
    Query("negative-tls", "TLS 인증서 읽기 오류와 HTTPS 리다이렉트 설정", None, "challenge"),
    Query("negative-db", "PostgreSQL에서 트랜잭션 교착으로 데이터 갱신이 실패합니다", None, "challenge"),
    Query("negative-menu", "점심 행사 참석 인원과 메뉴를 확정해 주세요", None, "challenge"),
]

PHASE_FIVE_QUERIES = QUERIES + [
    CHALLENGE_QUERIES[0],
    Query("boot-not-power", "전원을 켜면 PSU의 양쪽 입력은 정상인데 POST 중 B101 메인보드 진단 오류로 멈춥니다", "boot", "contrast"),
    Query("power-not-boot", "서버 자체 검사는 정상이며 부팅은 됩니다. PSU B 입력이 사라져 이중화 경고가 발생합니다", "power", "contrast"),
    Query("absent-device", "광학 테이프 장비의 가상 진단 코드 QZ-9741 복구 절차", None, "absence"),
]


class CountedEmbedder:
    def __init__(self, provider: Embedder):
        self.provider = provider
        self.profile = provider.profile
        self.document_requests = 0
        self.query_requests = 0

    def documents(self, title: str, texts: Sequence[str]) -> list[list[float]]:
        self.document_requests += 1
        return self.provider.documents(title, texts)

    def query(self, query: str) -> list[float]:
        self.query_requests += 1
        return self.provider.query(query)


def prepare(provider: CountedEmbedder, username: str) -> None:
    with Session(engine()) as db:
        if db.scalar(select(func.count()).select_from(Ticket)) or db.scalar(select(func.count()).select_from(SopDocument)):
            raise SystemExit("Use a fresh isolated Compose project for --prepare.")
    seed(username)
    end = time.monotonic() + 90
    while time.monotonic() < end:
        if process_next(provider):
            continue
        with Session(engine()) as db:
            active = db.scalar(select(func.count()).select_from(IndexJob).where(IndexJob.status.in_(ACTIVE_STATES)))
            if not active:
                break
        time.sleep(1)
    with Session(engine()) as db:
        statuses = list(db.execute(select(IndexJob.status, IndexJob.error_code)))
    if len(statuses) != 14 or any(row.status != IndexStatus.COMPLETED for row in statuses):
        print(json.dumps({"index_statuses": [tuple(row) for row in statuses]}))
        raise SystemExit("Synthetic indexing did not complete.")


def evaluate(provider: CountedEmbedder, queries=QUERIES) -> dict:
    with Session(engine()) as db:
        dimension_rows = db.execute(select(
            KnowledgeChunk.embedding_profile, func.min(func.vector_dims(KnowledgeChunk.embedding)),
            func.max(func.vector_dims(KnowledgeChunk.embedding)),
        ).group_by(KnowledgeChunk.embedding_profile)).all()
        titles = set(db.scalars(select(distinct(SopDocument.title))))
        ticket_titles = set(db.scalars(select(distinct(Ticket.title))))
    if [tuple(row) for row in dimension_rows] != [(provider.profile.fingerprint, EMBEDDING_DIM, EMBEDDING_DIM)]:
        raise SystemExit("Evaluation requires one active profile with 768-dimensional vectors.")
    cases = {case.key: case for case in SCENARIOS}
    if titles != {case.sop_title for case in cases.values()} or ticket_titles != {case.ticket_title for case in cases.values()}:
        raise SystemExit("Evaluation requires only the labeled synthetic corpus.")
    rows = []
    for query in queries:
        vector = provider.query(query.text)
        with Session(engine()) as db:
            results = search_index(db, vector=vector, profile=provider.profile.fingerprint, source_types=list(SourceType), limit=5)
        case = cases.get(query.scenario)
        expected = {(SourceType.SOP, case.sop_title), (SourceType.TICKET, case.ticket_title)} if case else set()
        matches = [(rank, hit.score) for rank, hit in enumerate(results, 1) if (hit.source_type, hit.title) in expected]
        rows.append(dict(label=query.label, split=query.split, relevant=bool(case), matches=matches,
                         top_score=results[0].score if results else None))
    positives = [row for row in rows if row["relevant"]]
    negatives = [row for row in rows if not row["relevant"]]
    low_calibrated_hit = min((max((score for _, score in row["matches"]), default=-1)
                              for row in positives if row["split"] == "calibration"), default=-1)
    high_calibrated_negative = max((row["top_score"] for row in negatives
                                    if row["split"] == "calibration" and row["top_score"] is not None), default=-1)
    candidate = (high_calibrated_negative + low_calibrated_hit) / 2 if high_calibrated_negative < low_calibrated_hit else None
    holdout_pos = [row for row in positives if row["split"] != "calibration"]
    holdout_neg = [row for row in negatives if row["split"] != "calibration"]
    applied = calibrated_min_score(provider.profile.fingerprint)
    return {
        "profile": provider.profile.fingerprint, "dimensions": EMBEDDING_DIM,
        "positive_queries": len(positives), "irrelevant_queries": len(negatives),
        "recall_at_5": sum(len(row["matches"]) for row in positives) / (2 * len(positives)),
        "holdout_recall_at_5": sum(len(row["matches"]) for row in holdout_pos) / (2 * len(holdout_pos)),
        "calibration_positive_floor": low_calibrated_hit, "calibration_irrelevant_ceiling": high_calibrated_negative,
        "candidate_min_score": candidate,
        "holdout_positive_hits_after_threshold": sum(
            score >= candidate for row in holdout_pos for _, score in row["matches"]
        ) if candidate is not None else None,
        "holdout_irrelevant_rejected": sum(
            row["top_score"] is None or row["top_score"] < candidate for row in holdout_neg
        ) if candidate is not None else None,
        "applied_min_score": applied,
        "positive_hits_after_applied_threshold": sum(
            score >= applied for row in positives for _, score in row["matches"]
        ) if applied is not None else None,
        "irrelevant_rejected_after_applied_threshold": sum(
            row["top_score"] is None or row["top_score"] < applied for row in negatives
        ) if applied is not None else None,
        "document_requests": provider.document_requests, "query_requests": provider.query_requests,
        "queries": rows,
    }


def acceptance_evaluation(provider: CountedEmbedder) -> dict:
    """Measure the same thresholded search used by the API, on the labeled corpus."""
    with Session(engine()) as db:
        sop_ids = dict(db.execute(select(SopDocument.title, SopDocument.id)).all())
        ticket_ids = dict(db.execute(select(Ticket.title, Ticket.id)).all())
        profiles = db.execute(select(
            KnowledgeChunk.embedding_profile, func.vector_dims(KnowledgeChunk.embedding),
        ).distinct()).all()
    cases = {case.key: case for case in SCENARIOS}
    if (set(sop_ids) != {case.sop_title for case in cases.values()}
            or set(ticket_ids) != {case.ticket_title for case in cases.values()}
            or [tuple(row) for row in profiles] != [(provider.profile.fingerprint, EMBEDDING_DIM)]):
        raise SystemExit("Use the isolated, indexed synthetic corpus with one 768-dimensional profile.")

    options = dict(profile=provider.profile.fingerprint, source_types=list(SourceType), limit=5,
                   min_score=calibrated_min_score(provider.profile.fingerprint))
    rows = []
    boot_vector = None
    for query in PHASE_FIVE_QUERIES:
        case = cases.get(query.scenario)
        expected = [str(sop_ids[case.sop_title]), str(ticket_ids[case.ticket_title])] if case else []
        started = time.perf_counter()
        vector = provider.query(query.text)
        embedded = time.perf_counter()
        with Session(engine()) as db:
            hits = search_index(db, vector=vector, **options)
        elapsed_ms = (time.perf_counter() - started) * 1000
        actual = [{"id": str(hit.source_id), "type": hit.source_type, "title": hit.title, "score": hit.score}
                  for hit in hits]
        ranks = [rank for rank, hit in enumerate(actual, 1) if hit["id"] in expected]
        rows.append(dict(label=query.label, query=query.text, split=query.split, expected_ids=expected,
                         actual=actual, embedding_ms=round((embedded - started) * 1000, 2),
                         elapsed_ms=round(elapsed_ms, 2), matched=len(ranks),
                         reciprocal_rank=1 / ranks[0] if ranks else 0))
        if query.label == "boot":
            boot_vector = vector

    removed_id = sop_ids[cases["boot"].sop_title]
    with Session(engine()) as db:
        before = [hit.source_id for hit in search_index(db, vector=boot_vector, **options)]
        db.delete(db.get(SopDocument, removed_id))
        db.flush()
        after = [hit.source_id for hit in search_index(db, vector=boot_vector, **options)]
        db.rollback()
        restored = [hit.source_id for hit in search_index(db, vector=boot_vector, **options)]
    positive = [row for row in rows if row["expected_ids"]]
    negative = [row for row in rows if not row["expected_ids"]]
    times = [row["elapsed_ms"] for row in rows]
    return {
        "provider": provider.profile.provider, "profile": provider.profile.fingerprint,
        "dimensions": EMBEDDING_DIM, "min_score": options["min_score"],
        "positive_queries": len(positive), "negative_queries": len(negative),
        "recall_at_5": statistics.mean(row["matched"] / len(row["expected_ids"]) for row in positive),
        "precision_at_5": statistics.mean(row["matched"] / 5 for row in positive),
        "mrr": statistics.mean(row["reciprocal_rank"] for row in positive),
        "negative_empty": sum(not row["actual"] for row in negative),
        "latency_ms": {"p50": statistics.median(times), "p95": statistics.quantiles(times, n=20, method="inclusive")[18],
                       "max": max(times)},
        "deletion": {"source_id": str(removed_id), "present_before": removed_id in before,
                     "absent_during_delete": removed_id not in after, "present_after_rollback": removed_id in restored},
        "document_requests": provider.document_requests, "query_requests": provider.query_requests, "queries": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="Seed and index a fresh isolated Compose project")
    parser.add_argument("--username", default="demo")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--challenge-only", action="store_true", help="Evaluate a separately labeled challenge set")
    mode.add_argument("--phase-five", action="store_true", help="Record thresholded acceptance metrics and deletion behavior")
    args = parser.parse_args()
    try:
        provider = CountedEmbedder(get_embedder())
        if args.prepare:
            prepare(provider, args.username)
        report = (acceptance_evaluation(provider) if args.phase_five
                  else evaluate(provider, CHALLENGE_QUERIES if args.challenge_only else QUERIES))
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except EmbeddingFailure as exc:
        print(json.dumps({"error_code": exc.code, "retryable": exc.retryable}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

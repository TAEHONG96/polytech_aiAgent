import argparse
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from threading import Event
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.agent.provider import AnalysisFailure, generator
from app.agent.queue import utcnow
from app.db import engine
from app.simulation.bmc_actions import BMC_RULE_VERSION
from app.simulation.evaluation_feedback import explain
from app.simulation.evaluation_input import ELIGIBLE
from app.simulation.evaluation_rules import grade
from app.simulation.models import SimulationEvaluation as Evaluation, SimulationRun, SimulationTicketEvaluation as Entry
from app.simulation.network_actions import NETWORK_RULE_VERSION

logger = logging.getLogger(__name__)
STAGES = ("rules", "explanation")
MAX_ATTEMPTS = 3
LEASE = timedelta(seconds=120)
RULE_VERSIONS = {"dco-basic-1.0": grade, "dco-basic-1.1": grade,
                 "synthetic-corpus-1": partial(grade, flexible=True),
                 BMC_RULE_VERSION: partial(grade, flexible=True, bmc_pilot=True),
                 NETWORK_RULE_VERSION: partial(grade, flexible=True, network_pilot=True)}


@dataclass(frozen=True)
class Claim:
    evaluation_id: UUID
    stage: str
    token: UUID


def column(stage, name):
    return getattr(Evaluation, f"{stage}_{name}")


def change(row, stage, **values):
    for name, value in values.items():
        setattr(row, f"{stage}_{name}", value)


def claim(stage: str) -> Claim | None:
    now = utcnow()
    status = column(stage, "status")
    with Session(engine()) as db, db.begin():
        statement = select(Evaluation).join(SimulationRun, SimulationRun.id == Evaluation.simulation_run_id).where(
            SimulationRun.status == "COMPLETED", SimulationRun.finalized_handover_id == Evaluation.handover_id,
            SimulationRun.finalized_revision == Evaluation.handover_revision,
            or_(and_(status == "PENDING", column(stage, "available_at") <= now),
                and_(status == "PROCESSING", column(stage, "lease_until") <= now)),
        )
        if stage == "explanation":
            statement = statement.where(Evaluation.rules_status == "READY")
        row = db.scalar(statement.order_by(Evaluation.created_at, Evaluation.id).limit(1)
                        .with_for_update(of=Evaluation, skip_locked=True))
        if row is None:
            return None
        attempts = getattr(row, f"{stage}_attempts")
        if attempts >= MAX_ATTEMPTS:
            change(row, stage, status="FAILED", lease_token=None, lease_until=None, error="EVALUATION_LEASE_EXHAUSTED")
            row.updated_at = now
            return None
        token = uuid4()
        change(row, stage, status="PROCESSING", attempts=attempts + 1, lease_token=token, lease_until=now + LEASE)
        row.updated_at = now
        return Claim(row.id, stage, token)


def owned(db: Session, work: Claim):
    row = db.get(Evaluation, work.evaluation_id, with_for_update=True)
    if (row is None or getattr(row, f"{work.stage}_status") != "PROCESSING"
            or getattr(row, f"{work.stage}_lease_token") != work.token
            or getattr(row, f"{work.stage}_lease_until") <= utcnow()):
        return None
    return row


def publish(work: Claim, results: dict) -> bool:
    with Session(engine()) as db, db.begin():
        row = owned(db, work)
        if row is None:
            return False
        rows = {str(entry.ticket_id): entry for entry in db.scalars(select(Entry).where(Entry.evaluation_id == row.id))}
        if rows.keys() != results.keys():
            raise ValueError("Evaluation entries do not match the fixed input")
        now = utcnow()
        counts = Counter()
        for ticket_id, result in results.items():
            entry = rows[ticket_id]
            if work.stage == "rules":
                entry.rule_results, entry.evidence = result["rules"], result["evidence"]
                counts.update(rule["result"] for rule in entry.rule_results)
            else:
                entry.explanation = result
        if work.stage == "rules":
            row.rule_summary = dict(counts)
            change(row, "explanation", status="PENDING", available_at=now)
        change(row, work.stage, status="READY", lease_token=None, lease_until=None, error=None)
        row.updated_at = now
        return True


def fail(work: Claim, error: str, *, retryable: bool, retry_after=None):
    with Session(engine()) as db, db.begin():
        row = owned(db, work)
        if row is None:
            return
        now = utcnow()
        attempts = getattr(row, f"{work.stage}_attempts")
        retry = retryable and attempts < MAX_ATTEMPTS
        delay = max(5 * 2 ** (attempts - 1), min(retry_after or 0, 120))
        change(row, work.stage, status="PENDING" if retry else "FAILED", lease_token=None, lease_until=None,
               error=error, available_at=now + timedelta(seconds=delay) if retry else None)
        row.updated_at = now


async def feedback(model, tickets, recorded):
    try:
        async with asyncio.timeout(90):
            return {ticket["id"]: await explain(model, ticket, *recorded[ticket["id"]]) for ticket in tickets}
    finally:
        try:
            async with asyncio.timeout(2):
                await model.close()
        except Exception as exc:
            logger.warning("evaluation client close failed type=%s", type(exc).__name__)


def process(stage: str, model_factory=generator) -> bool:
    work = claim(stage)
    if work is None:
        return False
    try:
        with Session(engine()) as db:
            row = db.get(Evaluation, work.evaluation_id)
            tickets = [ticket for ticket in row.input_snapshot["tickets"] if ticket["origin"] in ELIGIBLE]
            report_id = str(row.handover_id)
            version = row.rule_version
            if stage == "explanation":
                recorded = {str(entry.ticket_id): (entry.rule_results, entry.evidence)
                            for entry in db.scalars(select(Entry).where(Entry.evaluation_id == row.id))}
        if stage == "rules":
            rule_engine = RULE_VERSIONS.get(version)
            if rule_engine is None:
                raise AnalysisFailure("EVALUATION_UNKNOWN_RULE_VERSION", "지원되는 평가 규칙 버전을 확인하세요.")
            results = {ticket["id"]: rule_engine(ticket, report_id) for ticket in tickets}
        else:
            results = asyncio.run(feedback(model_factory(), tickets, recorded))
        publish(work, results)
    except AnalysisFailure as exc:
        fail(work, exc.code, retryable=exc.retryable, retry_after=exc.retry_after)
    except TimeoutError:
        fail(work, "EVALUATION_TIMEOUT", retryable=True)
    except Exception as exc:
        logger.warning("evaluation failed stage=%s type=%s", stage, type(exc).__name__)
        fail(work, "EVALUATION_PROCESSING_FAILED", retryable=True)
    return True


def retry_explanation(evaluation_id: UUID) -> bool:
    with Session(engine()) as db, db.begin():
        row = db.get(Evaluation, evaluation_id, with_for_update=True)
        if row is None or row.rules_status != "READY" or row.explanation_status != "FAILED" or not row.finalize_key:
            return False
        change(row, "explanation", status="PENDING", attempts=0, error=None,
               available_at=utcnow(), lease_token=None, lease_until=None)
        row.updated_at = utcnow()
        return True


def process_evaluation_queue(stage: str, stop: Event):
    while not stop.is_set():
        try:
            if process(stage):
                continue
        except Exception as exc:
            logger.warning("evaluation queue failed stage=%s type=%s", stage, type(exc).__name__)
        stop.wait(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retry only a failed training evaluation explanation.")
    parser.add_argument("--retry-explanation", type=UUID, required=True)
    if not retry_explanation(parser.parse_args().retry_explanation):
        raise SystemExit("No failed explanation eligible for retry.")

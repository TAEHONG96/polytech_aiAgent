"""Exercise QA worker failure or change approval of the versioned synthetic fixture."""
import argparse
import os
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agent.models import AgentRun
from app.agent.provider import AnalysisFailure, FakeGenerator
from app.agent.queue import claim_run as claim_agent, process_agent_run
from app.agent.schemas import RunStatus
from app.auth import password_hasher
from app.db import engine
from app.knowledge.models import SopDocument
from app.simulation import evaluation_worker as evaluation
from app.simulation.learning_materials import TITLE
from app.simulation.models import ScenarioIncident, SimulationEvaluation


class Unavailable(FakeGenerator):
    async def generate(self, contents, config):
        raise AnalysisFailure("QA_PROVIDER_UNAVAILABLE", "합성 QA에서 공급자가 응답하지 않았습니다.")


def provision(username: str):
    if not username.startswith("p5-"):
        raise ValueError("Use a synthetic Phase 5 account name.")
    UUID(username[3:])
    with Session(engine()) as db, db.begin():
        assert db.scalar(select(func.register_training_user(
            uuid4(), username, username, password_hasher.hash("synthetic-phase5-browser-password"),
        )))


def fail_jobs(run_id: UUID):
    with Session(engine()) as db:
        job = db.scalar(select(AgentRun).where(AgentRun.simulation_run_id == run_id, AgentRun.status == RunStatus.QUEUED))
        if job is None:
            raise RuntimeError("Expected the isolated synthetic analysis job")
        job_id = job.id
        record = db.scalar(select(SimulationEvaluation).where(SimulationEvaluation.simulation_run_id == run_id))
        if record is None or record.rules_status != "PENDING":
            raise RuntimeError("Expected a queued synthetic evaluation")
        record_id = record.id

    # Scope the existing worker claim functions to this test. The real background worker is stopped.
    from unittest.mock import patch

    def agent_claim():
        work = claim_agent()
        if work is None or work.id != job_id:
            raise RuntimeError("Unexpected agent claim in the isolated QA queue")
        return work

    claim_evaluation = evaluation.claim

    def evaluation_claim(stage):
        work = claim_evaluation(stage)
        if work is None or work.evaluation_id != record_id:
            raise RuntimeError("Unexpected evaluation claim in the isolated QA queue")
        return work

    with patch("app.agent.queue.claim_run", agent_claim), patch.object(evaluation, "claim", evaluation_claim):
        assert process_agent_run(Unavailable)
        assert evaluation.process("rules", FakeGenerator)
        assert evaluation.process("explanation", Unavailable)
    with Session(engine()) as db:
        record = db.get(SimulationEvaluation, record_id)
        assert db.get(AgentRun, job_id).error_code == "QA_PROVIDER_UNAVAILABLE"
        assert (record.rules_status, record.explanation_status, record.explanation_error) == (
            "READY", "FAILED", "QA_PROVIDER_UNAVAILABLE",
        )


def approval(kind: str, identifier: UUID, approved: bool):
    owner = create_engine(engine().url.set(username="postgres", password=os.environ["TEST_DB_OWNER_PASSWORD"]),
                          hide_parameters=True)
    try:
        with Session(owner) as db, db.begin():
            row = db.get(SopDocument if kind == "sop" else ScenarioIncident, identifier, with_for_update=True)
            if kind == "sop" and row and row.title == TITLE:
                row.training_approved = approved
            elif kind == "incident" and row and row.incident_key in {"SYN-MEM-01", "SYN-STOR-01"}:
                row.approved = approved
            else:
                raise RuntimeError("Expected a known Phase 5 synthetic learning fixture")
    finally:
        owner.dispose()


if __name__ == "__main__":
    if os.environ.get("RUN_POSTGRES_TESTS") != "1" or os.environ.get("AGENT_PROVIDER") != "fake":
        raise SystemExit("Use the isolated Fake QA test container.")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True, dest="command")
    failure = sub.add_parser("fail")
    failure.add_argument("run_id", type=UUID)
    approve = sub.add_parser("approval")
    approve.add_argument("kind", choices=("sop", "incident"))
    approve.add_argument("id", type=UUID)
    approve.add_argument("approved", choices=("yes", "no"))
    account = sub.add_parser("provision")
    account.add_argument("username")
    args = parser.parse_args()
    if args.command == "fail":
        fail_jobs(args.run_id)
    elif args.command == "approval":
        approval(args.kind, args.id, args.approved == "yes")
    else:
        provision(args.username)

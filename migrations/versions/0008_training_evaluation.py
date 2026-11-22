"""Add training finalization receipts, learning evidence, and evaluation leases."""
from alembic import op

revision = "0008_training_evaluation"
down_revision = "0007_simulation_workflows"


def upgrade():
    op.execute("""
      ALTER TABLE simulation_runs ADD COLUMN finalized_handover_id uuid, ADD COLUMN finalized_revision integer,
        ADD CONSTRAINT fk_run_final_revision FOREIGN KEY (finalized_handover_id,finalized_revision)
          REFERENCES handover_revisions(handover_id,version) MATCH FULL,
        ADD CONSTRAINT ck_run_final_state CHECK (finalized_handover_id IS NULL OR status='COMPLETED');
      GRANT UPDATE (completed_at, finalized_handover_id, finalized_revision) ON simulation_runs TO app_runtime;
      ALTER TABLE simulation_evaluations ADD COLUMN finalize_key varchar(128), ADD COLUMN finalize_digest varchar(64),
        ADD COLUMN finalize_response jsonb,
        ADD CONSTRAINT ck_finalize_receipt CHECK (
          (finalize_key IS NULL AND finalize_digest IS NULL AND finalize_response IS NULL) OR
          (finalize_key IS NOT NULL AND finalize_digest IS NOT NULL AND finalize_response IS NOT NULL)
        );
      GRANT INSERT ON simulation_evaluations, simulation_ticket_evaluations TO app_runtime;
      GRANT UPDATE (rules_status, explanation_status, rule_summary, updated_at)
        ON simulation_evaluations TO app_runtime;
      GRANT UPDATE (rule_results, evidence, explanation) ON simulation_ticket_evaluations TO app_runtime;
      CREATE TABLE simulation_learning_events (
        id uuid PRIMARY KEY, simulation_run_id uuid NOT NULL, ticket_id uuid NOT NULL,
        actor_id uuid NOT NULL REFERENCES users(id), event_sequence bigint NOT NULL, occurred_at timestamptz NOT NULL,
        kind varchar(12) NOT NULL, source_id uuid NOT NULL, activity varchar(6) NOT NULL,
        action_event_id uuid REFERENCES ticket_events(id),
        CONSTRAINT fk_learning_run_ticket FOREIGN KEY(simulation_run_id,ticket_id) REFERENCES tickets(simulation_run_id,id),
        CONSTRAINT uq_learning_sequence UNIQUE(simulation_run_id,event_sequence),
        CONSTRAINT uq_learning_evidence UNIQUE(simulation_run_id,ticket_id,kind,source_id,activity),
        CONSTRAINT ck_learning_activity CHECK (kind IN ('HOST_HISTORY','SOP') AND activity IN ('VIEWED','USED')
          AND (activity='USED') = (action_event_id IS NOT NULL))
      );
      GRANT SELECT, INSERT ON simulation_learning_events TO app_runtime;
    """)
    for stage in ("rules", "explanation"):
        op.execute(f"""
          ALTER TABLE simulation_evaluations
            ADD COLUMN {stage}_attempts integer NOT NULL DEFAULT 0,
            ADD COLUMN {stage}_available_at timestamptz,
            ADD COLUMN {stage}_lease_token uuid,
            ADD COLUMN {stage}_lease_until timestamptz,
            ADD COLUMN {stage}_error varchar(64);
          CREATE INDEX ix_evaluation_{stage}_queue ON simulation_evaluations ({stage}_status,{stage}_available_at)
            WHERE {stage}_status IN ('PENDING','PROCESSING');
          GRANT UPDATE ({stage}_attempts,{stage}_available_at,{stage}_lease_token,{stage}_lease_until,{stage}_error)
            ON simulation_evaluations TO app_runtime;
        """)


def downgrade():
    raise RuntimeError("Restore the matching backup to preserve finalized reports, evaluation results and evidence.")

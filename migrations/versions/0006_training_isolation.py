"""Add fail-closed training accounts and resource scopes."""

from alembic import op

revision = "0006_training_isolation"
down_revision = "0005_simulation_foundation"


def upgrade() -> None:
    op.execute("""ALTER TABLE users ADD COLUMN role varchar(8) NOT NULL DEFAULT 'operator',
                   ADD CONSTRAINT ck_users_role CHECK (role IN ('operator', 'trainee'))""")
    op.execute("""
        CREATE FUNCTION public.register_training_user(uuid, text, text, text) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
          INSERT INTO public.users (id, username, display_name, password_hash, role, is_active, created_at)
          VALUES ($1, $2, $3, $4, 'trainee', true, now())
          ON CONFLICT (username) DO NOTHING RETURNING id
        $$;
        REVOKE ALL ON FUNCTION public.register_training_user(uuid, text, text, text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.register_training_user(uuid, text, text, text) TO app_runtime;
        GRANT UPDATE (status) ON simulation_runs TO app_runtime;
    """)
    op.execute("""
        ALTER TABLE tickets DROP CONSTRAINT ck_tickets_simulation_seed,
          ALTER COLUMN training_origin TYPE varchar(12),
          ADD CONSTRAINT ck_tickets_simulation_seed CHECK (
            (simulation_run_id IS NULL AND scenario_id IS NULL AND scenario_host_id IS NULL
             AND scenario_ticket_template_id IS NULL AND training_origin IS NULL) OR
            (simulation_run_id IS NOT NULL AND scenario_id IS NOT NULL AND scenario_host_id IS NOT NULL
             AND shift_instance_id IS NULL AND training_origin IS NOT NULL
             AND ((scenario_ticket_template_id IS NOT NULL AND training_origin IN ('INITIAL', 'CARRYOVER'))
                  OR (scenario_ticket_template_id IS NULL AND training_origin = 'USER_CREATED')))
          );
        ALTER TABLE sop_documents ADD COLUMN training_approved boolean NOT NULL DEFAULT false;
        ALTER TABLE agent_runs ADD COLUMN simulation_run_id uuid REFERENCES simulation_runs(id);
        ALTER TABLE handovers ADD COLUMN simulation_run_id uuid UNIQUE REFERENCES simulation_runs(id),
          ALTER COLUMN outgoing_shift_id DROP NOT NULL, ALTER COLUMN incoming_shift_id DROP NOT NULL,
          ADD CONSTRAINT ck_handover_scope CHECK (
            (simulation_run_id IS NULL AND outgoing_shift_id IS NOT NULL AND incoming_shift_id IS NOT NULL) OR
            (simulation_run_id IS NOT NULL AND outgoing_shift_id IS NULL AND incoming_shift_id IS NULL
             AND supersedes_id IS NULL AND acknowledged_by IS NULL)
          );
    """)
    op.execute("""
        CREATE TABLE scenario_incidents (
          id uuid PRIMARY KEY,
          scenario_id uuid NOT NULL,
          scenario_host_id uuid NOT NULL,
          occurred_at timestamptz NOT NULL,
          title varchar(160) NOT NULL,
          resolution text NOT NULL,
          approved boolean NOT NULL DEFAULT false,
          embedding vector(768),
          embedding_profile varchar(160),
          CONSTRAINT fk_scenario_incidents_host FOREIGN KEY (scenario_id, scenario_host_id)
            REFERENCES scenario_hosts (scenario_id, id),
          CONSTRAINT ck_scenario_incidents_embedding CHECK ((embedding IS NULL) = (embedding_profile IS NULL))
        );
        GRANT SELECT ON scenario_incidents TO app_runtime;
        CREATE TABLE simulation_evaluations (
          id uuid PRIMARY KEY,
          simulation_run_id uuid UNIQUE NOT NULL REFERENCES simulation_runs(id),
          handover_id uuid NOT NULL,
          handover_revision integer NOT NULL,
          rule_version varchar(64) NOT NULL,
          input_snapshot jsonb NOT NULL,
          rules_status varchar(12) NOT NULL,
          explanation_status varchar(12) NOT NULL,
          rule_summary jsonb,
          natural_language_summary jsonb,
          created_at timestamptz NOT NULL,
          updated_at timestamptz NOT NULL,
          CONSTRAINT fk_sim_evaluation_revision FOREIGN KEY (handover_id, handover_revision)
            REFERENCES handover_revisions(handover_id, version),
          CONSTRAINT ck_sim_evaluation_rules CHECK (rules_status IN ('PENDING','PROCESSING','READY','FAILED')),
          CONSTRAINT ck_sim_evaluation_explanation
            CHECK (explanation_status IN ('NOT_STARTED','PENDING','PROCESSING','READY','FAILED'))
        );
        CREATE TABLE simulation_ticket_evaluations (
          id uuid PRIMARY KEY,
          evaluation_id uuid NOT NULL REFERENCES simulation_evaluations(id),
          ticket_id uuid NOT NULL REFERENCES tickets(id),
          activity_status varchar(14) NOT NULL,
          final_status varchar(20) NOT NULL,
          rule_results jsonb,
          evidence jsonb,
          explanation jsonb,
          CONSTRAINT uq_sim_evaluation_ticket UNIQUE (evaluation_id,ticket_id),
          CONSTRAINT ck_sim_evaluation_activity CHECK (activity_status IN ('ATTEMPTED','NOT_ATTEMPTED')),
          CONSTRAINT ck_sim_evaluation_final_status CHECK (final_status IN ('open','in_progress','waiting','resolved','closed'))
        );
        GRANT SELECT ON simulation_evaluations, simulation_ticket_evaluations TO app_runtime;
    """)


def downgrade() -> None:
    op.execute("""
      DO $$
      BEGIN
        IF EXISTS (SELECT 1 FROM users WHERE role = 'trainee')
          OR EXISTS (SELECT 1 FROM tickets WHERE training_origin = 'USER_CREATED')
          OR EXISTS (SELECT 1 FROM agent_runs WHERE simulation_run_id IS NOT NULL)
          OR EXISTS (SELECT 1 FROM handovers WHERE simulation_run_id IS NOT NULL)
          OR EXISTS (SELECT 1 FROM sop_documents WHERE training_approved)
          OR EXISTS (SELECT 1 FROM scenario_incidents)
          OR EXISTS (SELECT 1 FROM simulation_evaluations) THEN
          RAISE EXCEPTION 'Downgrade would remove training data or roles';
        END IF;
      END $$;
      DROP TABLE simulation_ticket_evaluations, simulation_evaluations, scenario_incidents;
      ALTER TABLE handovers DROP CONSTRAINT ck_handover_scope, DROP COLUMN simulation_run_id,
        ALTER COLUMN outgoing_shift_id SET NOT NULL, ALTER COLUMN incoming_shift_id SET NOT NULL;
      ALTER TABLE agent_runs DROP COLUMN simulation_run_id;
      ALTER TABLE sop_documents DROP COLUMN training_approved;
      ALTER TABLE tickets DROP CONSTRAINT ck_tickets_simulation_seed,
        ALTER COLUMN training_origin TYPE varchar(10),
        ADD CONSTRAINT ck_tickets_simulation_seed CHECK (
          (simulation_run_id IS NULL AND scenario_id IS NULL AND scenario_host_id IS NULL
           AND scenario_ticket_template_id IS NULL AND training_origin IS NULL) OR
          (simulation_run_id IS NOT NULL AND scenario_id IS NOT NULL AND scenario_host_id IS NOT NULL
           AND scenario_ticket_template_id IS NOT NULL AND training_origin IS NOT NULL AND shift_instance_id IS NULL)
        );
      REVOKE UPDATE (status) ON simulation_runs FROM app_runtime;
      DROP FUNCTION public.register_training_user(uuid, text, text, text);
      ALTER TABLE users DROP COLUMN role;
    """)

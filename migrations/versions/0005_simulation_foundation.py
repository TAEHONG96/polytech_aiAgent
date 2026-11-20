"""Add the simulation foundation and the first synthetic scenario."""

import sqlalchemy as sa
from alembic import op

revision = "0005_simulation_foundation"
down_revision = "0004_handover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE simulation_scenarios (
            id uuid PRIMARY KEY,
            scenario_key varchar(64) NOT NULL,
            version varchar(32) NOT NULL,
            title varchar(160) NOT NULL,
            timezone varchar(64) NOT NULL,
            virtual_start_at timestamptz NOT NULL,
            is_published boolean NOT NULL,
            config jsonb NOT NULL,
            CONSTRAINT uq_scenario_version UNIQUE (scenario_key, version)
        )
    """)
    op.execute("""
        CREATE TABLE scenario_shift_slots (
            id uuid PRIMARY KEY,
            scenario_id uuid NOT NULL REFERENCES simulation_scenarios(id),
            slot_order smallint NOT NULL,
            crew_code varchar(1) NOT NULL,
            shift_type varchar(5) NOT NULL,
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            actor_type varchar(7) NOT NULL,
            CONSTRAINT uq_scenario_slot_order UNIQUE (scenario_id, slot_order),
            CONSTRAINT ck_scenario_slot CHECK (
                slot_order BETWEEN 0 AND 2 AND crew_code IN ('A', 'B', 'C', 'D')
                AND shift_type IN ('DAY', 'NIGHT') AND starts_at < ends_at
                AND actor_type = CASE WHEN slot_order = 1 THEN 'TRAINEE' ELSE 'BOT' END
            )
        )
    """)
    op.execute("""
        CREATE TABLE scenario_hosts (
            id uuid PRIMARY KEY,
            scenario_id uuid NOT NULL REFERENCES simulation_scenarios(id),
            host_key varchar(64) NOT NULL,
            hostname varchar(160) NOT NULL,
            aliases jsonb NOT NULL,
            initial_state jsonb NOT NULL,
            CONSTRAINT uq_scenario_host_key UNIQUE (scenario_id, host_key),
            CONSTRAINT uq_scenario_host_id UNIQUE (scenario_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE scenario_ticket_templates (
            id uuid PRIMARY KEY,
            scenario_id uuid NOT NULL,
            scenario_host_id uuid NOT NULL,
            template_key varchar(64) NOT NULL,
            initial_ticket jsonb NOT NULL,
            detected_at timestamptz NOT NULL,
            origin varchar(10) NOT NULL,
            CONSTRAINT uq_scenario_ticket_key UNIQUE (scenario_id, template_key),
            CONSTRAINT uq_scenario_ticket_source UNIQUE (scenario_id, scenario_host_id, id, origin),
            CONSTRAINT fk_scenario_ticket_host FOREIGN KEY (scenario_id, scenario_host_id)
                REFERENCES scenario_hosts(scenario_id, id),
            CONSTRAINT ck_scenario_ticket_origin CHECK (origin IN ('INITIAL', 'CARRYOVER'))
        )
    """)
    op.execute("""
        CREATE TABLE simulation_runs (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            scenario_id uuid NOT NULL REFERENCES simulation_scenarios(id),
            status varchar(9) NOT NULL,
            started_at timestamptz NOT NULL,
            completed_at timestamptz,
            incoming_report_snapshot jsonb NOT NULL,
            start_request_key varchar(128) NOT NULL,
            CONSTRAINT uq_simulation_run_scenario UNIQUE (id, scenario_id),
            CONSTRAINT uq_simulation_run_request UNIQUE (user_id, start_request_key),
            CONSTRAINT ck_simulation_run_status CHECK (
                (status = 'ACTIVE' AND completed_at IS NULL)
                OR (status = 'COMPLETED' AND completed_at IS NOT NULL)
            )
        )
    """)
    op.create_index("uq_simulation_run_active", "simulation_runs", ["user_id"], unique=True,
                    postgresql_where=sa.text("status = 'ACTIVE'"))
    op.execute("""
        CREATE TABLE simulation_run_hosts (
            simulation_run_id uuid NOT NULL,
            scenario_host_id uuid NOT NULL,
            scenario_id uuid NOT NULL,
            state jsonb NOT NULL,
            PRIMARY KEY (simulation_run_id, scenario_host_id),
            CONSTRAINT uq_run_host_scenario UNIQUE (simulation_run_id, scenario_id, scenario_host_id),
            CONSTRAINT fk_run_host_run FOREIGN KEY (simulation_run_id, scenario_id)
                REFERENCES simulation_runs(id, scenario_id),
            CONSTRAINT fk_run_host_template FOREIGN KEY (scenario_id, scenario_host_id)
                REFERENCES scenario_hosts(scenario_id, id)
        )
    """)
    op.execute("""
        ALTER TABLE tickets
            ADD COLUMN simulation_run_id uuid,
            ADD COLUMN scenario_id uuid,
            ADD COLUMN scenario_host_id uuid,
            ADD COLUMN scenario_ticket_template_id uuid,
            ADD COLUMN training_origin varchar(10),
            ADD CONSTRAINT ck_tickets_simulation_seed CHECK (
                (simulation_run_id IS NULL AND scenario_id IS NULL AND scenario_host_id IS NULL
                 AND scenario_ticket_template_id IS NULL AND training_origin IS NULL)
                OR (simulation_run_id IS NOT NULL AND scenario_id IS NOT NULL AND scenario_host_id IS NOT NULL
                 AND scenario_ticket_template_id IS NOT NULL AND training_origin IS NOT NULL
                 AND shift_instance_id IS NULL)
            ),
            ADD CONSTRAINT fk_tickets_run_host FOREIGN KEY (simulation_run_id, scenario_id, scenario_host_id)
                REFERENCES simulation_run_hosts(simulation_run_id, scenario_id, scenario_host_id),
            ADD CONSTRAINT fk_tickets_scenario_template
                FOREIGN KEY (scenario_id, scenario_host_id, scenario_ticket_template_id, training_origin)
                REFERENCES scenario_ticket_templates(scenario_id, scenario_host_id, id, origin),
            ADD CONSTRAINT uq_tickets_run_template UNIQUE (simulation_run_id, scenario_ticket_template_id)
    """)
    protect_published_versions()
    seed()
    op.execute("GRANT SELECT ON simulation_scenarios, scenario_shift_slots, scenario_hosts, "
               "scenario_ticket_templates TO app_runtime")
    op.execute("GRANT SELECT, INSERT ON simulation_runs, simulation_run_hosts TO app_runtime")


def seed() -> None:
    op.execute("""
        INSERT INTO simulation_scenarios VALUES (
            'd1000000-0000-4000-8000-000000000001', 'dco-basic', '1.0',
            '데이터센터 장애 대응 기초', 'Asia/Seoul', '2026-11-20 08:00:00+09', false,
            '{"evaluation_rule_version":"dco-basic-1.0","evidence_versions":[],
              "incoming_report":{"summary":"야간에 시작된 스토리지 지연을 이어서 확인하세요.",
                                 "ticket_templates":["storage-latency"]}}'
        )
    """)
    op.execute("""
        INSERT INTO scenario_shift_slots VALUES
        ('d1000000-0000-4000-8000-000000000002', 'd1000000-0000-4000-8000-000000000001',
         0, 'D', 'NIGHT', '2026-11-19 20:00:00+09', '2026-11-20 08:00:00+09', 'BOT'),
        ('d1000000-0000-4000-8000-000000000003', 'd1000000-0000-4000-8000-000000000001',
         1, 'A', 'DAY', '2026-11-20 08:00:00+09', '2026-11-20 20:00:00+09', 'TRAINEE'),
        ('d1000000-0000-4000-8000-000000000004', 'd1000000-0000-4000-8000-000000000001',
         2, 'B', 'NIGHT', '2026-11-20 20:00:00+09', '2026-11-21 08:00:00+09', 'BOT')
    """)
    op.execute("""
        INSERT INTO scenario_hosts VALUES
        ('d1000000-0000-4000-8000-000000000005', 'd1000000-0000-4000-8000-000000000001',
         'ASSET-001', 'lab-storage-01', '[]', '{"health":"degraded"}'),
        ('d1000000-0000-4000-8000-000000000006', 'd1000000-0000-4000-8000-000000000001',
         'ASSET-002', 'lab-compute-01', '[]', '{"health":"degraded"}')
    """)
    op.execute("""
        INSERT INTO scenario_ticket_templates VALUES
        ('d1000000-0000-4000-8000-000000000007', 'd1000000-0000-4000-8000-000000000001',
         'd1000000-0000-4000-8000-000000000005', 'storage-latency',
         '{"title":"스토리지 응답 지연","description":"야간에 합성 응답 지연 경보가 발생했습니다.",
           "service":"storage","severity":"P2","status":"in_progress"}',
         '2026-11-20 06:20:00+09', 'CARRYOVER'),
        ('d1000000-0000-4000-8000-000000000008', 'd1000000-0000-4000-8000-000000000001',
         'd1000000-0000-4000-8000-000000000006', 'memory-alert',
         '{"title":"메모리 진단 경보","description":"합성 메모리 자산에서 진단 경보가 발생했습니다.",
           "service":"compute","severity":"P3","status":"open"}',
         '2026-11-20 08:00:00+09', 'INITIAL')
    """)

    op.execute("UPDATE simulation_scenarios SET is_published = true "
               "WHERE id = 'd1000000-0000-4000-8000-000000000001'")


def protect_published_versions() -> None:
    op.execute("""
        CREATE FUNCTION protect_simulation_scenario() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP <> 'INSERT' AND OLD.is_published THEN
                RAISE EXCEPTION 'Published scenario versions are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            IF NEW.is_published AND NOT EXISTS (
                SELECT 1 FROM scenario_shift_slots AS previous
                JOIN scenario_shift_slots AS current_slot USING (scenario_id)
                JOIN scenario_shift_slots AS following USING (scenario_id)
                WHERE previous.scenario_id = NEW.id AND previous.slot_order = 0
                  AND current_slot.slot_order = 1 AND following.slot_order = 2
                  AND previous.ends_at = current_slot.starts_at
                  AND current_slot.ends_at = following.starts_at
                  AND current_slot.starts_at = NEW.virtual_start_at
                  AND NOT EXISTS (
                      SELECT 1 FROM scenario_shift_slots AS slot
                      WHERE slot.scenario_id = NEW.id AND (
                          slot.ends_at - slot.starts_at <> interval '12 hours'
                          OR (slot.starts_at AT TIME ZONE NEW.timezone)::time <>
                              CASE WHEN slot.shift_type = 'DAY' THEN time '08:00' ELSE time '20:00' END
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'A published scenario requires three contiguous fixed shift slots';
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE FUNCTION protect_simulation_template() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE parent_ids uuid[] := ARRAY[]::uuid[];
                parent_published boolean;
        BEGIN
            IF TG_OP <> 'INSERT' THEN parent_ids := array_append(parent_ids, OLD.scenario_id); END IF;
            IF TG_OP <> 'DELETE' THEN parent_ids := array_append(parent_ids, NEW.scenario_id); END IF;
            FOR parent_published IN
                SELECT is_published FROM simulation_scenarios
                WHERE id = ANY(parent_ids) ORDER BY id FOR UPDATE
            LOOP
                IF parent_published THEN
                    RAISE EXCEPTION 'Published scenario templates are immutable';
                END IF;
            END LOOP;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("CREATE TRIGGER immutable_published_scenario BEFORE INSERT OR UPDATE OR DELETE ON simulation_scenarios "
               "FOR EACH ROW EXECUTE FUNCTION protect_simulation_scenario()")
    for table in ("scenario_shift_slots", "scenario_hosts", "scenario_ticket_templates"):
        op.execute(f"CREATE TRIGGER immutable_published_template BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION protect_simulation_template()")


def downgrade() -> None:
    op.execute("LOCK TABLE simulation_runs IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM simulation_runs) THEN
                RAISE EXCEPTION 'Cannot downgrade while simulation runs exist';
            END IF;
        END $$
    """)
    op.drop_constraint("fk_tickets_run_host", "tickets", type_="foreignkey")
    op.drop_constraint("fk_tickets_scenario_template", "tickets", type_="foreignkey")
    op.drop_constraint("ck_tickets_simulation_seed", "tickets", type_="check")
    op.drop_constraint("uq_tickets_run_template", "tickets", type_="unique")
    for column in ("training_origin", "scenario_ticket_template_id", "scenario_host_id",
                   "scenario_id", "simulation_run_id"):
        op.drop_column("tickets", column)
    for table in ("simulation_run_hosts", "simulation_runs", "scenario_ticket_templates", "scenario_hosts",
                  "scenario_shift_slots", "simulation_scenarios"):
        op.drop_table(table)
    op.execute("DROP FUNCTION protect_simulation_template()")
    op.execute("DROP FUNCTION protect_simulation_scenario()")

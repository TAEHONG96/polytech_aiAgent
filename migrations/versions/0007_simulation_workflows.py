"""Publish the practical scenario and extend the existing ticket audit."""
import json

from alembic import op
from sqlalchemy import text

revision = "0007_simulation_workflows"
down_revision = "0006_training_isolation"

PREVIOUS = "d1000000-0000-4000-8000-000000000001"
SCENARIO = "d3000000-0000-4000-8000-000000000001"

CATALOG = [
    {"code": "CHECK_SERVER_STATUS", "label": "서버 상태 확인", "variants": {
        "memory": {"message": "가상 서버 상태를 확인했습니다."},
        "storage": {"message": "가상 서버 상태를 확인했습니다."},
    }},
    {"code": "RUN_BMC_DIAGNOSTICS", "label": "BMC 진단", "variants": {
        "memory": {"parameter": {"target": "memory"}, "set": {"bmc_checked": True},
                   "message": "가상 메모리 진단을 실행했습니다."},
        "storage": {"parameter": {"target": "storage"}, "set": {"bmc_checked": True},
                    "message": "가상 스토리지 진단을 실행했습니다."},
    }},
    {"code": "CHECK_MEMORY", "label": "메모리 점검", "variants": {
        "memory": {"requires": {"bmc_checked": True}, "blocked": "먼저 BMC 메모리 진단을 실행하세요.",
                   "set": {"memory_checked": True}, "message": "가상 메모리 슬롯의 오류를 확인했습니다."},
    }},
    {"code": "CHECK_STORAGE", "label": "스토리지 점검", "variants": {
        "storage": {"requires": {"bmc_checked": True}, "blocked": "먼저 BMC 스토리지 진단을 실행하세요.",
                    "set": {"storage_checked": True}, "message": "가상 스토리지의 지연을 확인했습니다."},
    }},
    {"code": "REPLACE_MEMORY", "label": "가상 메모리 교체", "variants": {
        "memory": {"requires": {"memory_checked": True, "replacement_done": False, "error_code": "MEM_ECC_ERROR"},
                   "blocked": "메모리 오류를 점검하고 미교체 상태인지 확인하세요.",
                   "set": {"replacement_done": True, "health": "verification_pending"},
                   "message": "가상 메모리를 교체했습니다. 재진단이 필요합니다."},
    }},
    {"code": "RERUN_DIAGNOSTICS", "label": "재진단", "variants": {
        "memory": {"requires": {"replacement_done": True}, "blocked": "가상 메모리를 교체한 뒤 재진단하세요.",
                   "set": {"verified": True, "health": "healthy", "error_code": None},
                   "message": "가상 재진단을 통과했습니다."},
        "storage": {"requires": {"storage_checked": True}, "blocked": "스토리지 점검 후 재진단하세요.",
                    "set": {"verified": True}, "message": "가상 재진단에서 지연이 계속되었습니다."},
    }},
]


def upgrade() -> None:
    op.execute("""
      ALTER TABLE simulation_runs ADD COLUMN last_event_sequence bigint NOT NULL DEFAULT 0;
      ALTER TABLE tickets ADD CONSTRAINT uq_tickets_run_id UNIQUE (simulation_run_id, id);
      ALTER TABLE ticket_events ADD COLUMN simulation_run_id uuid,
        ADD COLUMN event_sequence bigint, ADD COLUMN action_request_key varchar(128),
        ADD COLUMN action_request_digest varchar(64);
      WITH historical AS (
        SELECT e.id, t.simulation_run_id,
          row_number() OVER (PARTITION BY t.simulation_run_id ORDER BY e.created_at, e.ticket_id, e.ticket_version) AS seq
        FROM ticket_events e JOIN tickets t ON t.id = e.ticket_id WHERE t.simulation_run_id IS NOT NULL
      )
      UPDATE ticket_events e SET simulation_run_id=h.simulation_run_id, event_sequence=h.seq FROM historical h WHERE h.id=e.id;
      UPDATE simulation_runs r SET last_event_sequence=e.seq FROM (
        SELECT simulation_run_id, max(event_sequence) AS seq FROM ticket_events GROUP BY simulation_run_id
      ) e WHERE r.id=e.simulation_run_id;
      ALTER TABLE ticket_events
        ADD CONSTRAINT uq_training_event_sequence UNIQUE (simulation_run_id, event_sequence),
        ADD CONSTRAINT uq_training_action_request UNIQUE (simulation_run_id, action_request_key),
        ADD CONSTRAINT fk_training_event_ticket FOREIGN KEY (simulation_run_id, ticket_id) REFERENCES tickets(simulation_run_id, id),
        ADD CONSTRAINT ck_training_event_scope CHECK ((simulation_run_id IS NULL) = (event_sequence IS NULL)),
        ADD CONSTRAINT ck_training_action_request CHECK (
          (action_request_key IS NULL AND action_request_digest IS NULL
           AND event_type NOT IN ('virtual_action_performed', 'training_note')) OR
          (action_request_key IS NOT NULL AND action_request_digest IS NOT NULL AND simulation_run_id IS NOT NULL
           AND event_type IN ('virtual_action_performed', 'training_note'))
        );
      GRANT UPDATE (last_event_sequence) ON simulation_runs TO app_runtime;
      GRANT UPDATE (state) ON simulation_run_hosts TO app_runtime;
      CREATE FUNCTION allocate_training_event_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        SELECT simulation_run_id INTO NEW.simulation_run_id FROM tickets WHERE id=NEW.ticket_id;
        NEW.event_sequence := NULL;
        IF NEW.simulation_run_id IS NOT NULL THEN
          UPDATE simulation_runs SET last_event_sequence=last_event_sequence+1
            WHERE id=NEW.simulation_run_id AND status='ACTIVE' RETURNING last_event_sequence INTO NEW.event_sequence;
          IF NEW.event_sequence IS NULL THEN
            RAISE EXCEPTION 'Cannot append a training event to a completed run';
          END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER allocate_training_event_before_insert BEFORE INSERT ON ticket_events
        FOR EACH ROW EXECUTE FUNCTION allocate_training_event_sequence();
      ALTER TABLE scenario_incidents ADD COLUMN incident_key varchar(64), ADD COLUMN error_code varchar(64),
        ADD CONSTRAINT uq_scenario_incident_key UNIQUE (scenario_id, incident_key);
    """)
    publish()


def publish() -> None:
    db = op.get_bind()
    values = {"previous": PREVIOUS, "scenario": SCENARIO, "catalog": json.dumps(CATALOG)}
    db.execute(text("""
        INSERT INTO simulation_scenarios (id, scenario_key, version, title, timezone, virtual_start_at, is_published, config)
        SELECT :scenario, scenario_key, '1.1', title, timezone, virtual_start_at, false,
               config || jsonb_build_object('action_catalog', CAST(:catalog AS jsonb),
                                             'evaluation_rule_version', 'dco-basic-1.1')
        FROM simulation_scenarios WHERE id=:previous
    """), values)
    db.execute(text("""
        INSERT INTO scenario_shift_slots
        SELECT md5(CAST(id AS text) || '-v1.1')::uuid, :scenario, slot_order, crew_code, shift_type, starts_at, ends_at, actor_type
        FROM scenario_shift_slots WHERE scenario_id=:previous
    """), values)
    db.execute(text("""
        INSERT INTO scenario_hosts
        SELECT md5(CAST(id AS text) || '-v1.1')::uuid, :scenario, host_key, hostname, aliases,
               initial_state || CASE host_key
                 WHEN 'ASSET-002' THEN jsonb_build_object('asset_kind','memory','error_code','MEM_ECC_ERROR','replacement_done',false)
                 ELSE jsonb_build_object('asset_kind','storage','error_code','STORAGE_LATENCY') END
        FROM scenario_hosts WHERE scenario_id=:previous
    """), values)
    db.execute(text("""
        INSERT INTO scenario_ticket_templates
        SELECT md5(CAST(id AS text) || '-v1.1')::uuid, :scenario, md5(CAST(scenario_host_id AS text) || '-v1.1')::uuid,
               template_key, initial_ticket, detected_at, origin
        FROM scenario_ticket_templates WHERE scenario_id=:previous
    """), values)
    op.execute("""
      INSERT INTO scenario_incidents (id, scenario_id, scenario_host_id, occurred_at, title, resolution, approved,
                                      incident_key, error_code)
      SELECT ids.id::uuid, h.scenario_id, h.id, ids.at::timestamptz, ids.title, ids.resolution, ids.approved, ids.key, ids.code
      FROM scenario_hosts h JOIN (VALUES
        ('d3000000-0000-4000-8000-000000000011','ASSET-001','2026-10-22 03:30:00+09',
         '합성 스토리지 지연','가상 대기열과 응답 지연을 비교하고 미해결 항목을 넘겼습니다.',true,'SYN-STOR-01','STORAGE_LATENCY'),
        ('d3000000-0000-4000-8000-000000000012','ASSET-002','2026-10-21 09:30:00+09',
         '합성 메모리 ECC 오류','가상 메모리 점검과 교체 뒤 재진단으로 오류가 해소되었습니다.',true,'SYN-MEM-01','MEM_ECC_ERROR'),
        ('d3000000-0000-4000-8000-000000000013','ASSET-002','2026-11-20 08:00:00+09',
         '합성 동시각 사고','시점 경계 검증 자료입니다.',true,'SYN-MEM-AT','MEM_ECC_ERROR'),
        ('d3000000-0000-4000-8000-000000000014','ASSET-002','2026-11-21 08:00:00+09',
         '합성 미래 사고','미래 시점 격리 검증 자료입니다.',true,'SYN-MEM-AFTER','MEM_ECC_ERROR'),
        ('d3000000-0000-4000-8000-000000000015','ASSET-002','2026-10-20 08:00:00+09',
         '합성 미승인 사고','자료 승인 경계 검증 자료입니다.',false,'SYN-MEM-PRIVATE','MEM_ECC_ERROR')
      ) AS ids(id, host, at, title, resolution, approved, key, code) ON h.host_key=ids.host
      WHERE h.scenario_id='d3000000-0000-4000-8000-000000000001';
      UPDATE simulation_scenarios SET is_published=true WHERE id='d3000000-0000-4000-8000-000000000001';
      CREATE FUNCTION protect_practical_incident() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP <> 'INSERT' AND OLD.scenario_id='d3000000-0000-4000-8000-000000000001' THEN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Published practical incident is immutable'; END IF;
          IF ROW(OLD.id, OLD.scenario_id, OLD.scenario_host_id, OLD.occurred_at, OLD.incident_key,
                 OLD.error_code, OLD.title, OLD.resolution)
             IS DISTINCT FROM ROW(NEW.id, NEW.scenario_id, NEW.scenario_host_id, NEW.occurred_at, NEW.incident_key,
                                  NEW.error_code, NEW.title, NEW.resolution) THEN
            RAISE EXCEPTION 'Published practical incident is immutable';
          END IF;
        ELSIF TG_OP <> 'DELETE' AND NEW.scenario_id='d3000000-0000-4000-8000-000000000001' THEN
          RAISE EXCEPTION 'Published practical incident corpus is closed';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER protect_practical_incident_before_write BEFORE INSERT OR UPDATE OR DELETE ON scenario_incidents
        FOR EACH ROW EXECUTE FUNCTION protect_practical_incident();
    """)
    index_fake_incidents(db)


def index_fake_incidents(db) -> None:
    # This seed uses the repository's local, deterministic Fake profile; no network is needed.
    from app.knowledge.embedding import FakeEmbedder

    embedder = FakeEmbedder()
    incidents = db.execute(text("SELECT id, title, resolution FROM scenario_incidents WHERE scenario_id=:scenario"),
                           {"scenario": SCENARIO}).all()
    for incident in incidents:
        vector, = embedder.documents(incident.title, [incident.resolution])
        db.execute(text("""
            UPDATE scenario_incidents SET embedding=CAST(:vector AS vector), embedding_profile=:profile WHERE id=:id
        """), {"vector": json.dumps(vector), "profile": embedder.profile.fingerprint, "id": incident.id})


def downgrade() -> None:
    raise RuntimeError("Restore the matching pre-migration backup to preserve published versions and activity history.")

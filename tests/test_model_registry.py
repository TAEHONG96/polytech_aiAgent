import subprocess
import sys

import pytest


@pytest.mark.parametrize("entry_point", ["app.knowledge.demo", "app.agent.evaluation", "app.knowledge.worker", "main"])
def test_db_entry_point_registers_cross_domain_foreign_keys(entry_point):
    script = """
import importlib
import sys

importlib.import_module(sys.argv[1])
from app.models import Base

tables = Base.metadata.tables
assert {'tickets', 'scenario_ticket_templates', 'simulation_run_hosts', 'index_jobs', 'agent_runs', 'handovers'} <= tables.keys()
Base.registry.configure()
for table in tables.values():
    for foreign_key in table.foreign_keys:
        assert foreign_key.column.table.metadata is Base.metadata
"""
    result = subprocess.run([sys.executable, "-c", script, entry_point], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

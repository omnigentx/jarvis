"""Pin the canonical agent-name normalization behavior.

Memory ownership keys on ``normalize_agent_name``; every SSE event also
flows through it. Behavior must not drift.
"""

from helpers.agent_identity import normalize_agent_name
from services import sse_progress


def test_strips_trailing_instance_suffix():
    assert normalize_agent_name("FinanceAgent[1]") == "FinanceAgent"
    assert normalize_agent_name("FinanceAgent[2]") == "FinanceAgent"
    assert normalize_agent_name("FinanceAgent[42]") == "FinanceAgent"


def test_keeps_team_role_suffix():
    # A role suffix is part of the identity and must survive.
    assert normalize_agent_name("Khoi [SA]") == "Khoi [SA]"
    # Instance suffix stripped, role suffix kept.
    assert normalize_agent_name("Khoi [SA][1]") == "Khoi [SA]"


def test_noop_when_no_suffix():
    assert normalize_agent_name("Jarvis") == "Jarvis"
    assert normalize_agent_name("") == ""


def test_only_trailing_suffix_stripped():
    # A bracketed number not at the end is left alone.
    assert normalize_agent_name("Agent[1]X") == "Agent[1]X"


def test_sse_progress_reexport_is_same_callable():
    # Backward-compat: importers of services.sse_progress keep working.
    assert sse_progress.normalize_agent_name is normalize_agent_name

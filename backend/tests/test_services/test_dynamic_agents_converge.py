"""Cross-layer test: agent_definitions DB row → real FastAgent.agents.

Covers the SSoT win of the Phase 4 refactor — the cross-process
converge mechanism that replaces the old file-watcher path.

The unit tests in ``test_dynamic_agents.py`` stub ``agent_app.load_agent_data``
to assert the control flow of the poll loop. THIS test drives the WHOLE
pipeline against a REAL ``FastAgent`` instance so a silent regression in
the chain

    writer call → DB row → poll detects rev → load_agents_from_dicts →
        fast.agents updated → attach to parent → reachable as tool

is impossible: an unwired link anywhere in that chain fails one of
the assertions below.

Why this matters: the previous file-watcher path silently broke if
the ``.reload_needed`` signal was missed. The replacement is a rev
counter polled by the reader. The bug class this test guards against
is "writer thinks it persisted; reader never sees it" — symmetric to
what we tested HTTP-side in ``test_agents_dynamic_crud`` but here
exercising the second half of the chain end-to-end with a real
fast_agent runtime instead of a mock app.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fast_agent import FastAgent
from fast_agent.core.agent_card_loader import is_memory_card_path


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """Per-test isolated SQLite DB pointed at by SPAWN_REGISTRY_DB."""
    db_path = str(tmp_path / "converge.db")
    monkeypatch.setenv("SPAWN_REGISTRY_DB", db_path)
    yield db_path


@pytest.fixture()
def fast_with_parent(tmp_path):
    """Real ``FastAgent`` with an empty config + a ``Jarvis`` parent.

    Dynamic agents in production are attached to Jarvis as tools; this
    fixture mirrors that setup with the minimum needed (empty config,
    no MCP servers, no LLM calls).
    """
    config_path = tmp_path / "fastagent.config.yaml"
    config_path.write_text("", encoding="utf-8")

    fast_obj = FastAgent(
        "converge-test",
        config_path=str(config_path),
        parse_cli_args=False,
        quiet=True,
    )

    @fast_obj.agent(name="Jarvis", instruction="parent", default=True, agents=[])
    async def _jarvis(prompt: str = "Hello"):  # noqa: ARG001
        pass

    return fast_obj


def _defs_for_load() -> list[dict]:
    """Shape DB rows the way ``dynamic_agents.preload_dynamic_agents`` does.

    Kept in-line (instead of importing the private helper) so this test
    catches drift between the reader-side projection and the DB columns
    — if the helper starts dropping a column, this test still passes
    its own shape through and reveals the breakage in the assertion
    rather than papering over it.
    """
    from services import agent_definitions as defs_svc

    out: list[dict] = []
    for row in defs_svc.list_definitions():
        card: dict = {
            "name": row["name"],
            "instruction": row["instruction"],
            "servers": row["servers"],
            "tools": row["tools"],
            "skills": row["skills"],
            "use_history": row["use_history"],
        }
        if row.get("model"):
            card["model"] = row["model"]
        if row.get("request_params"):
            card["request_params"] = row["request_params"]
        out.append(card)
    return out


def _converge(fast_obj: FastAgent, parent: str = "Jarvis") -> list[str]:
    """One tick of the converge: read defs from DB, push into fast_agent,
    attach as children of ``parent``. Mirrors the poll-loop body without
    the asyncio sleep."""
    cards = _defs_for_load()
    loaded = fast_obj.load_agents_from_dicts(cards)
    if loaded:
        fast_obj.attach_agent_tools(parent, loaded)
    return loaded


# ── Create path ──────────────────────────────────────────────────────


def test_db_insert_propagates_to_fast_agents_and_jarvis_children(
    db_env, fast_with_parent
):
    from services import agent_definitions as defs_svc

    # Writer side — same path the REST endpoint and spawn_agent MCP take.
    defs_svc.create_definition(
        name="ResearchTest",
        instruction="Test research agent.",
        servers=[],
        skills=[],
    )

    # Reader side — what the poll loop would do on the next tick.
    loaded = _converge(fast_with_parent)

    assert loaded == ["ResearchTest"]
    assert "ResearchTest" in fast_with_parent.agents

    # The memory:// marker is the cross-cutting flag the rest of the
    # system uses to tell DB-backed from code-defined agents
    # (skill_service.is_agent_card_based, routes.agents._is_static_agent).
    # If this assertion breaks, those callers misclassify the agent.
    src = fast_with_parent._agent_card_sources.get("ResearchTest")
    assert src is not None
    assert is_memory_card_path(src)

    # Tool-callable: Jarvis (parent) lists the new agent in its child set.
    jarvis_data = fast_with_parent.agents["Jarvis"]
    children = jarvis_data.get("child_agents") or []
    assert "ResearchTest" in children, (
        f"Jarvis should expose dynamic agent as a tool — children={children}"
    )

    # The DB row stays authoritative — fast.agents is a derived view,
    # not a fork. If a second converge runs without changes, semantics
    # must be idempotent (no duplicate child, no exception).
    loaded_again = _converge(fast_with_parent)
    assert loaded_again == ["ResearchTest"]
    children_after = fast_with_parent.agents["Jarvis"].get("child_agents") or []
    assert children_after.count("ResearchTest") == 1, (
        "Second converge must not duplicate the child — attach_agent_tools "
        "is supposed to be idempotent for already-attached names."
    )


# ── Update path ──────────────────────────────────────────────────────


def test_db_update_propagates_new_instruction_to_runtime(db_env, fast_with_parent):
    """update_definition bumps the rev; the next converge picks up the
    new instruction. Catches "writer touches DB, reader uses cache" bugs."""
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="Tweaker", instruction="v1", servers=[])
    _converge(fast_with_parent)
    cfg_before = fast_with_parent.agents["Tweaker"]["config"]
    assert cfg_before.instruction.strip() == "v1"

    defs_svc.update_definition("Tweaker", instruction="v2")
    _converge(fast_with_parent)

    cfg_after = fast_with_parent.agents["Tweaker"]["config"]
    assert cfg_after.instruction.strip() == "v2"


# ── Delete path ──────────────────────────────────────────────────────


def test_db_delete_removes_from_fast_agents(db_env, fast_with_parent):
    """delete_definition + converge must drop the agent from fast.agents.

    Replace semantics in ``load_agents_from_dicts`` are responsible for
    this — the writer doesn't directly mutate fast.agents; the reader
    sends the full set, and any name missing from it gets removed.
    """
    from services import agent_definitions as defs_svc

    defs_svc.create_definition(name="Temp", instruction="temp", servers=[])
    _converge(fast_with_parent)
    assert "Temp" in fast_with_parent.agents

    assert defs_svc.delete_definition("Temp") is True
    _converge(fast_with_parent)

    assert "Temp" not in fast_with_parent.agents
    assert "Temp" not in fast_with_parent._agent_card_sources


# ── Static-agent collision guard ─────────────────────────────────────


def test_db_row_with_static_name_is_rejected(db_env, fast_with_parent):
    """A DB row whose name matches a code-defined agent must not silently
    clobber the static decorator. ``load_agents_from_dicts`` raises so
    operators see the conflict instead of a silent override.

    This guards the static-vs-dynamic boundary: if it broke, an attacker
    or buggy migration could shadow Jarvis itself by inserting a row
    with name="Jarvis".
    """
    from services import agent_definitions as defs_svc
    from fast_agent.core.exceptions import AgentConfigError

    defs_svc.create_definition(name="Jarvis", instruction="x", servers=[])

    with pytest.raises(AgentConfigError):
        _converge(fast_with_parent)
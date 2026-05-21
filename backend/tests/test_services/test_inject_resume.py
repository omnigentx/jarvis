"""Regression tests for ``services.inject_resume``.

Guards the auto-wake-orchestrator path that broke production
(b61af7db incident, 2026-05-09): when all team members went idle,
``_trigger_orchestrator_resume`` failed silently with::

    ImportError: cannot import name 'DisplayManager' from
    'fast_agent.spawn.spawn_display'

The class had been renamed to ``SpawnDisplayManager`` but
``inject_resume._create_bridge_display`` was never updated. The exception
was swallowed by ``_trigger_orchestrator_resume``'s try/except, surviving
only as a WARNING in ``spawn_activity.log`` — meanwhile the orchestrator
was never woken to read the team-status notification sitting in its inbox.

Also guards the ``server_overrides`` forwarding path (2026-05-17 incident):
``resume_with_inject`` is the dashboard "Send message to agent" code path.
Earlier work fixed ``_check_and_resume_on_inbox`` / ``restart_spawn`` /
``resume_spawn`` to forward ``server_overrides``, but ``inject_resume.py``
was missed → every dashboard inject silently fell back to the base
``fastagent.config.yaml`` filesystem args (``./data`` only) and the agent
lost workspace + skills access. Pin the forward here so the next refactor
catches it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_create_bridge_display_imports_correct_class():
    """Smoke-test the import path that was stale.

    The bug: ``inject_resume.py`` imported ``DisplayManager`` (renamed to
    ``SpawnDisplayManager``). This test calls ``_create_bridge_display``
    with a mock bridge and asserts that the import + instantiation +
    ``set_event_callback`` chain all succeed.
    """
    from services.inject_resume import _create_bridge_display

    mock_bridge = MagicMock()
    dm = _create_bridge_display(mock_bridge)

    # Verify the right class was imported and instantiated.
    from fast_agent.spawn.spawn_display import SpawnDisplayManager
    assert isinstance(dm, SpawnDisplayManager), (
        f"Expected SpawnDisplayManager, got {type(dm).__name__}"
    )

    # Verify the event callback was attached — without it, child
    # subprocess events never reach the bridge.
    assert dm._event_callback is not None, (
        "set_event_callback never ran — events from resumed agent "
        "would not flow to SpawnProgressBridge"
    )


def test_create_bridge_display_forwards_events_to_bridge():
    """End-to-end: SpawnEvent fired through DM lands in bridge.process_event."""
    import json

    from services.inject_resume import _create_bridge_display

    mock_bridge = MagicMock()
    dm = _create_bridge_display(mock_bridge)

    # Build a SpawnEvent-like object (duck-typed — uses getattr fallbacks).
    fake_event = MagicMock()
    fake_event.agent_name = "Cameron [PM]"
    fake_event.event = "agent_ready"
    fake_event.run_id = "abc12345"
    fake_event.data = {"foo": "bar"}

    dm._event_callback(fake_event)

    mock_bridge.process_event.assert_called_once()
    sent_line = mock_bridge.process_event.call_args[0][0]
    payload = json.loads(sent_line)
    assert payload["agent_name"] == "Cameron [PM]"
    assert payload["event_type"] == "agent_ready"
    assert payload["run_id"] == "abc12345"
    assert payload["data"] == {"foo": "bar"}


# ─── server_overrides forwarding (2026-05-17 incident) ────────────────────────
#
# These tests pin the dashboard inject code path. The bug was a single missing
# kwarg on the ``run_isolated_agent_background`` call inside
# ``resume_with_inject`` — without the test, a future refactor that touches
# the call signature could silently drop ``server_overrides`` again and every
# subsequent inject would point filesystem MCP at the wrong directory.


@pytest.fixture
def _baseline_original_config():
    """Original config a team-spawned agent would have in its spawn_record.

    Mirrors what ``isolated_spawner._register_spawn`` persists into
    ``original_config_json`` for a team agent. The presence of
    ``server_overrides`` here is what we want forwarded.
    """
    return {
        "task": "previous task",
        "instruction": "You are a designer.",
        "context": "",
        "servers": ["filesystem", "meeting_room", "email"],
        "skills": ["figma-designer"],
        "model": "",
        "timeout_seconds": 0,
        "role": "designer",
        "agent_name": "Jesse [Designer]",
        "team_name": "team-alpha",
        "workspace_dir": "/ws/team-alpha",
        "env_vars": {"TEAM_SESSION_ID": "sess-1", "TEAM_MY_NAME": "Jesse [Designer]"},
        "project_dir": "/proj",
        "server_overrides": {
            "filesystem": {
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "/ws/team-alpha",
                    "/proj/.fast-agent/skills",
                ],
            },
        },
    }


@pytest.mark.asyncio
async def test_inject_resume_forwards_server_overrides(_baseline_original_config):
    """resume_with_inject must pass server_overrides from original_config.

    Without this forward, the dashboard "Send message" path falls back to
    base config defaults → agent loses per-role filesystem args. Pin by
    asserting the exact dict propagates to run_isolated_agent_background.
    """
    from services import inject_resume

    spawn_record = {
        "agent_name": "Jesse [Designer]",
        "team_name": "team-alpha",
        "original_config": _baseline_original_config,
    }

    captured_kwargs: dict = {}

    async def fake_run_isolated(**kwargs):
        captured_kwargs.update(kwargs)
        return "run_new_123"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        result = await inject_resume.resume_with_inject(
            agent_name="Jesse [Designer]",
            inject_message="export the figma to png and attach to Jira",
            spawn_record=spawn_record,
            bridge=None,
        )

    assert result["status"] == "resumed"
    assert result["run_id"] == "run_new_123"

    # The pin: server_overrides must be present AND equal to original_config's.
    assert "server_overrides" in captured_kwargs, (
        "resume_with_inject dropped server_overrides — every dashboard inject "
        "will spawn the agent with base fastagent.config.yaml filesystem args "
        "(./data only) and lose workspace + skills access."
    )
    assert captured_kwargs["server_overrides"] == _baseline_original_config["server_overrides"]
    # Sanity: workspace_dir + agent identity also still forwarded.
    assert captured_kwargs["workspace_dir"] == "/ws/team-alpha"
    assert captured_kwargs["agent_name"] == "Jesse [Designer]"
    assert captured_kwargs["team_name"] == "team-alpha"


@pytest.mark.asyncio
async def test_inject_resume_passes_none_when_no_overrides(_baseline_original_config):
    """Non-team / ad-hoc spawn → no server_overrides in original_config.

    The forward must not invent a value; it must pass None so
    run_isolated_agent_background falls through to base defaults (the
    correct behaviour when the agent never had role-specific overrides).
    """
    from services import inject_resume

    cfg = dict(_baseline_original_config)
    cfg.pop("server_overrides")

    spawn_record = {
        "agent_name": "ad-hoc-agent",
        "team_name": "",
        "original_config": cfg,
    }

    captured_kwargs: dict = {}

    async def fake_run_isolated(**kwargs):
        captured_kwargs.update(kwargs)
        return "run_new_456"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        await inject_resume.resume_with_inject(
            agent_name="ad-hoc-agent",
            inject_message="continue",
            spawn_record=spawn_record,
            bridge=None,
        )

    # Pin the explicit-None behaviour — empty dict or omission would also
    # confuse isolated_spawner; ``None`` is the documented sentinel for "no
    # override, use base config".
    assert captured_kwargs.get("server_overrides") is None


@pytest.mark.asyncio
async def test_inject_resume_explicit_none_in_config_forwarded_as_none(_baseline_original_config):
    """Defence-in-depth: an explicit None (vs missing key) must also forward as None.

    Some upstream paths may write ``server_overrides: null`` instead of
    omitting the key entirely. The ``or None`` coalesce in inject_resume
    must collapse both to the same forward value.
    """
    from services import inject_resume

    cfg = dict(_baseline_original_config)
    cfg["server_overrides"] = None

    captured_kwargs: dict = {}

    async def fake_run_isolated(**kwargs):
        captured_kwargs.update(kwargs)
        return "run_new_789"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        await inject_resume.resume_with_inject(
            agent_name="Jesse [Designer]",
            inject_message="continue",
            spawn_record={"agent_name": "Jesse [Designer]", "original_config": cfg},
            bridge=None,
        )

    assert captured_kwargs.get("server_overrides") is None


# ─── template-driven resume (2026-05-18 incident) ─────────────────────────────
#
# After incident 2026-05-18: the patch script + /reload endpoint correctly
# updated ``team_sessions.template.roles.qe.servers`` to add ``playwright``,
# but every subsequent QE resume still passed the stale 7-server list to
# ``run_isolated_agent_background`` because ``resume_with_inject`` was
# reading from the FROZEN ``original_config.servers`` snapshot rather than
# the live template. Result: agent had no playwright tools and silently
# fell back to scrapling-server.
#
# Root cause was an SSoT violation — two truths for "what servers does QE
# have", the writer/patch updated one, the reader/resume read the other.
# These tests pin the fix: when the live template diverges from the
# snapshot, resume MUST use the template values.


@pytest.fixture
def _team_config_with_playwright():
    """Original config snapshot taken BEFORE playwright was added.

    Represents the production state on 2026-05-18: agent first spawned
    when the template had 7 servers, then the operator patched the
    template to add playwright. The snapshot is still 7 — that's the
    point. The reader must NOT trust this anymore for team agents.
    """
    return {
        "task": "test",
        "instruction": "OLD instruction",
        "context": "",
        "servers": ["filesystem", "meeting_room", "email", "scrapling-server"],
        "skills": [],
        "model": "",
        "timeout_seconds": 0,
        "role": "qe",
        "agent_name": "Eden [QE]",
        "team_name": "agile-team",
        "workspace_dir": "/ws/agile-team",
        "env_vars": {"TEAM_SESSION_ID": "sess-be885ae8", "TEAM_MY_NAME": "Eden [QE]"},
        "project_dir": "/proj",
        "server_overrides": {"filesystem": {"args": ["-y", "x", "/ws"]}},
    }


@pytest.mark.asyncio
async def test_resume_prefers_live_template_servers_over_snapshot(
    _team_config_with_playwright,
):
    """Pin the 2026-05-18 fix: when team_session_id resolves a live template,
    spawn passes the TEMPLATE's servers list — not the original_config snapshot.

    Without this, the dashboard /reload flow is broken: user edits the
    template to add a server, reload kills + resumes the agent, but the
    new agent still has the old server list. The bug was visible to the
    user as "QE doesn't see playwright tools even though the template says
    so" and required tail-following two SQL rows to diagnose.
    """
    from services import inject_resume

    fake_session = MagicMock()
    fake_session.template = {
        "roles": {
            "qe": {
                "servers": [
                    "filesystem", "meeting_room", "email",
                    "scrapling-server", "playwright",  # ← new
                ],
                "server_overrides": {"filesystem": {"args": ["-y", "x", "/ws"]}},
                "instruction": "NEW instruction",
                "skills": ["browser-testing"],
                "model": "",
            },
        },
    }

    captured: dict = {}

    async def fake_run_isolated(**kwargs):
        captured.update(kwargs)
        return "run_new_001"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch("fast_agent.spawn.team_spawner.get_team_session", return_value=fake_session), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        await inject_resume.resume_with_inject(
            agent_name="Eden [QE]",
            inject_message="test",
            spawn_record={
                "agent_name": "Eden [QE]",
                "team_name": "agile-team",
                "original_config": _team_config_with_playwright,
            },
            bridge=None,
        )

    assert "playwright" in captured["servers"], (
        f"resume_with_inject passed servers={captured['servers']!r} — the live "
        f"team template added 'playwright' but resume still read the stale "
        f"original_config snapshot. SSoT violation reintroduced."
    )
    assert captured["instruction"] == "NEW instruction", (
        "Instruction edits in the template must reach the next resume."
    )
    assert captured["skills"] == ["browser-testing"], (
        "Skill edits in the template must reach the next resume."
    )


@pytest.mark.asyncio
async def test_resume_falls_back_to_snapshot_when_template_missing(
    _team_config_with_playwright,
):
    """If the team session was deleted (or the role was removed from the
    template after the agent spawned), resume MUST fall back to the snapshot
    rather than crash. This preserves the ability to inspect / wind down
    legacy agents whose template no longer exists.
    """
    from services import inject_resume

    captured: dict = {}

    async def fake_run_isolated(**kwargs):
        captured.update(kwargs)
        return "run_new_002"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch("fast_agent.spawn.team_spawner.get_team_session", return_value=None), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        await inject_resume.resume_with_inject(
            agent_name="Eden [QE]",
            inject_message="test",
            spawn_record={
                "agent_name": "Eden [QE]",
                "team_name": "agile-team",
                "original_config": _team_config_with_playwright,
            },
            bridge=None,
        )

    assert captured["servers"] == _team_config_with_playwright["servers"]
    assert captured["instruction"] == "OLD instruction"


@pytest.mark.asyncio
async def test_resume_for_ad_hoc_agent_does_not_lookup_template(
    _team_config_with_playwright,
):
    """Ad-hoc spawns (no TEAM_SESSION_ID in env_vars) MUST NOT trigger the
    template lookup — they don't belong to any team. Pin this so the
    template-load helper doesn't sneak its way into the ad-hoc path and
    add a needless DB read per resume.
    """
    from services import inject_resume

    cfg = dict(_team_config_with_playwright)
    cfg["env_vars"] = {}  # ad-hoc — no team session
    cfg["team_name"] = ""

    captured: dict = {}
    template_lookups = MagicMock()

    async def fake_run_isolated(**kwargs):
        captured.update(kwargs)
        return "run_new_003"

    with patch.object(inject_resume, "_load_context_from_db", return_value=None), \
         patch.object(inject_resume, "_create_registry", return_value=MagicMock()), \
         patch.object(inject_resume, "_reinject_team_context", side_effect=lambda m, *a, **k: m), \
         patch("fast_agent.spawn.team_spawner.get_team_session", new=template_lookups), \
         patch(
             "fast_agent.spawn.isolated_spawner.run_isolated_agent_background",
             new=AsyncMock(side_effect=fake_run_isolated),
         ):
        await inject_resume.resume_with_inject(
            agent_name="ad-hoc",
            inject_message="test",
            spawn_record={"agent_name": "ad-hoc", "original_config": cfg},
            bridge=None,
        )

    template_lookups.assert_not_called()
    # Snapshot values survive — ad-hoc agents have no template.
    assert captured["servers"] == cfg["servers"]


def test_audit_all_run_isolated_agent_background_callers_forward_overrides():
    """Static audit: every caller of run_isolated_agent_background that
    handles a TEAM-managed agent must forward server_overrides.

    This guard catches NEW call sites added in future refactors. Without it
    the bug-class repeats: 5 of 6 paths fixed, 1 silent miss. We hard-code
    the known team-aware call sites — if a new one is added without
    forwarding overrides, the assertion message tells the author exactly
    which test fixture to update and which kwarg they forgot.
    """
    import re
    from pathlib import Path

    # Paths that legitimately do NOT need server_overrides (generic ad-hoc
    # spawn tools that never had role-specific config). Keep this list tight.
    EXEMPT_FILES = {
        "fast_agent/spawn/servers/agent_spawner_server.py",  # _spawn_agent_background MCP tool
    }

    # All files we expect to forward overrides when they call the spawner.
    REQUIRED_FILES = [
        "backend/services/inject_resume.py",
        "backend/fast-agent/src/fast_agent/spawn/team_spawner.py",
        "backend/fast-agent/src/fast_agent/spawn/isolated_spawner.py",
    ]

    project_root = Path(__file__).resolve().parents[3]
    misses = []
    for rel in REQUIRED_FILES:
        f = project_root / rel
        if not f.exists():
            continue
        src = f.read_text()
        # Find each call to run_isolated_agent_background(...) and check the
        # subsequent block contains "server_overrides=".
        for m in re.finditer(r"run_isolated_agent_background\s*\(", src):
            start = m.start()
            # Find the matching close paren (simple balance counter — these
            # calls don't contain string literals with stray parens).
            depth = 0
            i = m.end() - 1  # position of '('
            end = None
            for j in range(i, len(src)):
                ch = src[j]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if end is None:
                continue
            call_text = src[start:end]
            line_no = src[:start].count("\n") + 1
            if "server_overrides" not in call_text:
                misses.append(f"{rel}:{line_no}")

    assert not misses, (
        "run_isolated_agent_background callers that DO NOT forward "
        "server_overrides:\n  " + "\n  ".join(misses) + "\n\n"
        "Each missing call will silently drop per-role MCP arg overrides "
        "(filesystem allowed roots, git repo path, etc.) on the next spawn. "
        "Either add `server_overrides=<source>.get('server_overrides') or None` "
        "to the call, or add the file to EXEMPT_FILES with a justification."
    )

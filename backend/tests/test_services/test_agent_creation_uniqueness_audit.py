"""Static audit: every agent-CREATION entry point must pass through a
uniqueness gate, so a duplicate normalized agent name can never be created.

Two gates count:
  - ``ensure_unique_agent_name`` — validates an explicit/persistent name;
  - ``_generate_unique_agent_name`` — produces an inherently-unique name.

Resume / restart / auto-wake paths are intentionally OUT of scope: they take
a ``run_id`` and reuse an existing record's name, so there is no new name to
validate.

Belt-and-suspenders with the runtime checks (registry validator +
agent_definitions UNIQUE constraint). This catches a NEW creation entry added
without a gate at review time. If you add a creation entry, add it to the
relevant list below — the explicit list is the point: it forces a conscious
decision about uniqueness for every new spawn surface.
"""
from __future__ import annotations

import ast
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_SPAWN = _PROJECT_ROOT / "backend" / "fast-agent" / "src" / "fast_agent" / "spawn"
_SPAWNER_SERVER = _SPAWN / "servers" / "agent_spawner_server.py"
_TEAM_SPAWNER = _SPAWN / "team_spawner.py"

# (file, function name, required gate symbol)
_CREATION_ENTRIES = [
    (_SPAWNER_SERVER, "spawn_agent", "ensure_unique_agent_name"),
    (_SPAWNER_SERVER, "spawn_and_run_isolated", "ensure_unique_agent_name"),
    (_TEAM_SPAWNER, "spawn_team", "_generate_unique_agent_name"),
    (_TEAM_SPAWNER, "_spawn_single_agent", "_generate_unique_agent_name"),
    (_TEAM_SPAWNER, "spawn_team_members_for_session", "_generate_unique_agent_name"),
]


def _names_referenced_in(path: Path, func_name: str) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    raise AssertionError(f"function {func_name!r} not found in {path}")


def test_every_creation_entry_calls_a_uniqueness_gate():
    offenders: list[str] = []
    for path, func, gate in _CREATION_ENTRIES:
        refs = _names_referenced_in(path, func)
        if gate not in refs:
            offenders.append(
                f"{path.relative_to(_PROJECT_ROOT)}::{func} does not reference "
                f"{gate!r} — a duplicate agent name could be created here."
            )
    assert not offenders, (
        "Agent creation entry points missing a uniqueness gate:\n  - "
        + "\n  - ".join(offenders)
    )


def test_audit_list_is_not_empty():
    # Guard against a refactor silently emptying the audit (false-green).
    assert len(_CREATION_ENTRIES) >= 5
    for path, _, _ in _CREATION_ENTRIES:
        assert path.exists(), f"audited source missing: {path}"

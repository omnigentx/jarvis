"""Static audit: callers of ``run_isolated_agent_background`` must pass a
team-distinct ``agent_name`` whenever they also pass a non-empty
``team_name``.

This is belt-and-suspenders with the runtime validator in
``SpawnRegistry.register`` (see ``test_spawn_registry_validation.py``).
The runtime validator catches the bug at write time; this static test
catches new call sites at CI time so the bug is rejected in PR review
BEFORE it ships.

Scope: parses every ``run_isolated_agent_background(...)`` invocation in
backend code (NOT in tests), then asserts the per-call pattern. If a call
site adds ``team_name=`` it MUST also add ``agent_name=``, AND the
``agent_name=`` value must not be the literal string ``"role"`` (which
would mean it's just echoing the role variable as identity — the exact
mistake that produced the 2026-05-17 incident).

Refer to incident notes in ``isolated_spawner.py`` and the SpawnRegistry
validator for full context.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Source trees to scan. Don't include /tests or generated /__pycache__.
_SOURCE_TREES = [
    _PROJECT_ROOT / "backend" / "services",
    _PROJECT_ROOT / "backend" / "routes",
    _PROJECT_ROOT / "backend" / "fast-agent" / "src" / "fast_agent" / "spawn",
]

_TARGET = "run_isolated_agent_background"


def _iter_python_files(root: Path):
    for p in root.rglob("*.py"):
        # Skip generated copies of the source tree inside agent workspaces
        if ".runtime" in p.parts or "node_modules" in p.parts:
            continue
        yield p


def _is_target_call(node: ast.AST) -> bool:
    """True if this AST node is a Call to ``run_isolated_agent_background``.

    Matches both bare-name and attribute calls. We don't try to resolve
    imports — assume the function name is unique enough in this codebase
    (it is — grep confirms one definition).
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == _TARGET:
        return True
    if isinstance(func, ast.Attribute) and func.attr == _TARGET:
        return True
    return False


def _kw_value(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _is_definitely_non_empty(node: ast.AST | None) -> bool:
    """Best-effort: does this expression have a non-empty value at all
    paths? We only enforce the EASY cases (literal strings, simple ``Name``s)
    — anything fancy is treated as "trust the caller" with a permissive
    return. The runtime validator catches what we miss.
    """
    if node is None:
        return False
    # Literal: must be a non-empty string. Empty string or None → fail.
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and bool(node.value)
    # Subscript like cfg["agent_name"] / spawn_record["agent_name"] / dict.get(...).
    # We can't statically prove non-emptiness; allow with hope the runtime
    # validator catches surprises.
    return True


def _looks_like_bare_role_variable(node: ast.AST | None) -> bool:
    """The exact mistake at agent_spawner_server.py:445 was
    ``agent_name=role`` — using the role *variable* as the identity. For
    ad-hoc spawns this is OK (no team_name) but for team-managed spawns
    it's the silent-fail pattern we want to forbid.
    """
    return isinstance(node, ast.Name) and node.id == "role"


def test_every_team_spawn_caller_passes_distinct_agent_name():
    """For each ``run_isolated_agent_background(...)`` call: if it passes
    ``team_name=<truthy expression>`` it MUST also pass ``agent_name=`` with
    a value that is NOT the bare ``role`` variable.

    The runtime validator in ``SpawnRegistry.register`` already enforces
    this at WRITE time. This static test exists to catch new offenders
    *during code review*, before the runtime validator fires in
    production. Failing here means: a caller you added would crash on its
    first execution. Fix the call before merging.
    """
    offenders: list[str] = []

    for tree in _SOURCE_TREES:
        if not tree.exists():
            continue
        for src_file in _iter_python_files(tree):
            try:
                source = src_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if _TARGET not in source:
                continue
            try:
                module = ast.parse(source, filename=str(src_file))
            except SyntaxError:
                # If the file doesn't parse the broader test suite will
                # explode anyway; skip silently here.
                continue

            for node in ast.walk(module):
                if not _is_target_call(node):
                    continue
                team_kw = _kw_value(node, "team_name")
                # No team_name → ad-hoc spawn → out of scope.
                if team_kw is None:
                    continue
                # team_name explicitly empty string → also ad-hoc, skip.
                if isinstance(team_kw, ast.Constant) and team_kw.value in ("", None):
                    continue

                agent_kw = _kw_value(node, "agent_name")
                rel = src_file.relative_to(_PROJECT_ROOT)
                location = f"{rel}:{node.lineno}"

                if not _is_definitely_non_empty(agent_kw):
                    offenders.append(
                        f"{location}: team_name passed but agent_name is missing "
                        f"or definitely-empty — would crash SpawnRegistry validator."
                    )
                    continue
                if _looks_like_bare_role_variable(agent_kw):
                    offenders.append(
                        f"{location}: team_name passed AND agent_name=role — "
                        f"this is the exact silent-fail pattern from the "
                        f"2026-05-17 incident. Pass the team-managed identity "
                        f"(e.g. cfg['agent_name']) instead of the bare role."
                    )

    assert not offenders, (
        "run_isolated_agent_background callers violate team-distinct-name "
        "invariant:\n  - " + "\n  - ".join(offenders) + "\n\n"
        "Each offender will crash at registry.register() time. Fix the "
        "call site, or — if this is intentionally an ad-hoc spawn — drop "
        "the ``team_name=`` kwarg so the validator doesn't apply."
    )


def test_target_function_actually_referenced():
    """Smoke test: prove our scan isn't a no-op. If someone renames
    ``run_isolated_agent_background`` and forgets to update this test,
    the audit would silently pass on zero call sites — a false-green.
    """
    total_hits = 0
    for tree in _SOURCE_TREES:
        if not tree.exists():
            continue
        for src_file in _iter_python_files(tree):
            try:
                if _TARGET in src_file.read_text(encoding="utf-8"):
                    total_hits += 1
            except OSError:
                pass
    assert total_hits >= 3, (
        f"Static audit found only {total_hits} file(s) mentioning "
        f"{_TARGET!r}. Either the function was renamed (update _TARGET) "
        f"or the _SOURCE_TREES list is wrong."
    )

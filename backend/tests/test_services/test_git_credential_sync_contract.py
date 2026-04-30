"""Contract test: docker-compose + git_credential_sync must agree on paths.

Catches the class of bug where one side (compose mount target, env var, or
module constant) drifts from another and git silently can't find the
credential helper — which manifested in prod as
``fatal: could not read Username for 'https://github.com'`` even though the
token was correctly stored via the Settings UI.

The check is static: parse compose, import module, compare strings.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "docker-compose.yaml"
SERVER_PY = REPO_ROOT / "backend" / "server.py"


@pytest.fixture(scope="module")
def backend_service() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data["services"]["jarvis-backend"]


def _env_map(backend: dict) -> dict[str, str]:
    """Parse ``environment:`` list entries of form ``KEY=VALUE`` into a dict."""
    out: dict[str, str] = {}
    for entry in backend.get("environment", []):
        if "=" in entry:
            k, _, v = entry.partition("=")
            out[k] = v
    return out


def test_git_config_global_matches_module_gitconfig_path(backend_service):
    """``GIT_CONFIG_GLOBAL`` in container env must match the absolute path
    the module writes the rendered gitconfig to; otherwise git reads a
    stale/missing file and the whole credential helper chain breaks."""
    from services import git_credential_sync as mod

    env = _env_map(backend_service)
    git_config_global = env.get("GIT_CONFIG_GLOBAL")
    assert git_config_global, "GIT_CONFIG_GLOBAL not set in compose environment"

    # Module writes to _PERSIST_DIR/<filename>. Inside the container,
    # _PERSIST_DIR falls back to /app (WORKDIR), so the absolute path git
    # ends up reading is /app/<filename>.
    expected = f"/app/{mod._GITCONFIG_PATH.name}"
    assert git_config_global == expected, (
        f"GIT_CONFIG_GLOBAL={git_config_global!r} but module writes to "
        f"{expected!r} — git will not find the helper"
    )


def test_container_credentials_path_matches_module_credentials_path():
    """``_CONTAINER_CREDENTIALS_PATH`` (baked into gitconfig's
    ``credential.helper = store --file=...``) must point at the same file
    the module writes credentials to."""
    from services import git_credential_sync as mod

    expected = f"/app/{mod._GIT_CREDENTIALS_PATH.name}"
    assert mod._CONTAINER_CREDENTIALS_PATH == expected, (
        f"_CONTAINER_CREDENTIALS_PATH={mod._CONTAINER_CREDENTIALS_PATH!r} "
        f"but module writes credentials to {expected!r} — git's store "
        f"helper will look at the wrong file"
    )


_FORBIDDEN_TARGETS = (
    "/app/.git-credentials",
    "/app/.gitconfig",
    "/app/git-credentials",
    "/app/gitconfig",
)


def _volume_fingerprint(entry) -> str:
    """Flatten either compose syntax to a single searchable string.

    Compose accepts both short (``"source:target[:mode]"``) and long
    (``{type: bind, source: ..., target: ...}``) volume entries. The short
    string-only check used to miss the long form — a regression reintroducing
    the bind-mount via long syntax would have slipped through.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return f"{entry.get('source', '')}:{entry.get('target', '')}"
    return str(entry)


def test_no_stale_git_credential_bind_mounts(backend_service):
    """Bind-mounting the git files was removed because the DB is the source
    of truth and files are regenerated on every boot. A regression that
    re-adds the mount without also re-adding host file pre-init would
    silently break production again (Docker would create empty dirs at the
    mount targets)."""
    volumes = backend_service.get("volumes", [])
    suspicious = [
        v for v in volumes
        if any(t in _volume_fingerprint(v) for t in _FORBIDDEN_TARGETS)
    ]
    assert not suspicious, (
        f"git credential files must not be bind-mounted — found {suspicious}. "
        f"DB-reconcile-on-boot is the intended persistence mechanism."
    )


def test_git_terminal_prompt_disabled(backend_service):
    """Without ``GIT_TERMINAL_PROMPT=0``, an unauthenticated ``git clone`` in
    the agent subprocess would block forever on the username prompt instead
    of failing. The old ``:ro`` bind-mount also guarded against the
    ``credential.helper = store`` appending prompt input — that defence is
    gone, so this env var is the replacement."""
    env = _env_map(backend_service)
    assert env.get("GIT_TERMINAL_PROMPT") == "0", (
        "GIT_TERMINAL_PROMPT must be '0' in the backend container env — "
        "dev agents have no TTY and would otherwise hang on git prompts."
    )


def _find_git_sync_call(tree: ast.Module) -> ast.Call | None:
    """Locate the ``git_credential_sync.reconcile_from_db(...)`` call in
    the parsed server.py AST. Returns ``None`` if absent."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match: git_credential_sync.reconcile_from_db(...)
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "reconcile_from_db"
            and isinstance(func.value, ast.Name)
            and func.value.id == "git_credential_sync"
        ):
            return node
    return None


def _is_inside_broad_except(tree: ast.Module, target: ast.AST) -> bool:
    """Return True if ``target`` is lexically inside a ``try`` body whose
    ``except`` handler catches a broad type (``Exception`` or bare) without
    re-raising. This is the pattern that silently swallowed the reconcile
    error in the 2026-04-24 incident and must not reappear."""
    target_lineno = getattr(target, "lineno", None)
    if target_lineno is None:
        return False

    def _handler_is_broad(handler: ast.ExceptHandler) -> bool:
        # Bare "except:" — always broad.
        if handler.type is None:
            return True
        # "except Exception" or "except Exception as e" — broad.
        if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
            return True
        return False

    def _handler_reraises(handler: ast.ExceptHandler) -> bool:
        for node in ast.walk(handler):
            if isinstance(node, ast.Raise):
                return True
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        # Does the target fall inside this try's body?
        body_start = node.body[0].lineno if node.body else node.lineno
        body_end = node.end_lineno or body_start
        if not (body_start <= target_lineno <= body_end):
            continue
        # Is any handler broad AND non-reraising?
        for h in node.handlers:
            if _handler_is_broad(h) and not _handler_reraises(h):
                return True
    return False


def test_server_calls_git_sync_outside_swallowing_try():
    """Silent-fallback guard: ``git_credential_sync.reconcile_from_db`` must
    not be buried inside a ``try: ... except Exception: logger.warning(...)``
    block. The 2026-04-24 incident was caused by a different missing-piece,
    but wrapping this call in such a block would recreate the exact same
    symptom (dev-agent ``git clone`` silently failing at runtime) if any
    filesystem/DB error occurs at boot. Fail loud → container crash-loops
    → operator sees the real error."""
    tree = ast.parse(SERVER_PY.read_text(encoding="utf-8"))
    call = _find_git_sync_call(tree)
    assert call is not None, (
        "git_credential_sync.reconcile_from_db() call not found in server.py"
    )
    assert not _is_inside_broad_except(tree, call), (
        f"server.py:{call.lineno} — git_credential_sync.reconcile_from_db "
        f"is inside a try/except that swallows Exception. Any OSError / "
        f"sqlite3.DatabaseError at boot would be silently downgraded to a "
        f"WARNING log, and dev agents would hit the 2026-04-24 symptom "
        f"at runtime with no clear cause. Move the call outside the "
        f"swallowing block (or tighten the except to a specific type)."
    )

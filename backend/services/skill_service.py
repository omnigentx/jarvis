"""Skill management service — CRUD over .fast-agent/skills/<name>/SKILL.md.

The skills directory is the single source of truth. Each skill lives in its
own subdirectory with a ``SKILL.md`` file containing a YAML frontmatter block
followed by markdown body.

This service handles validation, atomic writes, optimistic locking via mtime,
built-in protection (via ``_builtin.yaml`` manifest), and used-by computation
across both code-based agents (parsed statically from ``agent.py``) and
card-based agents (YAML frontmatter in ``.fast-agent/agent_cards/*.md``).
"""
from __future__ import annotations

import ast
import errno
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("skill_service")

# ----- Paths --------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = _BACKEND_DIR / ".fast-agent" / "skills"
AGENT_CARDS_DIR = _BACKEND_DIR / ".fast-agent" / "agent_cards"
AGENT_CODE_FILE = _BACKEND_DIR / "agent.py"
BUILTIN_MANIFEST = SKILLS_DIR / "_builtin.yaml"

# ----- Limits / regex -----------------------------------------------------

MAX_BYTES = 256 * 1024
SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
RESERVED_NAMES = {"con", "prn", "aux", "nul", "_builtin"}

# Frontmatter block at the very start of a markdown file:
#   ---\n
#   ...yaml...
#   ---\n
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


# ----- Data ---------------------------------------------------------------


@dataclass
class SkillSummary:
    name: str
    description: Optional[str]
    is_builtin: bool
    used_by: list[str]
    mtime_ns: int
    parse_error: Optional[str] = None


@dataclass
class SkillDetail(SkillSummary):
    content: str = ""


# ----- Validation errors --------------------------------------------------


class SkillValidationError(Exception):
    """Raised for any validation failure. Carries an HTTP-friendly status."""

    def __init__(self, status_code: int, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.detail = detail or {}


# ----- Name validation ----------------------------------------------------


def validate_skill_name(name: str) -> None:
    """Raise SkillValidationError if name fails any check.

    Covers: empty, length, regex, reserved, traversal characters.
    """
    if not isinstance(name, str) or not name:
        raise SkillValidationError(400, "Skill name is required.")
    if len(name) > 64:
        raise SkillValidationError(400, "Skill name must be ≤64 characters.")
    if not SKILL_NAME_RE.fullmatch(name):
        raise SkillValidationError(
            400,
            "Skill name must be lowercase alphanumeric with hyphens "
            "(e.g. 'my-skill'); cannot start/end with a hyphen.",
        )
    if name.lower() in RESERVED_NAMES:
        raise SkillValidationError(400, f"Skill name '{name}' is reserved.")


def _resolve_skill_dir(name: str) -> Path:
    """Validate name and return the skill directory path under SKILLS_DIR.

    Defence in depth: even though regex blocks bad chars, we still check that
    the resolved path stays under SKILLS_DIR.
    """
    validate_skill_name(name)
    candidate = (SKILLS_DIR / name).resolve()
    skills_root = SKILLS_DIR.resolve()
    if skills_root not in candidate.parents:
        raise SkillValidationError(400, "Path traversal detected.")
    return candidate


def _skill_md_path(name: str) -> Path:
    return _resolve_skill_dir(name) / "SKILL.md"


# ----- Built-in detection -------------------------------------------------

_builtin_cache: dict | None = None
_builtin_mtime_ns: int = 0


def _load_builtin_set() -> set[str]:
    """Read manifest, cached by file mtime. Returns a set of skill names.

    On parse failure, log warning and return an empty set (treat all as user
    skills — fail-open for editability, which matches dev workflow). If the
    file is missing entirely, also return empty.
    """
    global _builtin_cache, _builtin_mtime_ns
    try:
        st = BUILTIN_MANIFEST.stat()
    except FileNotFoundError:
        _builtin_cache = set()
        _builtin_mtime_ns = 0
        return _builtin_cache
    if _builtin_cache is not None and st.st_mtime_ns == _builtin_mtime_ns:
        return _builtin_cache  # type: ignore[return-value]
    try:
        data = yaml.safe_load(BUILTIN_MANIFEST.read_text(encoding="utf-8")) or {}
        names = data.get("builtin") or []
        if not isinstance(names, list):
            raise ValueError("'builtin' key must be a list")
        _builtin_cache = {str(n) for n in names if isinstance(n, str)}
    except Exception as exc:
        logger.warning("[skills] _builtin.yaml parse failed (%s) — treating all as user", exc)
        _builtin_cache = set()
    _builtin_mtime_ns = st.st_mtime_ns
    return _builtin_cache


def is_builtin(name: str) -> bool:
    return name in _load_builtin_set()


# ----- Frontmatter --------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Raise SkillValidationError on
    missing or invalid YAML.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        raise SkillValidationError(
            400,
            "SKILL.md must start with a YAML frontmatter block (--- ... ---).",
        )
    raw = m.group(1)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillValidationError(
            400,
            "Invalid YAML frontmatter.",
            detail={"error": str(exc)},
        ) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SkillValidationError(400, "Frontmatter must be a YAML mapping.")
    body = content[m.end():]
    return data, body


def _validate_frontmatter_for_save(name: str, content: str) -> None:
    """Ensure required fields are present and 'name' matches directory.

    Raises SkillValidationError on any failure.
    """
    fm, _body = parse_frontmatter(content)
    fm_name = fm.get("name")
    if not isinstance(fm_name, str) or not fm_name.strip():
        raise SkillValidationError(400, "Frontmatter 'name' is required.")
    if fm_name.strip() != name:
        raise SkillValidationError(
            400,
            f"Frontmatter 'name' ({fm_name!r}) must match skill directory "
            f"({name!r}). Renaming via editor is not supported.",
        )
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise SkillValidationError(400, "Frontmatter 'description' is required.")


# ----- Used-by ------------------------------------------------------------


def _used_by_from_agent_cards(skill_name: str) -> list[str]:
    """Return agent card names whose YAML frontmatter references this skill."""
    if not AGENT_CARDS_DIR.exists():
        return []
    out: list[str] = []
    for card in sorted(AGENT_CARDS_DIR.glob("*.md")):
        try:
            text = card.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except Exception:
            continue
        skills = fm.get("skills") or []
        if not isinstance(skills, list):
            continue
        for entry in skills:
            if not isinstance(entry, str):
                continue
            # Entries look like ".fast-agent/skills/<name>"
            tail = entry.rstrip("/").split("/")[-1]
            if tail == skill_name:
                agent_name = fm.get("name") or card.stem
                out.append(str(agent_name))
                break
    return out


def _parse_code_skill_refs() -> dict[str, set[str]]:
    """Static parse of agent.py via the ``ast`` module.

    Returns a mapping of ``skill_name -> {agent_name, ...}`` for every agent
    declared via ``@fast.agent(...)`` or ``@fast.custom(...)`` whose ``skills=``
    keyword contains ``get_skills("foo", "bar", ...)`` calls. Imperfect for
    dynamic constructions, but covers every static usage in the project's
    agent.py — and is robust to formatting changes that broke the previous
    regex-based approach.
    """
    if not AGENT_CODE_FILE.exists():
        return {}
    try:
        text = AGENT_CODE_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        logger.warning("[skills] could not parse %s for skill refs", AGENT_CODE_FILE)
        return {}

    skill_to_agents: dict[str, set[str]] = {}

    def _agent_name_from_decorator(call: ast.Call) -> Optional[str]:
        for kw in call.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        return None

    def _skill_strings_in(node: ast.AST) -> list[str]:
        out: list[str] = []
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "get_skills"
            ):
                for arg in sub.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        out.append(arg.value)
        return out

    def _is_agent_decorator(call: ast.Call) -> bool:
        # Matches `fast.agent(...)` or `fast.custom(...)` (any object whose attr
        # is `agent` or `custom`).
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in ("agent", "custom")
        )

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and _is_agent_decorator(deco)):
                continue
            agent_name = _agent_name_from_decorator(deco)
            if agent_name is None:
                continue
            for kw in deco.keywords:
                if kw.arg != "skills":
                    continue
                for skill in _skill_strings_in(kw.value):
                    skill_to_agents.setdefault(skill, set()).add(agent_name)
    return skill_to_agents


def _used_by(skill_name: str, code_index: dict[str, set[str]] | None = None) -> list[str]:
    """Return the list of agents currently using this skill.

    **Runtime is authoritative when available.** A code-based agent that gets
    a skill attached at runtime (no source-file edit) is invisible to static
    analysis — yet the user just attached it and expects to see it. So if
    the FastAgent app is up and reports any agents, we read directly from
    `config.skill_manifests`.

    Static analysis (parsing agent.py + agent_cards/*.md) is the fallback
    for when the runtime isn't available — unit tests without the live app,
    or API hits during early boot before agents register.
    """
    fast_obj, _state, _rebuild = _runtime_handles()
    runtime_pairs = list(_iter_agents_with_names(fast_obj)) if fast_obj is not None else []
    if runtime_pairs:
        out: list[str] = []
        for name, cfg in runtime_pairs:
            if any(getattr(m, "name", None) == skill_name for m in cfg.skill_manifests):
                out.append(name)
        return sorted(out)
    # Fallback: static analysis (boot-time prediction).
    code_index = code_index if code_index is not None else _parse_code_skill_refs()
    code_agents = code_index.get(skill_name, set())
    card_agents = set(_used_by_from_agent_cards(skill_name))
    return sorted(code_agents | card_agents)


# ----- Runtime invalidation ----------------------------------------------
#
# FastAgent loads skill_manifests once at module import; agent endpoints read
# from those in-memory copies rather than from disk. After a save/delete on
# disk the runtime is stale, and `/api/agents/{name}` keeps returning the
# pre-edit content even across page reloads. We patch the in-memory manifest
# in-place so the next read returns fresh data.
#
# Indirect via these module-level hooks so tests can substitute stubs without
# pulling the entire agent.py at import time.


def _default_runtime_loader(name: str):
    """Return (fast, fresh_manifest) for the given skill name.

    Lazy-imports `agent` so this module stays importable in unit tests that
    don't need the FastAgent runtime spun up.
    """
    try:
        from agent import fast, get_skills  # type: ignore
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        logger.debug("[skills] runtime not available (%s) — skipping reload", exc)
        return None, None
    try:
        fresh = get_skills(name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[skills] could not reload manifest for %s: %s", name, exc)
        return fast, None
    return fast, (fresh[0] if fresh else None)


_runtime_loader = _default_runtime_loader


def _iter_agents_with_names(fast_obj):
    """Yield (agent_name, config) pairs from FastAgent's agent registry.

    Handles both dict-shaped (`{name: {"config": ...}}`) and attribute-shaped
    container variants so this stays compatible with whatever fast-agent
    chooses to expose.
    """
    agents = getattr(fast_obj, "agents", None)
    if agents is None:
        return
    items = agents.items() if hasattr(agents, "items") else []
    for name, entry in items:
        if isinstance(entry, dict):
            cfg = entry.get("config")
        else:
            cfg = getattr(entry, "config", None)
        if cfg is not None and hasattr(cfg, "skill_manifests"):
            yield name, cfg


def _iter_agent_configs(fast_obj):
    """Backward-compat shim: yield only configs (drops names)."""
    for _name, cfg in _iter_agents_with_names(fast_obj):
        yield cfg


def _default_runtime_handles():
    """Return (fast, state, rebuild_fn). Each component is None if unreachable.

    Tests monkeypatch this whole function to inject a stub triplet.
    """
    fast_obj = None
    try:
        from agent import fast as _fast  # type: ignore
        fast_obj = _fast
    except Exception:
        pass
    state = None
    try:
        from services import shared_state as _state
        state = _state
    except Exception:
        pass
    rebuild_fn = None
    try:
        from fast_agent.core.instruction_refresh import rebuild_agent_instruction
        rebuild_fn = rebuild_agent_instruction
    except Exception:
        pass
    return fast_obj, state, rebuild_fn


_runtime_handles = _default_runtime_handles


async def _apply_manifests_to_agent(
    agent_name: str,
    new_manifests: list,
    *,
    fast_obj=None,
) -> bool:
    """Push a new skill_manifests list into one agent's runtime state.

    Full path: ``rebuild_agent_instruction`` updates BOTH the config and the
    agent's cached `instruction` string so the next LLM turn uses the new
    `{{agentSkills}}` content. If the live agent instance isn't reachable
    (unit tests, app not started yet, etc.), fall back to mutating
    ``config.skill_manifests`` so the dashboard at least reads fresh metadata.

    `fast_obj` may be passed by callers that already resolved it (avoids a
    redundant import); otherwise it's looked up via `_runtime_handles`.
    Returns True if the full rebuild path was taken.
    """
    handles_fast, state, rebuild_fn = _runtime_handles()
    if fast_obj is None:
        fast_obj = handles_fast

    # Full path: rebuild the agent's instruction so the LLM sees fresh skills.
    # If rebuild_fn IS available but RAISES, propagate — callers (attach,
    # detach) need that signal to roll back the YAML write. The fallback
    # below is only for environments where rebuild_fn is genuinely absent
    # (unit tests without the full runtime, app boot before agents started).
    if rebuild_fn is not None and state is not None and getattr(state, "agent_app", None) is not None:
        agent_instance = None
        try:
            agent_instance = state.agent_app.get_agent(agent_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[skills] could not resolve runtime agent %s: %s", agent_name, exc)
        if agent_instance is not None:
            await rebuild_fn(agent_instance, skill_manifests=new_manifests)
            # rebuild_agent_instruction updates the AGENT INSTANCE's
            # skill_manifests, but the AgentConfig living on `fast.agents`
            # is a separate object — and various other read paths
            # (`/api/agents/{name}`, `_used_by`, etc.) iterate configs.
            # Keep both in sync so the dashboard's view never lags the LLM's.
            if fast_obj is not None:
                for name, cfg in _iter_agents_with_names(fast_obj):
                    if name == agent_name:
                        cfg.skill_manifests = list(new_manifests)
                        break
            logger.info(
                "[skills] rebuilt instruction for agent=%s (%d skills)",
                agent_name, len(new_manifests),
            )
            return True

    # Fallback: mutate config so /api/agents reads fresh data even if the
    # full LLM-side rebuild isn't available.
    if fast_obj is not None:
        for name, cfg in _iter_agents_with_names(fast_obj):
            if name == agent_name:
                cfg.skill_manifests = list(new_manifests)
                return False
    return False


async def _runtime_refresh_skill(skill_name: str) -> int:
    """After an edit on disk: refresh every agent that uses this skill so the
    next LLM turn reads the new body. Returns count of agents touched.

    Best-effort: a per-agent rebuild failure is logged but doesn't fail the
    overall save — the disk write already succeeded and reverting it would be
    more confusing than telling the user "saved, but live agent may need a
    restart". (Attach/detach use the strict path via ``_apply_manifests_to_agent``
    directly so they can roll the YAML back.)
    """
    fast_obj, fresh_manifest = _runtime_loader(skill_name)
    if fast_obj is None or fresh_manifest is None:
        return 0
    refreshed = 0
    for agent_name, cfg in _iter_agents_with_names(fast_obj):
        manifests = list(cfg.skill_manifests)
        if not any(getattr(m, "name", None) == skill_name for m in manifests):
            continue
        new_list = [
            fresh_manifest if getattr(m, "name", None) == skill_name else m
            for m in manifests
        ]
        try:
            await _apply_manifests_to_agent(agent_name, new_list, fast_obj=fast_obj)
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[skills] runtime refresh failed for agent=%s skill=%s: %s",
                agent_name, skill_name, exc,
            )
    if refreshed:
        logger.info("[skills] runtime refresh: %s → %d agent(s)", skill_name, refreshed)
    return refreshed


async def _runtime_remove_skill_from_all(skill_name: str) -> int:
    """After a delete on disk: strip the skill from every agent's runtime.

    Best-effort, same rationale as ``_runtime_refresh_skill``.
    """
    fast_obj, _ = _runtime_loader(skill_name)
    if fast_obj is None:
        return 0
    removed = 0
    for agent_name, cfg in _iter_agents_with_names(fast_obj):
        manifests = list(cfg.skill_manifests)
        new_list = [m for m in manifests if getattr(m, "name", None) != skill_name]
        if len(new_list) != len(manifests):
            try:
                await _apply_manifests_to_agent(agent_name, new_list, fast_obj=fast_obj)
                removed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[skills] runtime remove failed for agent=%s skill=%s: %s",
                    agent_name, skill_name, exc,
                )
    if removed:
        logger.info("[skills] runtime remove: %s → %d agent(s)", skill_name, removed)
    return removed


# ----- Disk I/O -----------------------------------------------------------


def _atomic_write_skill_md(path: Path, content: str) -> None:
    """Backup → temp write → atomic rename. Falls back in-place on EBUSY.

    Mirrors the pattern used by routes/yaml_config.py so behavior stays
    consistent.
    """
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            logger.warning("[skills] backup failed for %s: %s", path, exc)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            path.write_text(content, encoding="utf-8")
            try:
                tmp.unlink()
            except OSError:
                pass
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


# ----- Public API ---------------------------------------------------------


def list_skills() -> list[SkillSummary]:
    """Enumerate skill directories under SKILLS_DIR.

    - Skips dot/underscore prefixed entries (e.g. ``_builtin.yaml``).
    - Skips entries whose name fails ``SKILL_NAME_RE`` (legacy folders).
    - Skips symlinks pointing outside SKILLS_DIR.
    - Skills missing ``SKILL.md`` are skipped with a warning.
    - Frontmatter parse failures yield an entry with parse_error set.
    """
    if not SKILLS_DIR.exists():
        return []
    code_index = _parse_code_skill_refs()
    out: list[SkillSummary] = []
    skills_root = SKILLS_DIR.resolve()
    for entry in sorted(SKILLS_DIR.iterdir()):
        name = entry.name
        if name.startswith(".") or name.startswith("_"):
            continue
        if not entry.is_dir():
            continue
        if not SKILL_NAME_RE.fullmatch(name):
            logger.warning("[skills] skipping invalid skill name: %s", name)
            continue
        try:
            real = entry.resolve()
            if skills_root not in real.parents and real != skills_root:
                logger.warning("[skills] skipping symlink outside dir: %s", name)
                continue
        except OSError:
            continue
        md_path = entry / "SKILL.md"
        if not md_path.exists():
            logger.warning("[skills] %s has no SKILL.md — skipping", name)
            continue
        try:
            content = md_path.read_text(encoding="utf-8")
            mtime_ns = md_path.stat().st_mtime_ns
        except OSError:
            continue
        description: Optional[str] = None
        parse_error: Optional[str] = None
        try:
            fm, _ = parse_frontmatter(content)
            d = fm.get("description")
            if isinstance(d, str):
                description = d.strip() or None
        except SkillValidationError as exc:
            parse_error = exc.message
        out.append(SkillSummary(
            name=name,
            description=description,
            is_builtin=is_builtin(name),
            used_by=_used_by(name, code_index),
            mtime_ns=mtime_ns,
            parse_error=parse_error,
        ))
    return out


def get_skill(name: str) -> SkillDetail:
    md = _skill_md_path(name)
    if not md.exists():
        raise SkillValidationError(404, f"Skill '{name}' not found.")
    try:
        content = md.read_text(encoding="utf-8")
        mtime_ns = md.stat().st_mtime_ns
    except OSError as exc:
        raise SkillValidationError(500, f"Read failed: {exc}") from exc
    description: Optional[str] = None
    parse_error: Optional[str] = None
    try:
        fm, _ = parse_frontmatter(content)
        d = fm.get("description")
        if isinstance(d, str):
            description = d.strip() or None
    except SkillValidationError as exc:
        parse_error = exc.message
    return SkillDetail(
        name=name,
        description=description,
        is_builtin=is_builtin(name),
        used_by=_used_by(name),
        mtime_ns=mtime_ns,
        parse_error=parse_error,
        content=content,
    )


def create_skill(name: str, content: str) -> SkillDetail:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise SkillValidationError(413, f"Content exceeds {MAX_BYTES} bytes.")
    skill_dir = _resolve_skill_dir(name)

    # Case-insensitive collision check (macOS/Windows default FS).
    if SKILLS_DIR.exists():
        existing_lower = {p.name.lower() for p in SKILLS_DIR.iterdir() if p.is_dir()}
        if name.lower() in existing_lower:
            raise SkillValidationError(409, f"Skill '{name}' already exists.")

    _validate_frontmatter_for_save(name, content)

    try:
        skill_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SkillValidationError(409, f"Skill '{name}' already exists.")
    except OSError as exc:
        raise SkillValidationError(500, f"Could not create directory: {exc}") from exc

    md = skill_dir / "SKILL.md"
    try:
        _atomic_write_skill_md(md, content)
    except OSError as exc:
        # Roll back the empty directory we just created.
        try:
            shutil.rmtree(skill_dir)
        except OSError:
            pass
        raise SkillValidationError(500, f"Write failed: {exc}") from exc

    logger.info("[skills] created %s (%d bytes)", name, len(content))
    return get_skill(name)


async def update_skill(name: str, content: str, expected_mtime_ns: Optional[int]) -> SkillDetail:
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise SkillValidationError(413, f"Content exceeds {MAX_BYTES} bytes.")
    md = _skill_md_path(name)
    if not md.exists():
        raise SkillValidationError(404, f"Skill '{name}' not found.")

    # Optimistic lock: reject if disk has moved on since the client's GET.
    if expected_mtime_ns is not None:
        try:
            current_mtime_ns = md.stat().st_mtime_ns
        except OSError as exc:
            raise SkillValidationError(500, f"Stat failed: {exc}") from exc
        if current_mtime_ns != expected_mtime_ns:
            raise SkillValidationError(
                409,
                "Skill has been modified elsewhere. Reload to see the latest version.",
                detail={"current_mtime_ns": current_mtime_ns},
            )

    _validate_frontmatter_for_save(name, content)

    try:
        _atomic_write_skill_md(md, content)
    except OSError as exc:
        raise SkillValidationError(500, f"Write failed: {exc}") from exc

    await _runtime_refresh_skill(name)
    logger.info("[skills] updated %s (%d bytes)", name, len(content))
    return get_skill(name)


async def delete_skill(name: str) -> dict:
    """Delete a user-created skill directory and clean up agent card references.

    - Built-in skills cannot be deleted (403).
    - Returns the list of agent card filenames that had this skill removed.
    - Removes the entire skill directory (including extra files inside).
    """
    skill_dir = _resolve_skill_dir(name)
    if not skill_dir.exists():
        raise SkillValidationError(404, f"Skill '{name}' not found.")
    if is_builtin(name):
        raise SkillValidationError(
            403,
            f"'{name}' is a built-in skill and cannot be deleted. "
            "You can edit its content but the skill itself is required by the app.",
        )

    # Backup all affected agent cards first so we can roll back on failure.
    affected_cards = _find_agent_cards_referencing(name)
    card_backups: dict[Path, bytes] = {}
    try:
        for card_path in affected_cards:
            card_backups[card_path] = card_path.read_bytes()
    except OSError as exc:
        raise SkillValidationError(500, f"Could not back up agent cards: {exc}") from exc

    # Remove the skill directory.
    try:
        shutil.rmtree(skill_dir)
    except OSError as exc:
        raise SkillValidationError(500, f"Delete failed: {exc}") from exc

    # Now strip the reference from each agent card. If any single card edit
    # fails, restore everything (skill dir from .bak is harder; we leave a
    # warning and leave the partial state — operator can rerun).
    removed_from: list[str] = []
    failure: Optional[str] = None
    for card_path in affected_cards:
        try:
            _remove_skill_from_card(card_path, name)
            removed_from.append(card_path.stem)
        except Exception as exc:  # noqa: BLE001
            failure = f"{card_path.name}: {exc}"
            # Restore this card's pre-edit content. Other cards already
            # successfully edited stay edited — partial cleanup is documented
            # in the logs and surfaced to the caller.
            try:
                card_path.write_bytes(card_backups[card_path])
            except OSError:
                pass
            break

    if failure is not None:
        logger.error("[skills] partial cleanup after deleting %s: %s", name, failure)

    await _runtime_remove_skill_from_all(name)
    logger.info("[skills] deleted %s; cleaned %d agent card reference(s)", name, len(removed_from))
    return {
        "deleted": True,
        "name": name,
        "removed_from_agents": removed_from,
        "warning": failure,
    }


# ----- Agent card cleanup helpers -----------------------------------------


def _find_agent_cards_referencing(skill_name: str) -> list[Path]:
    if not AGENT_CARDS_DIR.exists():
        return []
    out: list[Path] = []
    for card in sorted(AGENT_CARDS_DIR.glob("*.md")):
        try:
            text = card.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(text)
        except Exception:
            continue
        skills = fm.get("skills") or []
        if not isinstance(skills, list):
            continue
        for entry in skills:
            if isinstance(entry, str) and entry.rstrip("/").split("/")[-1] == skill_name:
                out.append(card)
                break
    return out


def _remove_skill_from_card(card_path: Path, skill_name: str) -> None:
    """Remove the skill entry from the card's YAML frontmatter, atomically."""
    text = card_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    skills = fm.get("skills") or []
    if not isinstance(skills, list):
        return
    new_skills = [
        s for s in skills
        if not (isinstance(s, str) and s.rstrip("/").split("/")[-1] == skill_name)
    ]
    if len(new_skills) == len(skills):
        return  # Nothing to do.
    fm["skills"] = new_skills
    new_fm = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    new_text = f"---\n{new_fm}\n---\n{body}"

    # Backup + atomic write, same pattern as skills.
    backup = card_path.with_suffix(card_path.suffix + ".bak")
    try:
        backup.write_bytes(card_path.read_bytes())
    except OSError as exc:
        logger.warning("[skills] card backup failed for %s: %s", card_path, exc)
    tmp = card_path.with_suffix(card_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        os.replace(tmp, card_path)
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise
        card_path.write_text(new_text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def _add_skill_to_card(card_path: Path, skill_name: str) -> None:
    """Append the skill entry to the card's YAML frontmatter `skills:` list,
    atomically. No-op if already present.
    """
    text = card_path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    skills = fm.get("skills") or []
    if not isinstance(skills, list):
        skills = []
    entry = f".fast-agent/skills/{skill_name}"
    if any(
        isinstance(s, str) and s.rstrip("/").split("/")[-1] == skill_name
        for s in skills
    ):
        return  # already attached
    skills = list(skills) + [entry]
    fm["skills"] = skills
    new_fm = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip("\n")
    new_text = f"---\n{new_fm}\n---\n{body}"

    backup = card_path.with_suffix(card_path.suffix + ".bak")
    try:
        backup.write_bytes(card_path.read_bytes())
    except OSError as exc:
        logger.warning("[skills] card backup failed for %s: %s", card_path, exc)
    tmp = card_path.with_suffix(card_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        os.replace(tmp, card_path)
    except OSError as exc:
        if exc.errno != errno.EBUSY:
            raise
        card_path.write_text(new_text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


# ----- Attach / detach ----------------------------------------------------


def _resolve_runtime_agent(agent_name: str):
    """Return (cfg, fast_obj) for an existing runtime agent, or raise 404."""
    fast_obj, _state, _rebuild = _runtime_handles()
    if fast_obj is None:
        raise SkillValidationError(503, "Agent runtime not available.")
    for name, cfg in _iter_agents_with_names(fast_obj):
        if name == agent_name:
            return cfg, fast_obj
    raise SkillValidationError(404, f"Agent '{agent_name}' not found.")


async def attach_skill_to_agent(agent_name: str, skill_name: str) -> dict:
    """Attach an existing skill to an agent.

    Card-based agent → write the agent's `.md` YAML so the change survives
    restart. Code-based agent (no card file) → runtime-only; the response's
    `persisted=False` tells the UI to warn the user that the change reverts
    on next backend boot unless `agent.py` is edited.

    The runtime side calls fast-agent's `rebuild_agent_instruction` so the
    agent's cached system prompt picks up the new skill body before the next
    LLM turn — no restart required.

    Atomicity: the YAML write happens FIRST; if the runtime rebuild fails,
    the YAML is rolled back so disk and runtime stay consistent.
    """
    validate_skill_name(skill_name)
    skill_dir = _resolve_skill_dir(skill_name)
    if not skill_dir.exists():
        raise SkillValidationError(404, f"Skill '{skill_name}' not found.")

    # Look up the live agent config from the runtime.
    cfg, fast_obj = _resolve_runtime_agent(agent_name)
    current = list(cfg.skill_manifests)
    if any(getattr(m, "name", None) == skill_name for m in current):
        raise SkillValidationError(
            409, f"'{skill_name}' is already attached to '{agent_name}'."
        )

    # Load a fresh manifest object from disk to add to the list.
    _fast2, fresh_manifest = _runtime_loader(skill_name)
    if fresh_manifest is None:
        raise SkillValidationError(500, f"Could not load skill '{skill_name}' from disk.")

    new_list = current + [fresh_manifest]

    # Persist to YAML iff the agent is card-based.
    card_path = AGENT_CARDS_DIR / f"{agent_name}.md"
    is_card_based = card_path.exists()
    yaml_backup: Optional[bytes] = None
    if is_card_based:
        try:
            yaml_backup = card_path.read_bytes()
            _add_skill_to_card(card_path, skill_name)
        except Exception as exc:  # noqa: BLE001
            raise SkillValidationError(500, f"Could not update agent card: {exc}") from exc

    # Push to runtime; rollback YAML on failure to keep disk and memory aligned.
    try:
        await _apply_manifests_to_agent(agent_name, new_list, fast_obj=fast_obj)
    except Exception as exc:  # noqa: BLE001
        if yaml_backup is not None:
            try:
                card_path.write_bytes(yaml_backup)
            except OSError:
                logger.error("[skills] YAML rollback failed for %s after runtime error", card_path)
        raise SkillValidationError(500, f"Runtime rebuild failed: {exc}") from exc

    logger.info(
        "[skills] attached %s -> %s (persisted=%s)", skill_name, agent_name, is_card_based
    )
    # Slim payload — token-budget conscious. The full manifest list (with
    # bodies) used to be returned here, but no caller actually reads it: the
    # dashboard re-fetches /api/agents/{name} after attach, and the LLM
    # only needs to confirm the action + know the persistence implication.
    # If a caller really wants the post-attach list, it can call skill.list.
    return {
        "agent": agent_name,
        "skill": skill_name,
        "persisted": is_card_based,
        "skill_count": len(new_list),
    }


async def detach_skill_from_agent(agent_name: str, skill_name: str) -> dict:
    """Detach a skill from an agent. Symmetric to ``attach_skill_to_agent``.

    Card-based agent → strip the entry from the YAML so the change survives
    restart. Code-based agent → runtime-only; ``persisted=False`` in response.
    """
    validate_skill_name(skill_name)

    cfg, fast_obj = _resolve_runtime_agent(agent_name)
    current = list(cfg.skill_manifests)
    if not any(getattr(m, "name", None) == skill_name for m in current):
        raise SkillValidationError(
            409, f"'{skill_name}' is not attached to '{agent_name}'."
        )

    new_list = [m for m in current if getattr(m, "name", None) != skill_name]

    card_path = AGENT_CARDS_DIR / f"{agent_name}.md"
    is_card_based = card_path.exists()
    yaml_backup: Optional[bytes] = None
    if is_card_based:
        try:
            yaml_backup = card_path.read_bytes()
            _remove_skill_from_card(card_path, skill_name)
        except Exception as exc:  # noqa: BLE001
            raise SkillValidationError(500, f"Could not update agent card: {exc}") from exc

    try:
        await _apply_manifests_to_agent(agent_name, new_list, fast_obj=fast_obj)
    except Exception as exc:  # noqa: BLE001
        if yaml_backup is not None:
            try:
                card_path.write_bytes(yaml_backup)
            except OSError:
                logger.error("[skills] YAML rollback failed for %s after runtime error", card_path)
        raise SkillValidationError(500, f"Runtime rebuild failed: {exc}") from exc

    logger.info(
        "[skills] detached %s from %s (persisted=%s)", skill_name, agent_name, is_card_based
    )
    return {
        "agent": agent_name,
        "skill": skill_name,
        "persisted": is_card_based,
        "skill_count": len(new_list),
    }


def is_agent_card_based(agent_name: str) -> bool:
    """True if the agent has a `.md` card on disk (so attach/detach edits persist)."""
    return (AGENT_CARDS_DIR / f"{agent_name}.md").exists()


# ----- Templates ----------------------------------------------------------


SKILL_TEMPLATE = """\
---
name: {name}
description: One-line description of what this skill does and when to use it.
---

# {name}

## When to use
- Trigger condition 1
- Trigger condition 2

## How it works
Describe the steps the agent should take.
"""


def render_template(name: str = "my-skill") -> str:
    return SKILL_TEMPLATE.format(name=name)

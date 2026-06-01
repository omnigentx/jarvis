"""Factory-yaml team template service — read/write ``backend/team_templates/*.yaml``.

Companion to :mod:`services.team_template_service`, which edits the running
team's DB SSoT. This module edits the **factory default** the spawner reads
at team creation. Per user decision 2026-05-17 the two paths are kept
deliberately separate: editing a factory yaml does NOT mutate running teams.
Drift is surfaced via ``/api/team-sessions/{id}/template/yaml-diff`` and the
user clicks Reload-from-yaml manually.

Safety properties (mirrors :mod:`routes.yaml_config`):

* Only files directly inside the ``team_templates`` directory may be touched;
  path-traversal (``..``) is refused.
* Writes are validated for BOTH syntax (``yaml.safe_load``) and the minimal
  template structure (top-level dict with a ``roles`` mapping, either at
  the root or under ``team:``). The structural check is deliberately strict
  because the MCP tool lets an unattended LLM write these — a syntactically
  valid but structurally wrong yaml would brick every subsequent
  ``spawn_team`` call.
* Atomic write via ``tmp → os.replace`` with ``EBUSY`` fallback for bind-mounted
  files (Docker compose dev loop).
* Previous content rotates into a ``.bak`` sibling so a bad edit is recoverable.

Concurrency: writes are single-writer (no file lock). The MCP tool plus the
UI can in principle race on the same file, but the only realistic concurrent
caller pair is "human in the UI" + "human-driven LLM", which is implicitly
serialised by the human. If true concurrent automated writers ever land,
gate this module behind a per-path lock.
"""
from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Resolved once at import — same hardening as yaml_config: even if cwd flips
# mid-request we never escape the factory directory.
_FACTORY_DIR = (Path(__file__).resolve().parent.parent / "team_templates").resolve()

# Same cap as yaml_config.py; team templates are typically a few KB.
MAX_BYTES = 256 * 1024


class FactoryTemplateError(Exception):
    """Base class for factory-yaml errors. Subclasses map to HTTP statuses."""

    status: int = 500


class NotFoundError(FactoryTemplateError):
    status = 404


class ValidationError(FactoryTemplateError):
    status = 400


class PathTraversalError(FactoryTemplateError):
    status = 400


def factory_dir() -> Path:
    """Return the resolved team_templates directory. Mostly for tests."""
    return _FACTORY_DIR


def _resolve(name: str) -> Path:
    """Map a logical template name (``agile_team``) to its yaml path.

    Refuses any name that resolves outside ``_FACTORY_DIR`` — this is the
    only allowlist we maintain. Adding a new template = dropping a yaml in
    the directory; no service-side enum to keep in sync.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise PathTraversalError(f"invalid template name: {name!r}")
    candidate = (_FACTORY_DIR / f"{name}.yaml").resolve()
    if _FACTORY_DIR not in candidate.parents:
        raise PathTraversalError(f"path traversal blocked: {name!r}")
    return candidate


def resolve_factory_path(name: str) -> Path:
    """Map a running template's ``name`` to its factory yaml path — GUARDED.

    Tries the hyphen→underscore form first (``agile-team`` → ``agile_team.yaml``),
    then the literal name. BOTH forms go through :func:`_resolve`, so a
    malicious template name (e.g. ``../../secrets`` — the ``name`` field is
    LLM-writable via ``team_template_write_factory`` and is NOT structurally
    validated) is refused with ``PathTraversalError`` instead of being opened.

    Returns the underscored candidate when it exists, else the literal-name
    candidate (existence is the caller's concern). Raises ``PathTraversalError``
    if neither form is a safe name.
    """
    underscored = (name or "agile-team").replace("-", "_")
    candidate = _resolve(underscored)
    if not candidate.exists() and name and name != underscored:
        candidate = _resolve(name)
    return candidate


def load_factory_roles(path: Path) -> dict[str, Any]:
    """Read a factory yaml and return its ``roles`` mapping.

    Single source for the "open + unwrap roles" step the yaml-diff callers
    (REST route + RPC handler) both need — kept here so the shape-guard can't
    drift between the two layers.

    Accepts the two layouts the spawner supports: top-level ``roles:`` or
    nested ``team.roles:``. Only a NON-EMPTY ``team:`` mapping overrides the
    top level — an empty ``team: {}`` or a non-dict ``team: [..]`` falls back
    to the top-level ``roles:`` (an unusable ``team`` should not blank out the
    diff). Returns ``{}`` when no roles are defined.

    Raises ``ValidationError`` if the file isn't a mapping at all (a top-level
    list/scalar), which would otherwise crash the caller with AttributeError.
    """
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValidationError(f"factory yaml at {path} is not a mapping")
    team = doc.get("team")
    container = team if (isinstance(team, dict) and team) else doc
    roles = container.get("roles")
    return roles if isinstance(roles, dict) else {}


def list_factory_templates() -> list[dict[str, Any]]:
    """Return every yaml in ``team_templates/`` with size + parsed display name.

    Display name comes from the yaml's top-level ``name:`` field when it
    exists (e.g. ``agile-team``). Falls back to the filename stem when the
    file is empty or malformed so the UI can still show + offer to fix it.
    """
    if not _FACTORY_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(_FACTORY_DIR.glob("*.yaml")):
        try:
            text = p.read_text(encoding="utf-8")
            doc = yaml.safe_load(text) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("[factory-yaml] %s: %s", p.name, exc)
            doc, text = {}, ""
        team = doc.get("team") if isinstance(doc, dict) and isinstance(doc.get("team"), dict) else doc
        display = (team.get("name") if isinstance(team, dict) else None) or p.stem
        out.append({
            "name": p.stem,
            "filename": p.name,
            "display_name": display,
            "description": (team.get("description") if isinstance(team, dict) else None) or "",
            "size": len(text.encode("utf-8")),
            "exists": True,
        })
    return out


def read_factory_template(name: str) -> dict[str, Any]:
    """Return ``{name, filename, content, parsed, exists, size}``.

    ``parsed`` is the safe-loaded dict (or ``None`` if file is empty or
    invalid) so the UI can render a structural view without re-parsing.
    """
    path = _resolve(name)
    if not path.exists():
        raise NotFoundError(f"template not found: {name}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactoryTemplateError(f"read failed: {exc}") from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Return content so the UI can still show it for hand-editing,
        # but flag the parse failure so the UI can warn before save.
        return {
            "name": name,
            "filename": path.name,
            "content": text,
            "parsed": None,
            "parse_error": str(exc),
            "exists": True,
            "size": len(text.encode("utf-8")),
        }
    return {
        "name": name,
        "filename": path.name,
        "content": text,
        "parsed": parsed,
        "exists": True,
        "size": len(text.encode("utf-8")),
    }


def _validate_template_structure(parsed: Any) -> None:
    """Reject yaml that parses but is shaped wrong for the spawner.

    Minimal contract enforced here:
      * Top-level must be a mapping.
      * Either the top-level OR a ``team:`` child must contain a ``roles``
        mapping (the two layouts the spawner accepts today).
      * ``roles`` itself must be a mapping (``role_name → role_config``).
      * Each role config must be a mapping (an empty dict ``{}`` is OK —
        the spawner falls back to whole-team defaults).

    Wider checks (per-field types, allowed servers, ...) live in
    :mod:`services.team_template_service.validate_patch`; we don't duplicate
    them here because the factory file is read once at spawn time and any
    silly value will surface immediately. The point of this check is solely
    to keep a malformed top-level (missing ``roles``, ``roles`` is a list,
    etc.) from saving and bricking ``spawn_team``.
    """
    if not isinstance(parsed, dict):
        raise ValidationError(
            "template must be a YAML mapping at top level (got "
            f"{type(parsed).__name__})"
        )
    team = parsed.get("team") if isinstance(parsed.get("team"), dict) else None
    roles = (team or parsed).get("roles")
    if not isinstance(roles, dict):
        raise ValidationError(
            "template must have a 'roles' mapping (at top level or under 'team:')"
        )
    if not roles:
        raise ValidationError("template 'roles' mapping must define at least one role")
    for role_name, role_cfg in roles.items():
        if not isinstance(role_name, str) or not role_name:
            raise ValidationError(f"role name must be a non-empty string (got {role_name!r})")
        if not isinstance(role_cfg, dict):
            raise ValidationError(
                f"role '{role_name}' config must be a mapping (got {type(role_cfg).__name__})"
            )


def write_factory_template(name: str, content: str) -> dict[str, Any]:
    """Validate + atomically write yaml. Rotates previous content to ``.bak``.

    Returns ``{name, filename, size, saved: True}``. Raises:
      * ValidationError — content fails ``yaml.safe_load``, is structurally
        invalid (missing ``roles``, etc.), or exceeds cap.
      * PathTraversalError — name escapes the factory dir.
      * FactoryTemplateError — underlying OSError.
    """
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValidationError(f"content exceeds {MAX_BYTES} bytes")

    path = _resolve(name)
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML — file not saved: {exc}") from exc

    # Structural check — refuses syntactically valid but shape-wrong yaml
    # (e.g. ``roles:`` missing) that would brick the next spawn_team call.
    # Critical because team_template_write_factory MCP tool lets the LLM
    # write here without a human review step.
    _validate_template_structure(parsed)

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
        except OSError as exc:
            logger.warning("[factory-yaml] could not write backup for %s: %s", path, exc)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError as exc:
            # Docker bind-mounts a single file by inode — rename-over-mount
            # fails with EBUSY on Linux. Fall back to truncate + in-place write;
            # the .bak from above is still our rollback option.
            if exc.errno != errno.EBUSY:
                raise
            path.write_text(content, encoding="utf-8")
            try: tmp.unlink()
            except OSError: pass
    except OSError as exc:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
        raise FactoryTemplateError(f"write failed: {exc}") from exc

    logger.info("[factory-yaml] %s saved (%d bytes)", path.name, len(content))
    return {
        "name": name,
        "filename": path.name,
        "size": len(content.encode("utf-8")),
        "saved": True,
    }

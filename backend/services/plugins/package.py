"""Read bounded, untrusted plugin packages without executing their content.

The immutable descriptor is an inventory, not an authorization to execute.
Unsupported components fail closed until a runtime adapter is available.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 2000
DATA_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf", ".csv"}


class PackageError(ValueError):
    """An invalid or unsafe package that must not be activated."""


@dataclass(frozen=True)
class PluginSkill:
    name: str
    description: str
    path: str


@dataclass(frozen=True)
class PluginPackage:
    name: str
    version: str | None
    ecosystem: str
    digest: str
    skills: tuple[PluginSkill, ...]
    servers: dict[str, dict[str, Any]]
    blockers: tuple[str, ...]
    license: str | None


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PackageError(f"Invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"JSON object required: {path.name}")
    return value


def _path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in relative:
        raise PackageError("Component path escapes package")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root):
        raise PackageError("Component path escapes package")
    if not resolved.exists():
        raise PackageError("Component path does not exist")
    return resolved


def _inventory(root: Path, max_bytes: int) -> tuple[str, set[str]]:
    digest = hashlib.sha256()
    files: set[str] = set()
    total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(base) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PackageError("Package contains a symlink")
            if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                raise PackageError("Package contains a special file")
            if stat.S_ISDIR(mode):
                continue
            total += path.stat().st_size
            if total > max_bytes or len(files) >= MAX_FILES:
                raise PackageError("Package size limit exceeded")
            relative = path.relative_to(root).as_posix()
            body = path.read_bytes()
            if len(body) > max_bytes or len(body) + total - path.stat().st_size > max_bytes:
                raise PackageError("Package size changed during inspection")
            files.add(relative)
            digest.update(relative.encode() + b"\0" + body + b"\0")
        dirs.sort()
    return digest.hexdigest(), files


def _settings(root: Path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    if (root / "plugin.json").is_file():
        manifest = _json(root / "plugin.json")
        if manifest.get("$schema", SCHEMA) != SCHEMA:
            raise PackageError("Unsupported portable schema")
        extensions = manifest.get("extensions", {})
        if not isinstance(extensions, dict):
            raise PackageError("Invalid extensions object")
        overlay = extensions.get("com.openai")
        if overlay is None and (root / ".codex-plugin/plugin.json").is_file():
            overlay = _json(root / ".codex-plugin/plugin.json")
        if overlay is not None and not isinstance(overlay, dict):
            raise PackageError("Invalid OpenAI extension")
        return "portable", manifest, overlay or {}
    paths = [("claude", root / ".claude-plugin/plugin.json"),
             ("codex", root / ".codex-plugin/plugin.json")]
    found = [(kind, path) for kind, path in paths if path.is_file()]
    if len(found) != 1:
        raise PackageError("Missing or ambiguous host manifest")
    kind, path = found[0]
    manifest = _json(path)
    return kind, manifest, manifest


def _skills(root: Path, settings: dict[str, Any], ecosystem: str) -> tuple[PluginSkill, ...]:
    paths = [root / "skills"]
    if ecosystem != "portable":
        extra = settings.get("skills", [])
        extra = [extra] if isinstance(extra, str) else extra
        if not isinstance(extra, list) or any(not isinstance(p, str) for p in extra):
            raise PackageError("Invalid skill paths")
        paths += [_path(root, p) for p in extra]
    found: dict[str, PluginSkill] = {}
    seen_paths: set[Path] = set()
    if (root / "SKILL.md").is_file():
        paths.append(root / "SKILL.md")
    for directory in paths:
        candidates = ([directory] if directory.is_file()
                      else sorted(directory.rglob("SKILL.md")) if directory.exists() else [])
        for path in candidates:
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                content = path.read_text(encoding="utf-8")
                parts = content.split("---", 2)
                if len(parts) != 3 or parts[0].strip():
                    raise ValueError("Missing frontmatter")
                metadata = yaml.safe_load(parts[1])
                if not isinstance(metadata, dict):
                    raise PackageError("Invalid frontmatter")
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                raise PackageError("Invalid skill frontmatter") from exc
            name = metadata.get("name", path.parent.name)
            description = metadata.get("description")
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise PackageError("Invalid skill name")
            if not isinstance(description, str) or not description.strip():
                raise PackageError("Missing skill description")
            if name in found:
                raise PackageError("Duplicate skill name")
            found[name] = PluginSkill(name, description, path.relative_to(root).as_posix())
    return tuple(found.values())


def _servers(root: Path, settings: dict[str, Any], ecosystem: str) -> dict[str, dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    default = root / ("mcp.json" if ecosystem == "portable" else ".mcp.json")
    if default.is_file():
        configs.append(_json(default))
    declared = settings.get("mcpServers")
    if declared is not None:
        declarations = declared if isinstance(declared, list) else [declared]
        for item in declarations:
            if isinstance(item, str):
                configs.append(_json(_path(root, item)))
            elif isinstance(item, dict):
                configs.append({"mcpServers": item})
            else:
                raise PackageError("Invalid MCP declaration")
    out: dict[str, dict[str, Any]] = {}
    for config in configs:
        mapping = config.get("mcpServers")
        if not isinstance(mapping, dict):
            raise PackageError("Invalid MCP server map")
        for name, server in mapping.items():
            if not NAME.fullmatch(name) or not isinstance(server, dict):
                raise PackageError("Invalid MCP server name or config")
            transport = server.get("type", "stdio" if server.get("command") else "http")
            transport = "http" if transport == "streamable-http" else transport
            if transport not in {"stdio", "http", "sse"}:
                raise PackageError("Unsupported MCP transport")
            out[name] = {**server, "transport": transport}
    return out


def inspect_package(root: Path, *, max_bytes: int = MAX_BYTES) -> PluginPackage:
    """Return a common inventory; blockers prohibit automatic activation."""
    if root.is_symlink() or not root.is_dir():
        raise PackageError("Invalid package root")
    root = root.resolve()
    digest, files = _inventory(root, max_bytes)
    ecosystem, manifest, settings = _settings(root)
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise PackageError("Invalid plugin name")
    blockers: set[str] = set()
    for key, prefix, blocker in [
        ("hooks", "hooks/", "hooks"), ("agents", "agents/", "agents"),
        ("commands", "commands/", "commands"), ("apps", ".app.json", "host_connectors"),
        ("lspServers", ".lsp.json", "lsp"),
    ]:
        if key in settings or any(p.startswith(prefix) for p in files):
            blockers.add(blocker)
    if any(Path(p).suffix.lower() not in DATA_SUFFIXES or p.startswith(("bin/", "scripts/")) for p in files):
        blockers.add("executable_content")
    for key in ("dependencies", "settings", "userConfig", "channels", "workflows", "experimental", "outputStyles"):
        if key in settings or key in manifest:
            blockers.add(f"unsupported_{key}")
    for prefix, key in [("output-styles/", "outputStyles"), ("themes/", "themes"),
                        ("monitors/", "monitors"), ("workflows/", "workflows")]:
        if any(path.startswith(prefix) for path in files):
            blockers.add(f"unsupported_{key}")
    servers = _servers(root, settings, ecosystem)
    if servers:
        blockers.add("mcp_requires_policy_review")
    skills = _skills(root, settings, ecosystem)
    if not skills and not servers:
        blockers.add("no_supported_capabilities")
    return PluginPackage(name, manifest.get("version"), ecosystem, digest,
                         skills, servers,
                         tuple(sorted(blockers)), manifest.get("license"))

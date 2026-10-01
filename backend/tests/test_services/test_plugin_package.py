"""Package contracts: parsing never executes third-party content."""

import json
from pathlib import Path

import pytest

from services.plugins.package import PackageError, inspect_package


def write(root: Path, path: str, value: object) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) if isinstance(value, dict) else str(value))


@pytest.mark.parametrize(
    "manifest,ecosystem",
    [
        (".claude-plugin/plugin.json", "claude"),
        (".codex-plugin/plugin.json", "codex"),
        ("plugin.json", "portable"),
    ],
)
def test_three_formats_share_inventory(tmp_path, manifest, ecosystem):
    write(tmp_path, manifest, {"name": "review", "version": "1.0.0"})
    write(
        tmp_path,
        "skills/review/SKILL.md",
        "---\ndescription: Review changes\n---\nRead references/check.md",
    )
    write(tmp_path, "skills/review/references/check.md", "Keep supporting resources.")
    package = inspect_package(tmp_path)
    assert package.ecosystem == ecosystem
    assert package.skills[0].name == "review"
    assert package.skills[0].path == "skills/review/SKILL.md"
    assert package.blockers == ()
    assert len(package.digest) == 64


def test_digest_covers_helper_not_just_manifest(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(tmp_path, "skills/review/SKILL.md", "---\ndescription: Review\n---\nReview.")
    write(tmp_path, "skills/review/helper.py", "print('old')")
    before = inspect_package(tmp_path)
    write(tmp_path, "skills/review/helper.py", "print('new')")
    assert inspect_package(tmp_path).digest != before.digest
    assert "executable_content" in before.blockers


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "./../../outside"])
def test_custom_components_cannot_escape_root(tmp_path, path):
    write(tmp_path, ".claude-plugin/plugin.json", {"name": "review", "skills": path})
    with pytest.raises(PackageError, match="path"):
        inspect_package(tmp_path)


def test_symlink_rejected_even_for_internal_target(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(tmp_path, "resource.md", "resource")
    (tmp_path / "alias.md").symlink_to("resource.md")
    with pytest.raises(PackageError, match="symlink"):
        inspect_package(tmp_path)


@pytest.mark.parametrize(
    "path,blocker",
    [
        ("hooks/hooks.json", "hooks"),
        ("agents/reviewer.md", "agents"),
        ("commands/review.md", "commands"),
        (".app.json", "host_connectors"),
        (".lsp.json", "lsp"),
        ("bin/helper", "executable_content"),
    ],
)
def test_unsupported_components_cannot_silently_be_ready(tmp_path, path, blocker):
    write(tmp_path, ".codex-plugin/plugin.json", {"name": "review"})
    write(tmp_path, path, {} if path.endswith("json") else "content")
    assert blocker in inspect_package(tmp_path).blockers


def test_portable_has_explicit_mcp_transport(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(
        tmp_path,
        "mcp.json",
        {"mcpServers": {"docs": {"type": "http", "url": "https://docs.example/mcp"}}},
    )
    package = inspect_package(tmp_path)
    assert package.servers["docs"]["transport"] == "http"
    assert "mcp_requires_policy_review" in package.blockers


def test_manifest_declared_inline_hook_detected(tmp_path):
    write(
        tmp_path,
        ".claude-plugin/plugin.json",
        {"name": "review", "hooks": {"Stop": []}},
    )
    assert "hooks" in inspect_package(tmp_path).blockers


def test_codex_connector_not_misrepresented_as_mcp(tmp_path):
    write(
        tmp_path, ".codex-plugin/plugin.json", {"name": "drive", "apps": "./.app.json"}
    )
    write(
        tmp_path,
        ".app.json",
        {"apps": {"drive": {"id": "connector_private", "required": True}}},
    )
    package = inspect_package(tmp_path)
    assert not package.servers
    assert "host_connectors" in package.blockers


def test_multiple_host_manifests_need_explicit_choice(tmp_path):
    write(tmp_path, ".claude-plugin/plugin.json", {"name": "review"})
    write(tmp_path, ".codex-plugin/plugin.json", {"name": "different"})
    with pytest.raises(PackageError, match="ambiguous"):
        inspect_package(tmp_path)


@pytest.mark.parametrize("name", ["../../review", "Review", "", "x" * 65])
def test_invalid_namespace_rejected(tmp_path, name):
    write(tmp_path, "plugin.json", {"name": name})
    with pytest.raises(PackageError, match="name"):
        inspect_package(tmp_path)


def test_unknown_portable_schema_fails_closed(tmp_path):
    write(
        tmp_path,
        "plugin.json",
        {"name": "review", "$schema": "https://evil.example/schema.json"},
    )
    with pytest.raises(PackageError, match="schema"):
        inspect_package(tmp_path)


def test_content_is_bounded(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(tmp_path, "large.md", "x" * 1025)
    with pytest.raises(PackageError, match="size"):
        inspect_package(tmp_path, max_bytes=1024)


def test_portable_inline_overlay_replaces_legacy_overlay(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review", "extensions": {"com.openai": {}}})
    write(
        tmp_path, ".codex-plugin/plugin.json", {"name": "review", "hooks": {"Stop": []}}
    )
    assert "hooks" not in inspect_package(tmp_path).blockers


def test_portable_fallback_overlay_is_inspected(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(
        tmp_path, ".codex-plugin/plugin.json", {"name": "review", "hooks": {"Stop": []}}
    )
    assert "hooks" in inspect_package(tmp_path).blockers


def test_root_skill_is_discovered(tmp_path):
    write(tmp_path, ".claude-plugin/plugin.json", {"name": "review"})
    write(
        tmp_path,
        "SKILL.md",
        "---\nname: review\ndescription: Review changes\n---\nReview.",
    )
    assert inspect_package(tmp_path).skills[0].path == "SKILL.md"


def test_unknown_component_directory_does_not_pass_ready(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    write(tmp_path, "output-styles/my-style.md", "Apply these instructions globally")
    assert "unsupported_outputStyles" in inspect_package(tmp_path).blockers


def test_empty_plugin_does_not_claim_capability(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review"})
    assert "no_supported_capabilities" in inspect_package(tmp_path).blockers


def test_duplicate_mcp_names_cannot_override_reviewed_server(tmp_path):
    write(
        tmp_path,
        ".claude-plugin/plugin.json",
        {"name": "review", "mcpServers": {"echo": {"command": "python"}}},
    )
    write(tmp_path, ".mcp.json", {"mcpServers": {"echo": {"command": "node"}}})
    with pytest.raises(PackageError, match="Duplicate"):
        inspect_package(tmp_path)


def test_non_string_version_rejected(tmp_path):
    write(tmp_path, "plugin.json", {"name": "review", "version": {"payload": "hidden"}})
    with pytest.raises(PackageError, match="version"):
        inspect_package(tmp_path)

"""
Unit tests for skill path resolution.

Verifies that SkillRegistry correctly resolves skill directories
in all environments (local dev, Docker) and handles both individual
skill directories (SKILL.md at root) and parent directories (scan children).
"""
import os
import tempfile
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — create temporary skill trees
# ---------------------------------------------------------------------------

VALID_SKILL_MD = textwrap.dedent("""\
    ---
    name: test-skill
    description: A test skill for unit testing
    ---
    # Test Skill
    Instructions for test skill.
""")

VALID_SKILL_MD_2 = textwrap.dedent("""\
    ---
    name: another-skill
    description: Another skill for unit testing
    ---
    # Another Skill
    Instructions for another skill.
""")

INVALID_SKILL_MD = textwrap.dedent("""\
    ---
    description: Missing required 'name' field
    ---
    # Bad Skill
""")


@pytest.fixture
def temp_skill_dir(tmp_path):
    """Create a single skill directory with SKILL.md at root."""
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(VALID_SKILL_MD)
    return skill_dir


@pytest.fixture
def temp_parent_dir(tmp_path):
    """Create a parent directory with multiple child skill directories."""
    parent = tmp_path / "skills"
    parent.mkdir()

    skill_a = parent / "skill-a"
    skill_a.mkdir()
    (skill_a / "SKILL.md").write_text(VALID_SKILL_MD)

    skill_b = parent / "skill-b"
    skill_b.mkdir()
    (skill_b / "SKILL.md").write_text(VALID_SKILL_MD_2)

    # Non-skill directory (no SKILL.md)
    (parent / "not-a-skill").mkdir()

    # Regular file (should be ignored)
    (parent / "README.md").write_text("# Skills")

    return parent


@pytest.fixture
def temp_mixed_dir(tmp_path):
    """Parent dir with valid + invalid skills."""
    parent = tmp_path / "skills"
    parent.mkdir()

    valid = parent / "valid-skill"
    valid.mkdir()
    (valid / "SKILL.md").write_text(VALID_SKILL_MD)

    invalid = parent / "invalid-skill"
    invalid.mkdir()
    (invalid / "SKILL.md").write_text(INVALID_SKILL_MD)

    return parent


# ---------------------------------------------------------------------------
# Tests — SkillRegistry._load_directory
# ---------------------------------------------------------------------------

class TestLoadDirectoryIndividual:
    """Test loading a single skill directory (SKILL.md at directory root)."""

    def test_individual_skill_dir_returns_manifest(self, temp_skill_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_skill_dir)
        assert len(manifests) == 1
        assert manifests[0].name == "test-skill"
        assert manifests[0].description == "A test skill for unit testing"

    def test_individual_skill_dir_has_correct_path(self, temp_skill_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_skill_dir)
        assert manifests[0].path == temp_skill_dir / "SKILL.md"

    def test_individual_skill_dir_has_body(self, temp_skill_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_skill_dir)
        assert "Test Skill" in manifests[0].body


class TestLoadDirectoryParentScan:
    """Test loading a parent directory that contains child skill dirs."""

    def test_parent_dir_scans_children(self, temp_parent_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_parent_dir)
        names = {m.name for m in manifests}
        assert names == {"test-skill", "another-skill"}

    def test_parent_dir_ignores_non_skill_dirs(self, temp_parent_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_parent_dir)
        # Should only find 2 skills, not "not-a-skill" dir
        assert len(manifests) == 2

    def test_parent_dir_ignores_files(self, temp_parent_dir):
        from fast_agent.skills.registry import SkillRegistry

        manifests = SkillRegistry._load_directory(temp_parent_dir)
        paths = {str(m.path) for m in manifests}
        # README.md should not appear
        assert not any("README" in p for p in paths)


class TestLoadDirectoryErrors:
    """Test error handling in _load_directory."""

    def test_invalid_skill_reports_error(self, temp_mixed_dir):
        from fast_agent.skills.registry import SkillRegistry

        errors = []
        manifests = SkillRegistry._load_directory(temp_mixed_dir, errors)
        assert len(manifests) == 1  # only valid one
        assert len(errors) == 1    # invalid reported
        assert "Missing" in errors[0]["error"]

    def test_empty_dir_returns_empty(self, tmp_path):
        from fast_agent.skills.registry import SkillRegistry

        empty = tmp_path / "empty"
        empty.mkdir()
        manifests = SkillRegistry._load_directory(empty)
        assert manifests == []


# ---------------------------------------------------------------------------
# Tests — SkillRegistry via constructor (full flow)
# ---------------------------------------------------------------------------

class TestSkillRegistryFullFlow:
    """Test the full SkillRegistry constructor → load_manifests flow."""

    def test_individual_dir_via_constructor(self, temp_skill_dir, tmp_path):
        from fast_agent.skills.registry import SkillRegistry

        registry = SkillRegistry(base_dir=tmp_path, directories=[temp_skill_dir])
        manifests = registry.load_manifests()
        assert len(manifests) == 1
        assert manifests[0].name == "test-skill"

    def test_parent_dir_via_constructor(self, temp_parent_dir, tmp_path):
        from fast_agent.skills.registry import SkillRegistry

        registry = SkillRegistry(base_dir=tmp_path, directories=[temp_parent_dir])
        manifests = registry.load_manifests()
        assert len(manifests) == 2

    def test_relative_path_resolved_against_base_dir(self, tmp_path):
        """Ensure relative paths in directories are resolved against base_dir."""
        from fast_agent.skills.registry import SkillRegistry

        # Create skill under tmp_path/rel/my-skill/SKILL.md
        skill_dir = tmp_path / "rel" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(VALID_SKILL_MD)

        # Use relative path "rel/my-skill" with base_dir=tmp_path
        registry = SkillRegistry(
            base_dir=tmp_path,
            directories=[Path("rel/my-skill")],
        )
        manifests = registry.load_manifests()
        assert len(manifests) == 1
        assert manifests[0].name == "test-skill"

    def test_nonexistent_dir_warns(self, tmp_path):
        from fast_agent.skills.registry import SkillRegistry

        registry = SkillRegistry(
            base_dir=tmp_path,
            directories=[Path("does-not-exist")],
        )
        manifests = registry.load_manifests()
        assert manifests == []
        assert len(registry.warnings) == 1

    def test_multiple_directories(self, temp_skill_dir, temp_parent_dir, tmp_path):
        from fast_agent.skills.registry import SkillRegistry

        registry = SkillRegistry(
            base_dir=tmp_path,
            directories=[temp_skill_dir, temp_parent_dir],
        )
        manifests = registry.load_manifests()
        # temp_skill_dir has "test-skill", temp_parent_dir has "test-skill" + "another-skill"
        # Dedup: "test-skill" from parent overrides individual, + "another-skill"
        names = {m.name for m in manifests}
        assert "another-skill" in names
        assert "test-skill" in names


# ---------------------------------------------------------------------------
# Tests — Cross-environment path resolution
# ---------------------------------------------------------------------------

class TestCrossEnvironmentPaths:
    """Verify skill resolution works regardless of CWD."""

    def test_cwd_independence(self, tmp_path):
        """Skills resolve correctly even when CWD is different from base_dir."""
        from fast_agent.skills.registry import SkillRegistry

        # Project structure: tmp_path/.fast-agent/skills/my-skill/SKILL.md
        skills_base = tmp_path / ".fast-agent" / "skills"
        skill = skills_base / "my-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(VALID_SKILL_MD)

        # Simulate different CWDs
        original_cwd = os.getcwd()
        try:
            for test_cwd in [str(tmp_path), tempfile.gettempdir(), "/"]:
                os.chdir(test_cwd)
                registry = SkillRegistry(
                    base_dir=tmp_path,
                    directories=[Path(".fast-agent/skills/my-skill")],
                )
                manifests = registry.load_manifests()
                assert len(manifests) == 1, (
                    f"Failed with CWD={test_cwd}: got {len(manifests)} manifests"
                )
        finally:
            os.chdir(original_cwd)

    def test_absolute_path_always_works(self, tmp_path):
        """Absolute path to skill dir works regardless of base_dir."""
        from fast_agent.skills.registry import SkillRegistry

        skill = tmp_path / "my-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(VALID_SKILL_MD)

        # base_dir is something completely different
        other_base = tmp_path / "other"
        other_base.mkdir()

        registry = SkillRegistry(
            base_dir=other_base,
            directories=[skill],  # absolute path
        )
        manifests = registry.load_manifests()
        assert len(manifests) == 1

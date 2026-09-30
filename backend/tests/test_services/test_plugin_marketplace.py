"""Marketplace data never grants trust to an external publisher."""
import pytest
from services.plugins.marketplace import normalize_marketplace
from services.plugins.package import PackageError


def test_claude_relative_source_uses_catalog_repository():
    items = normalize_marketplace({"name": "official", "plugins": [{"name": "review", "source": "./plugins/review"}]}, "anthropics/claude-code", "a" * 40)
    assert items[0]["repo"] == "anthropics/claude-code"
    assert items[0]["commit"] == "a" * 40
    assert items[0]["subdirectory"] == "plugins/review"


def test_codex_relative_source_is_normalized():
    items = normalize_marketplace({"name": "official", "plugins": [{"name": "review", "source": {"source": "local", "path": "./plugins/review"}}]}, "openai/plugins", "a" * 40)
    assert items[0]["subdirectory"] == "plugins/review"


def test_external_source_cannot_inherit_marketplace_commit_or_trust():
    items = normalize_marketplace({"plugins": [{"name": "review", "source": {"source": "github", "repo": "someone/plugin"}}]}, "openai/plugins", "a" * 40)
    assert items[0]["repo"] == "someone/plugin"
    assert items[0]["commit"] is None
    assert items[0]["requires_source_approval"] is True


def test_source_ref_is_not_treated_as_immutable_commit():
    items = normalize_marketplace({"plugins": [{"name": "review", "source": {"source": "github", "repo": "someone/plugin", "ref": "main"}}]}, "openai/plugins", "a" * 40)
    assert items[0]["commit"] is None


def test_catalog_path_traversal_rejected():
    with pytest.raises(PackageError, match="path"):
        normalize_marketplace({"plugins": [{"name": "review", "source": "../outside"}]}, "openai/plugins", "a" * 40)


@pytest.mark.parametrize("source", [
    {"source": "url", "url": "https://github.com/CrowdStrike/foundry-skills.git"},
    {"source": "git-subdir", "url": "https://github.com/CrowdStrike/foundry-skills.git", "path": "codex-packages/review"},
])
def test_real_codex_external_sources_do_not_block_whole_catalog(source):
    items = normalize_marketplace({"plugins": [{"name": "review", "source": source}]}, "openai/plugins", "a" * 40)
    assert items[0]["repo"] == "CrowdStrike/foundry-skills"
    assert items[0]["commit"] is None
    assert items[0]["requires_source_approval"] is True


@pytest.mark.parametrize("url", ["https://github.com.evil.test/x/y", "https://localhost/x/y", "https://github.com/user/repo?token=secret"])
def test_catalog_cannot_turn_external_url_into_network_request(url):
    with pytest.raises(PackageError):
        normalize_marketplace({"plugins": [{"name": "review", "source": {"source": "url", "url": url}}]}, "openai/plugins", "a" * 40)

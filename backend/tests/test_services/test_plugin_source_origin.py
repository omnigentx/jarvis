"""Source reputation is derived from exact identity, never publisher labels."""

import pytest
from services.plugins.provenance import source_origin
from services.plugins.marketplace import normalize_marketplace
from routes.plugins import public_record


@pytest.mark.parametrize(
    "repo,kind,publisher",
    [
        ("openai/plugins", "official", "OpenAI"),
        ("anthropics/claude-code", "official", "Anthropic"),
        ("OpenAI/Plugins", "official", "OpenAI"),
        ("openai-fake/plugins", "external", None),
        ("openai/plugins.evil", "external", None),
        ("someone/plugin", "external", None),
    ],
)
def test_exact_repository_identity(repo, kind, publisher):
    result = source_origin(repo)
    assert result["kind"] == kind
    assert result["publisher"] == publisher


def test_external_listing_does_not_inherit_official_publisher():
    item = normalize_marketplace(
        {
            "plugins": [
                {
                    "name": "review",
                    "author": {"name": "OpenAI"},
                    "source": {"source": "github", "repo": "someone/plugin"},
                }
            ]
        },
        "openai/plugins",
        "a" * 40,
    )[0]
    assert item["source_origin"] == {
        "kind": "community",
        "publisher": None,
        "marketplace": "OpenAI",
        "repo": "someone/plugin",
    }


def test_untrusted_catalog_cannot_claim_official_marketplace():
    item = normalize_marketplace(
        {"plugins": [{"name": "review", "source": "./review"}]},
        "someone/plugins",
        "a" * 40,
    )[0]
    assert item["source_origin"]["kind"] == "external"
    assert item["source_origin"]["marketplace"] is None


def test_inventory_recomputes_origin_instead_of_trusting_stored_label():
    item = public_record(
        {
            "repo": "someone/plugin",
            "source_origin": {"kind": "official", "publisher": "OpenAI"},
        }
    )
    assert item["source_origin"]["kind"] == "external"

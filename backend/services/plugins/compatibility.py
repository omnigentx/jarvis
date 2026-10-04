"""Explicit host metadata adapters; unknown capabilities remain blocked."""

from pathlib import Path
import json
import yaml

ROVO_ENDPOINTS = {
    "https://mcp.atlassian.com/v1/mcp/authv2",
    "https://mcp.atlassian.com/v2/mcp",
}
ROVO_CONNECTOR = "connector_692de805e3ec8191834719067174a384"


def display_metadata(root: Path) -> bool:
    """Codex interface metadata does not define executable agents."""
    try:
        value = yaml.safe_load((root / "agents/openai.yaml").read_text())
        return (
            isinstance(value, dict)
            and set(value) == {"interface"}
            and isinstance(value["interface"], dict)
            and set(value["interface"])
            <= {
                "display_name",
                "short_description",
                "icon_small",
                "icon_large",
                "brand_color",
            }
            and all(isinstance(v, str) for v in value["interface"].values())
        )
    except (OSError, ValueError, yaml.YAMLError):
        return False


def native_connector(root: Path, servers: dict) -> bool:
    """Map this known vendor connector to its declared native MCP equivalent.

    Jarvis does not emulate Codex app IDs. This explicit adapter offers Rovo's
    native tools, with independently authorized Atlassian account access.
    """
    try:
        apps = json.loads((root / ".app.json").read_text())
        return (
            apps == {"apps": {"atlassian-rovo": {"id": ROVO_CONNECTOR}}}
            and set(servers) == {"atlassian-rovo"}
            and servers["atlassian-rovo"].get("transport") == "http"
            and servers["atlassian-rovo"].get("url") in ROVO_ENDPOINTS
        )
    except (OSError, ValueError):
        return False

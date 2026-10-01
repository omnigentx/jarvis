"""First-party acceptance fixture; no credentials, network or filesystem tools."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("Jarvis plugin acceptance")


@server.tool()
def echo(value: str) -> str:
    """Return the supplied acceptance marker, prefixed by sandbox:."""
    return "sandbox:" + value


if __name__ == "__main__":
    server.run(transport="stdio")

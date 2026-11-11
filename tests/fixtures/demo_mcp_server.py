"""A tiny MCP server used by the tests: one read-only tool, one that changes things, one that fails."""

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

app = FastMCP("demo")


@app.tool(annotations=ToolAnnotations(readOnlyHint=True))
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@app.tool()
def erase(target: str) -> str:
    """Erase something."""
    return f"erased {target}"


@app.tool()
def boom() -> str:
    """Always fails."""
    raise ValueError("nope")


if __name__ == "__main__":
    app.run()

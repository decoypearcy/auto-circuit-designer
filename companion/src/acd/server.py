"""stdio MCP server entry point (acd-mcp)."""

from __future__ import annotations

from . import tools

try:  # official SDK 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:  # official SDK 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]

TOOL_NAMES = (
    "init_project", "apply_profile", "lock_check", "confirm_spec", "register_part",
    "verify_part", "check_parts", "run_erc", "run_drc", "review_summary", "gate_status",
    "backup", "list_backups", "restore_backup", "approve", "export_release",
    "verify_release", "new_revision", "fix_bom_flags",
)


def build_server() -> FastMCP:
    server = FastMCP("acd")
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        server.tool(name=name)(fn)
    return server


def main() -> None:
    build_server().run()  # stdio transport


if __name__ == "__main__":
    main()

"""stdio MCP server entry point (acd-mcp)."""

from __future__ import annotations

import functools
import traceback
from pathlib import Path
from typing import Any, Callable

from . import tools
from .config import AcdError

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


def wrap_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return failures as a normal result so the caller sees the reason.

    Without this FastMCP reports only "Error executing tool". functools.wraps keeps the
    signature (inspect follows __wrapped__), so the input schema is unchanged.
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except AcdError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - every failure must reach the caller
            last = traceback.extract_tb(exc.__traceback__)[-1]
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "where": f"{Path(last.filename).name}:{last.lineno}"}
    return wrapper


def build_server() -> FastMCP:
    server = FastMCP("acd")
    for name in TOOL_NAMES:
        fn = getattr(tools, name)
        server.tool(name=name)(wrap_tool(fn))
    return server


def main() -> None:
    build_server().run()  # stdio transport


if __name__ == "__main__":
    main()

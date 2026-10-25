"""Регистрация всех инструментов MCP."""

from __future__ import annotations

from mcp.server import MCPServer

from .context import AppContext
from .image import register_image_tools
from .info import register_info_tools
from .tasks import register_task_tools


def register_all(mcp: MCPServer, ctx: AppContext) -> None:
    register_image_tools(mcp, ctx)
    register_task_tools(mcp, ctx)
    register_info_tools(mcp, ctx)


__all__ = ["AppContext", "register_all"]

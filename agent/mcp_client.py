"""Thin async wrapper around the FastMCP stdio client. One session per assistant turn (see README)."""
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

REPO_ROOT = Path(__file__).resolve().parent.parent


class McpCallError(Exception):
    """The MCP server rejected a call at the protocol level (schema violation, crash)."""


def server_env(db_path: str | Path | None = None) -> dict[str, str]:
    """Minimal environment for the server subprocess: no API keys or other secrets are passed down."""
    env = {"PATH": os.environ.get("PATH", "")}
    chosen = db_path or os.environ.get("RECRUITING_DB_PATH")
    if chosen:
        env["RECRUITING_DB_PATH"] = str(chosen)
    return env


class McpSession:
    def __init__(self, db_path: str | Path | None = None, extra_args: tuple[str, ...] = ()):
        transport = StdioTransport(command=sys.executable, args=["-m", "mcp_server.server", *extra_args],
                                   env=server_env(db_path), cwd=str(REPO_ROOT), keep_alive=False)
        self._client = Client(transport)

    async def __aenter__(self) -> "McpSession":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> dict[str, str]:
        """{tool name: description} as advertised by the server (the allowlist is derived from this)."""
        return {t.name: t.description or "" for t in await self._client.list_tools()}

    async def list_tool_schemas(self) -> dict[str, dict]:
        """{tool name: JSON input schema}; used by the executor/server parity test."""
        return {t.name: t.input_schema for t in await self._client.list_tools()}

    async def call_tool(self, name: str, arguments: dict) -> dict:
        r = await self._client.call_tool(name, arguments, raise_on_error=False)
        if r.is_error or not isinstance(r.structured_content, dict):
            raise McpCallError("tool call failed at the protocol level")
        return r.structured_content

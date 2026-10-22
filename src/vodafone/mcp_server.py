"""`vodafone mcp`: a read-only stdio MCP server, a thin wrapper over the same api functions as the CLI.

Each tool's text content is exactly what the CLI prints: its stdout JSON on success (also returned
as structured content, typed by the CLI's output model), or its stderr {"error": {...}} payload
with is_error set. No write tools.
"""

import logging
import threading
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import api
from .client import DxlClient
from .errors import VodafoneError
from .models import InvoicesOutput, LinesOutput, MeOutput, UsageOutput
from .output import render, render_error

TOOL_NAMES = ("get_account", "list_lines", "get_usage", "list_invoices")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = (
    "Read-only access to the user's own Mi Vodafone (Vodafone España) account. Tools return the "
    "same JSON as the `vodafone` CLI. Failed calls return {\"error\": {\"type\", \"message\", "
    "\"exit_code\", ...}}; type auth_expired means the user must sign in again with "
    "`vodafone auth login` (or re-export the token file from a logged-in browser)."
)

AccountArg = Annotated[
    str | None,
    Field(description="Customer account id. Only needed when the login has several (see get_account)."),
]
LineArg = Annotated[
    str | None,
    Field(description="Line id (MSISDN or fibre service id) or its unique trailing digits. Omit for all lines."),
]

# Hosts may issue tool calls in parallel; run them one at a time so the API sees the same paced,
# serial traffic as the CLI. Sync tools execute in worker threads, hence a threading lock.
_serial = threading.Lock()


def _server_version() -> str:
    try:
        return version("vodafone-cli")
    except PackageNotFoundError:
        return ""


def _error(err: VodafoneError) -> CallToolResult:
    return CallToolResult(is_error=True, content=[TextContent(type="text", text=render_error(err))])


def build_server(tokens_path: Path, default_account: str | None = None) -> MCPServer:
    server = MCPServer(name="vodafone", version=_server_version(), instructions=INSTRUCTIONS)
    # MCPServer configures root logging at INFO, where httpx logs every request URL (with account
    # ids and the holder's NIF in query strings) to the host's server log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    def call(fn: Callable[[DxlClient], BaseModel]) -> CallToolResult:
        with _serial:
            try:
                payload = api.run_with_client(tokens_path, fn).model_dump(mode="json")
                text = render(payload)
            except VodafoneError as err:
                return _error(err)
            except Exception as exc:  # e.g. render() refusing a non-finite number
                return _error(VodafoneError(f"unexpected internal error: {type(exc).__name__}"))
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)

    @server.tool(annotations=READ_ONLY)
    def get_account() -> Annotated[CallToolResult, MeOutput]:
        """Customer account(s): status, segment, debt, flags and holder contact. Same JSON as `vodafone me`."""
        return call(api.get_me)

    @server.tool(annotations=READ_ONLY)
    def list_lines(account: AccountArg = None) -> Annotated[CallToolResult, LinesOutput]:
        """Every line (mobile, landline, fibre) with its tariff. Same JSON as `vodafone lines`."""
        return call(lambda client: api.get_lines(client, account or default_account))

    @server.tool(annotations=READ_ONLY)
    def get_usage(line: LineArg = None, account: AccountArg = None) -> Annotated[CallToolResult, UsageOutput]:
        """In-plan data / voice / SMS allowances per line. Same JSON as `vodafone usage [--line]`."""
        return call(lambda client: api.get_usage(client, account or default_account, line))

    @server.tool(annotations=READ_ONLY)
    def list_invoices(account: AccountArg = None) -> Annotated[CallToolResult, InvoicesOutput]:
        """Invoices with amount, dates and status. Same JSON as `vodafone invoices list`."""
        return call(lambda client: api.get_invoices(client, account or default_account))

    return server

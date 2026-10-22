"""`vodafone mcp`: a stdio MCP server, a thin wrapper over the same api functions as the CLI.

The read tools' text content is exactly what the CLI prints: its stdout JSON on success (also
returned as structured content, typed by the CLI's output model), or its stderr {"error": {...}}
payload with is_error set; a not-signed-in error points at the `login` tool instead of the CLI.
`login` signs the user in through a local browser page (vodafone.signin) and writes only the token
file; `auth_status` reports on that file, offline. Nothing writes to the Vodafone account.
"""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import NoBackChannelError
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestURLParams,
    InputRequiredResult,
    TextContent,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from . import api, auth, package_version, signin
from .client import DxlClient
from .errors import AuthError, VodafoneError
from .models import AuthStatusOutput, InvoicesOutput, LinesOutput, MeOutput, UsageOutput
from .output import render, render_error
from .tokens import FileTokenStore

READ_TOOLS = ("get_account", "list_lines", "get_usage", "list_invoices")
TOOL_NAMES = (*READ_TOOLS, "auth_status", "login")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
OFFLINE_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
# Writes the local token file (so not read-only), and nothing in the Vodafone account.
SIGN_IN = ToolAnnotations(
    title="Sign in to Vodafone",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

INSTRUCTIONS = (
    "Read-only access to the user's own Mi Vodafone (Vodafone España) account. The read tools "
    "return the same JSON as the `vodafone` CLI. Failed calls return {\"error\": {\"type\", "
    "\"message\", \"exit_code\", ...}}. Type auth_expired means the user is not signed in to "
    "Vodafone, or the session ended: the `login` tool signs them in, so offer it, or call it when "
    "the user asks to sign in. It opens a sign-in page in the user's browser on this computer, "
    "where they type their password and SMS code; never ask for either in the chat. `auth_status` "
    "says whether the user is signed in without contacting Vodafone. A session renews itself for "
    "about 90 days. (In a terminal, `vodafone auth login` signs in too.)"
)

NOT_SIGNED_IN = (
    "Not signed in to Vodafone: {reason}. Ask to sign in to Vodafone: the login tool opens a sign-in "
    "page in the browser (in a terminal, `vodafone auth login`)."
)

# Seconds `login` waits for the browser sign-in to finish when the host showed the link itself.
LOGIN_WAIT = 45.0
_ELICITATION = "vodafone_signin"  # the key of the URL elicitation in an InputRequiredResult
_ELICIT_MESSAGE = (
    "Sign in to your Mi Vodafone account. The page runs on this computer: your password and SMS code go "
    "from it straight to Vodafone, never through this chat."
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
# serial traffic as the CLI. Sync tools execute in worker threads, hence a threading lock; the
# browser sign-in's requests take it too.
_serial = threading.Lock()


def _error(err: VodafoneError) -> CallToolResult:
    if isinstance(err, AuthError) and err.kind == "auth_expired":
        reason = getattr(err, "reason", None) or "the session expired"
        err = AuthError(NOT_SIGNED_IN.format(reason=reason), status=err.status, transaction_id=err.transaction_id)
    return CallToolResult(is_error=True, content=[TextContent(type="text", text=render_error(err))])


def _text(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _offers_url_elicitation(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return caps is not None and caps.elicitation is not None and caps.elicitation.url is not None


def build_server(
    tokens_path: Path,
    default_account: str | None = None,
    *,
    login_wait: float = LOGIN_WAIT,
    open_browser: Callable[[str], bool] | None = None,
) -> MCPServer:
    server = MCPServer(name="vodafone", version=package_version(), instructions=INSTRUCTIONS)
    # MCPServer configures root logging at INFO, where httpx logs every request URL (with account
    # ids and the holder's NIF in query strings) to the host's server log.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    store = FileTokenStore(tokens_path)
    signins = signin.SignInManager(store, serial=_serial)

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

    @server.tool(annotations=OFFLINE_READ_ONLY)
    def auth_status() -> Annotated[CallToolResult, AuthStatusOutput]:
        """Whether the user is signed in to Vodafone, and when the session expires. Offline: reads the
        local token file only, never contacts Vodafone or refreshes. `vodafone auth status`'s JSON
        plus signed_in and reason."""
        try:
            payload = auth.sign_in_status(store, datetime.now(UTC)).model_dump(mode="json")
            text = render(payload)
        except VodafoneError as err:
            return _error(err)
        except Exception as exc:
            return _error(VodafoneError(f"unexpected internal error: {type(exc).__name__}"))
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=payload)

    async def wait_for(attempt: signin.SignIn) -> CallToolResult:
        deadline = anyio.current_time() + login_wait
        while attempt.active and anyio.current_time() < deadline:
            await anyio.sleep(0.2)
        if attempt.done.is_set():
            return _signed_in(attempt)
        if not attempt.active:
            return _text("The sign-in page has closed (it expired or was replaced). Call login again for a new link.")
        return _still_waiting(attempt)

    @server.tool(annotations=SIGN_IN)
    async def login(ctx: Context) -> CallToolResult | InputRequiredResult:
        """Sign the user in to their Mi Vodafone account, in their browser. Opens a sign-in page on this
        computer (http://127.0.0.1) where the user types their usuario, password and SMS code; none of
        them pass through the chat, so never ask for them. Use it when the user asks to sign in to
        Vodafone, or agrees to after a tool failed with auth_expired. It writes only the local token
        file and changes nothing in the Vodafone account. The page works for 10 minutes; calling login
        again meanwhile returns the same link, without asking the user again."""
        # The second round of a URL elicitation sent as an InputRequiredResult (2026-07-28 and later):
        # the host showed the link, the user answered, and the host retried this call.
        if ctx.input_responses is not None and _ELICITATION in ctx.input_responses:
            attempt = signins.find(ctx.request_state)
            if attempt is None:
                return _text("That sign-in link is no longer valid. Call login again for a new one.")
            return await _answered(attempt, ctx.input_responses[_ELICITATION])

        attempt, created = signins.begin()
        if not created:  # already open, maybe mid-way: point at it again, never a second prompt
            return _still_waiting(attempt, link=True)
        if _offers_url_elicitation(ctx):
            try:
                answer = await ctx.elicit_url(_ELICIT_MESSAGE, attempt.url, attempt.id)
            except NoBackChannelError:  # 2026-07-28: no server-to-client requests; ask in the result
                return InputRequiredResult(
                    input_requests={
                        _ELICITATION: ElicitRequest(
                            params=ElicitRequestURLParams(message=_ELICIT_MESSAGE, url=attempt.url, elicitation_id=attempt.id)
                        )
                    },
                    request_state=attempt.id,
                )
            except Exception:
                pass  # the host refused or failed the elicitation: fall back to the browser
            else:
                result = await _answered(attempt, answer)
                if attempt.done.is_set():
                    try:
                        await ctx.session.send_elicit_complete(attempt.id)
                    except Exception:
                        pass
                return result

        opened = await anyio.to_thread.run_sync(open_browser or signin.open_browser, attempt.url)
        where = (
            "Opened the Vodafone sign-in page in the browser. If no page opened, open this link on this computer:"
            if opened
            else "Could not open a browser. Open this link on this computer to sign in:"
        )
        return _text(
            f"{where}\n{attempt.url}\n\nThe user signs in there with their usuario, password and SMS code, not in "
            f"this chat. The link works for about {max(1, attempt.seconds_left() // 60)} minutes, on this computer "
            "only. The Vodafone tools work as soon as the sign-in is done; auth_status shows when."
        )

    async def _answered(attempt: signin.SignIn, answer: object) -> CallToolResult:
        """The user's answer to the elicitation of a sign-in this login call created."""
        if getattr(answer, "action", None) != "accept":
            if attempt.bound:  # a browser is already on the page (it had the link): leave it be
                return _text(
                    "The prompt was dismissed, but the Vodafone sign-in page is already in use in the browser, "
                    "so it stays open: finish signing in there."
                )
            signins.cancel(attempt)
            return _text("Sign-in cancelled: the user did not open the Vodafone sign-in page. Nothing was saved.")
        return await wait_for(attempt)

    return server


def _still_waiting(attempt: signin.SignIn, *, link: bool = False) -> CallToolResult:
    where = f" If the page isn't open, open this link on this computer:\n{attempt.url}\n\n" if link else " "
    return _text(
        f"Still waiting: finish signing in to Vodafone in the browser.{where}The page stays open for about "
        f"{max(1, attempt.seconds_left() // 60)} more minutes. The Vodafone tools work as soon as the sign-in "
        "is done; auth_status shows when."
    )


def _signed_in(attempt: signin.SignIn) -> CallToolResult:
    result = attempt.result
    renews = f" and renews itself until {result.refresh_token_expires_at}" if result and result.refresh_token_expires_at else ""
    until = f" The session is valid until {result.access_token_expires_at}{renews}." if result else ""
    return _text(f"Signed in to Vodafone.{until} The Vodafone tools work now.")


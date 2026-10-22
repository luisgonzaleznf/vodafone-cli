"""`vodafone` CLI. Prints JSON to stdout; errors go to stderr as {"error": {...}} with the exit
codes 2 (usage), 3 (auth), 5 (blocked/upstream) and 7 (API changed).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import BaseModel

from . import api
from .client import DxlClient
from .config import TOKENS_FILE_ENV, default_tokens_path
from .errors import EXIT_AUTH, NotCapturedError, VodafoneError
from .output import render, render_error
from .tokens import FileTokenStore

app = typer.Typer(
    name="vodafone",
    help="Read your own Mi Vodafone (Vodafone España) account over the DXL API. Output is JSON.",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print frame locals, which include the bearer header and the NIF query param.
    pretty_exceptions_enable=False,
)
lines_app = typer.Typer(help="Lines (mobile, landline, fibre) and their tariffs.")
invoices_app = typer.Typer(help="Invoices.", no_args_is_help=True)
auth_app = typer.Typer(help="Token set status.", no_args_is_help=True)
app.add_typer(lines_app, name="lines")
app.add_typer(invoices_app, name="invoices")
app.add_typer(auth_app, name="auth")


@dataclass
class State:
    tokens_path: Path
    account: str | None


def _emit(payload: Any) -> None:
    typer.echo(render(payload))


def _fail(err: VodafoneError) -> NoReturn:
    typer.echo(render_error(err), err=True)
    raise typer.Exit(err.exit_code)


def _run(ctx: typer.Context, fn: Callable[[DxlClient], BaseModel]) -> None:
    state: State = ctx.obj
    try:
        result = api.run_with_client(state.tokens_path, fn)
    except VodafoneError as err:
        _fail(err)
    _emit(result.model_dump(mode="json"))


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


@app.callback()
def _root(
    ctx: typer.Context,
    tokens: Annotated[
        Path | None,
        typer.Option(help=f"Token set export (default: ${TOKENS_FILE_ENV}, else .local-secrets/tokens.json)."),
    ] = None,
    account: Annotated[
        str | None, typer.Option(help="Customer account id, when the login has more than one.")
    ] = None,
) -> None:
    ctx.obj = State(tokens_path=tokens or default_tokens_path(), account=account)


@app.command()
def me(ctx: typer.Context) -> None:
    """Customer account(s): status, segment, debt, flags, holder contact."""
    _run(ctx, api.get_me)


@lines_app.callback(invoke_without_command=True)
def _lines_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _run(ctx, lambda client: api.get_lines(client, ctx.obj.account))


@lines_app.command("list")
def lines_list(ctx: typer.Context) -> None:
    """List the account's lines with their tariff."""
    _run(ctx, lambda client: api.get_lines(client, ctx.obj.account))


@app.command()
def usage(
    ctx: typer.Context,
    line: Annotated[
        str | None, typer.Option(help="Line id (MSISDN or fibre service id) or its unique trailing digits.")
    ] = None,
) -> None:
    """In-plan allowances (data / voice / SMS) per line; all lines unless --line is given."""
    _run(ctx, lambda client: api.get_usage(client, ctx.obj.account, line))


@invoices_app.command("list")
def invoices_list(ctx: typer.Context) -> None:
    """Invoices with amount, dates and status (TMF678 customerBill)."""
    _run(ctx, lambda client: api.get_invoices(client, ctx.obj.account))


@invoices_app.command("download")
def invoices_download(
    target: Annotated[str, typer.Argument(help="Invoice id, 'latest' or 'all'.")] = "latest",
    dir: Annotated[Path, typer.Option(help="Output directory.")] = Path("."),
) -> None:
    """Not implemented yet: needs the bill's download-hash field captured."""
    _fail(
        NotCapturedError(
            "invoices download is deferred: the field carrying each bill's /v2/download/{hash} token "
            "has not been confirmed yet (see README, Limitations)"
        )
    )


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Token validity and refresh-token days remaining (offline; exit 3 if the access token expired)."""
    path: Path = ctx.obj.tokens_path
    try:
        tokens = FileTokenStore(path).load()
    except VodafoneError as err:
        _fail(err)

    now = datetime.now(UTC)
    access_left = (tokens.access_expires_at - now).total_seconds() if tokens.access_expires_at else None
    refresh_left = (tokens.refresh_expires_at - now).total_seconds() if tokens.refresh_expires_at else None
    exp = tokens.claims().get("exp")
    _emit(
        {
            "store": "file",
            "tokens_file": str(path),
            "exported_at": _iso(tokens.exported_at),
            "access_token": {
                "expires_at": _iso(tokens.access_expires_at),
                "expired": tokens.is_access_expired(now),
                "seconds_remaining": max(0, int(access_left)) if access_left is not None else None,
            },
            "refresh_token": {
                "present": tokens.refresh_token is not None,
                "expires_at": _iso(tokens.refresh_expires_at),
                "expired": tokens.is_refresh_expired(now),
                "days_remaining": round(max(0.0, refresh_left) / 86400, 1) if refresh_left is not None else None,
            },
            "jwt": {
                "present": tokens.jwt is not None,
                "expires_at": _iso(datetime.fromtimestamp(exp, UTC)) if isinstance(exp, int | float) else None,
            },
        }
    )
    if tokens.is_access_expired(now):
        raise typer.Exit(EXIT_AUTH)


@auth_app.command("refresh")
def auth_refresh() -> None:
    """Not implemented yet: needs the refresh-grant request captured from a login HAR."""
    _fail(
        NotCapturedError(
            "auth refresh is deferred: the grant_type=refresh_token request to "
            "/v1/apixoauth2password/oauth2/token has not been captured yet (see README, Limitations); "
            "until then re-export .local-secrets/tokens.json from a logged-in browser"
        )
    )


@app.command("mcp")
def mcp_command(ctx: typer.Context) -> None:
    """Run a read-only MCP server on stdio (tools: get_account, list_lines, get_usage, list_invoices)."""
    from .mcp_server import build_server  # deferred: the MCP SDK is only needed by this command

    build_server(ctx.obj.tokens_path, ctx.obj.account).run("stdio")


def main() -> None:
    app()

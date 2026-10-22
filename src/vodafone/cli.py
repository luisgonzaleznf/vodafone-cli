"""`vodafone` CLI. Prints JSON to stdout; errors go to stderr as {"error": {...}} with the exit
codes 2 (usage), 3 (auth), 5 (blocked/upstream) and 7 (API changed).
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import BaseModel

from . import api, auth
from .client import DxlClient
from .config import TOKENS_FILE_ENV, default_tokens_path
from .errors import EXIT_AUTH, VodafoneError
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
auth_app = typer.Typer(help="Sign in, refresh, and check the token set.", no_args_is_help=True)
app.add_typer(lines_app, name="lines")
app.add_typer(invoices_app, name="invoices")
app.add_typer(auth_app, name="auth")


@dataclass
class State:
    tokens_path: Path
    account: str | None


def _fail(err: VodafoneError) -> NoReturn:
    typer.echo(render_error(err), err=True)
    raise typer.Exit(err.exit_code)


def _emit(produce: Callable[[], Any]) -> Any:
    """Print `produce()` as JSON on stdout and return it. Every command goes through here, so any
    failure becomes the stderr error payload: a VodafoneError as is, anything else by its type only
    (messages can embed request URLs, headers or token values)."""
    try:
        payload = produce()
        text = render(payload)
    except (typer.Exit, typer.Abort):
        raise
    except VodafoneError as err:
        _fail(err)
    except Exception as exc:
        _fail(VodafoneError(f"unexpected internal error: {type(exc).__name__}"))
    typer.echo(text)
    return payload


def _run(ctx: typer.Context, fn: Callable[[DxlClient], BaseModel]) -> None:
    state: State = ctx.obj
    _emit(lambda: api.run_with_client(state.tokens_path, fn).model_dump(mode="json"))


_KEYS_UP = ("\x1b[A", "\x1bOA", "\xe0H", "k")  # arrow keys (ANSI, application mode, Windows), vi
_KEYS_DOWN = ("\x1b[B", "\x1bOB", "\xe0P", "j")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _choose(title: str, options: list[str]) -> int:
    """The index of the option picked. On a terminal, an arrow-key menu (↑/↓ or a digit, Enter to
    confirm); otherwise (pipes, tests) a numbered prompt."""
    if not _interactive():
        typer.echo(title, err=True)
        for number, option in enumerate(options, 1):
            typer.echo(f"  {number}. {option}", err=True)
        while not 1 <= (pick := typer.prompt("Línea", type=int, default=1, err=True)) <= len(options):
            typer.echo(f"Elige un número del 1 al {len(options)}.", err=True)
        return pick - 1
    typer.echo(f"{title} ↑/↓ y Enter", err=True)
    current = 0
    while True:
        for i, option in enumerate(options):
            row = typer.style(f"❯ {option}", fg="cyan", bold=True) if i == current else f"  {option}"
            typer.echo(f"\x1b[2K{row}", err=True)
        try:
            key = typer.getchar()  # Ctrl-C raises KeyboardInterrupt, which aborts as for any prompt
        except EOFError:
            raise typer.Abort() from None
        if key in ("\r", "\n"):
            return current
        if key in _KEYS_UP:
            current = (current - 1) % len(options)
        elif key in _KEYS_DOWN:
            current = (current + 1) % len(options)
        elif key.isdigit() and 1 <= int(key) <= len(options):
            current = int(key) - 1
        typer.echo(f"\x1b[{len(options)}A", err=True, nl=False)  # back up to redraw the menu


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
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="Invoice id (from `invoices list`), 'latest' or 'all'.")] = "latest",
    directory: Annotated[Path, typer.Option("--dir", help="Output directory (created if missing).")] = Path("."),
) -> None:
    """Save invoice PDFs as factura_YYYY-MM_<id>.pdf, one at a time. Existing files are skipped."""
    _run(ctx, lambda client: api.download_invoices(client, target, directory, ctx.obj.account))


@auth_app.command("status")
def auth_status(ctx: typer.Context) -> None:
    """Token validity and refresh-token days remaining (offline; exit 3 if the access token expired)."""
    payload = _emit(lambda: _token_status(ctx.obj.tokens_path, datetime.now(UTC)))
    if payload["access_token"]["expired"]:
        raise typer.Exit(EXIT_AUTH)


def _token_status(path: Path, now: datetime) -> dict[str, Any]:
    tokens = FileTokenStore(path).load()
    access_left = (tokens.access_expires_at - now).total_seconds() if tokens.access_expires_at else None
    refresh_left = (tokens.refresh_expires_at - now).total_seconds() if tokens.refresh_expires_at else None
    return {
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
        "jwt": {"present": tokens.jwt is not None, "expires_at": _iso(tokens.jwt_expires_at())},
    }


@auth_app.command("login")
def auth_login(ctx: typer.Context) -> None:
    """Sign in with usuario + password + SMS code, and save the token set (no browser)."""
    # Prompts go to stderr so stdout stays pure JSON.
    username = typer.prompt("Usuario (móvil o email)", err=True)
    password = typer.prompt("Contraseña", hide_input=True, err=True)

    def choose_line(lines: list[str]) -> str:
        # Masked numbers (e.g. 6*****789) from the grant's claims, validated as such: no server text.
        return lines[_choose("¿A qué línea enviamos el código SMS? (se envía al elegirla)", lines)]

    def request_code() -> str:
        typer.echo("Te hemos enviado un código por SMS.", err=True)  # fixed text: none from the server
        return typer.prompt("Código SMS", err=True)  # shown as typed: single-use, already on the phone

    store = FileTokenStore(ctx.obj.tokens_path)
    _emit(lambda: auth.login(store, username, password, request_code, choose_line).model_dump(mode="json"))


@auth_app.command("refresh")
def auth_refresh(ctx: typer.Context) -> None:
    """Exchange the refresh token for a new access token and save it to the token file."""
    _emit(lambda: auth.refresh(FileTokenStore(ctx.obj.tokens_path)).model_dump(mode="json"))


@app.command("mcp")
def mcp_command(ctx: typer.Context) -> None:
    """Run a read-only MCP server on stdio (tools: get_account, list_lines, get_usage, list_invoices)."""
    from .mcp_server import build_server  # deferred: the MCP SDK is only needed by this command

    build_server(ctx.obj.tokens_path, ctx.obj.account).run("stdio")


def main() -> None:
    app()

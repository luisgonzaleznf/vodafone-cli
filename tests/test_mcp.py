"""MCP server: in-process Client against build_server, plus one real stdio launch. Synthetic data only."""

import base64
import json
import logging
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import anyio
import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from typer.testing import CliRunner

from vodafone import api, cli
from vodafone.client import DxlClient
from vodafone.mcp_server import TOOL_NAMES, build_server
from vodafone.models import MeOutput

ACCOUNT = "100000001"
MOBILE = "600000001"
FIBRE = "J00000ANF000"
NIF = "00000000T"
SECRET = "S" * 28

ALLOWANCE = {
    "availableAllowance": "1234", "availableAllowanceUnitCode": "GB",
    "usedAllowance": "5.67", "usedAllowanceUnitCode": "GB",
    "remainingAllowance": "1228.33", "remainingAllowanceUnitCode": "GB",
    "unlimited": True,
}
ROUTES = {
    "/v2/customer/customerAccounts": {
        "items": [{"id": ACCOUNT, "status": "Activo", "type": "RESPONSIBLE", "debtAmount": 0,
                   "address": {"formattedAddress": "CALLE EJEMPLO 1, CIUDAD EJEMPLO"}}]
    },
    f"/v2/customer/customerAccounts/{ACCOUNT}": {
        "id": ACCOUNT, "firstName": "Ana", "familyName": "Ejemplo", "email": "ana@example.com", "msisdn": MOBILE,
    },
    f"/v2/customer/customerAccounts/{ACCOUNT}/billingAccounts": {"user": {"nif": NIF}, "items": [{"id": ACCOUNT}]},
    "/v2/product/tariffs": {"items": [{"code": "TAR01", "name": "Prueba Ficticia Demo", "status": "active"}]},
    "/v2/service/serviceBalances": {
        "items": [{"type": "data", "name": "Datos móviles", "group": "card_data", "allowance": ALLOWANCE}]
    },
    "/es/csm-api/customerBillAPI/v3/customerBill": [
        {
            "id": {"value": "F-1"},
            "extraId": [{"value": "123"}],
            "status": "settled",
            "details": {"chargeDateTime": {"value": "2001-02-03T00:00:00"}, "dueDateTime": {"value": "2001-02-20T00:00:00"}},
            "amountsDue": {"amount": {"value": "45.10", "currencyID": "EUR"}, "netAmount": {"value": "37.27"}},
        }
    ],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


@pytest.fixture
def tokens_file(tmp_path):
    services = [
        {"id": MOBILE, "code": "XXXX1", "type": "Mobile postpaid", "customerAccountId": ACCOUNT},
        {"id": FIBRE, "code": "XX001", "type": "Fibre", "customerAccountId": ACCOUNT},
    ]
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({
        "accessToken": SECRET,
        "userCsrfToken": "c" * 36,
        "accessTokenExpirationTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "jwt": [_jwt({"userProfile": {"services": services}})],
    }))
    return path


@pytest.fixture
def dxl(monkeypatch):
    """Route every DxlClient, CLI and MCP alike, to a mock DXL. Returns {path: (status, body)} overrides."""
    overrides: dict[str, tuple[int, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = overrides.get(request.url.path, (200, ROUTES[request.url.path]))
        return httpx.Response(status, json=body, headers={"content-type": "application/vnd.vodafone.a42+json"})

    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler)))
    return overrides


def _cli(tokens_file, *args):
    return CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), *args])


async def _call(tokens_file, tool, arguments=None, **server_kwargs):
    async with Client(build_server(tokens_file, **server_kwargs)) as client:
        return await client.call_tool(tool, arguments or {})


@pytest.mark.anyio
async def test_exposes_exactly_the_four_read_only_tools(tokens_file):
    async with Client(build_server(tokens_file)) as client:
        tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == set(TOOL_NAMES) == {"get_account", "list_lines", "get_usage", "list_invoices"}
    for tool in tools:
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.output_schema is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "cli_args"),
    [
        ("get_account", {}, ["me"]),
        ("list_lines", {}, ["lines"]),
        ("get_usage", {}, ["usage"]),
        ("get_usage", {"line": MOBILE[-4:]}, ["usage", "--line", MOBILE[-4:]]),
        ("list_invoices", {}, ["invoices", "list"]),
    ],
)
async def test_tool_returns_the_cli_json(tokens_file, dxl, tool, arguments, cli_args):
    expected = _cli(tokens_file, *cli_args)
    assert expected.exit_code == 0, expected.stderr

    result = await _call(tokens_file, tool, arguments)

    assert not result.is_error
    assert result.content[0].text == expected.stdout.rstrip("\n")
    assert result.structured_content == json.loads(expected.stdout)


@pytest.mark.anyio
async def test_tool_error_is_the_cli_error_payload(tokens_file, dxl):
    dxl["/es/csm-api/customerBillAPI/v3/customerBill"] = (
        500, {"code": 205, "reason": "INTERNAL_SERVER_ERROR", "message": f"sin datos para el cliente {ACCOUNT}", "status": 500},
    )
    expected = _cli(tokens_file, "invoices", "list")
    assert expected.exit_code == 5

    result = await _call(tokens_file, "list_invoices")

    assert result.is_error
    assert result.structured_content is None
    assert result.content[0].text == expected.stderr.strip()
    assert json.loads(result.content[0].text)["error"]["type"] == "upstream_unavailable"
    assert ACCOUNT not in result.content[0].text


@pytest.mark.anyio
async def test_unexpected_failure_is_sanitized(tokens_file, monkeypatch):
    def boom(client):
        raise RuntimeError(f"GET https://example.invalid/?nif={NIF} Bearer {SECRET}")

    monkeypatch.setattr(api, "get_me", boom)
    result = await _call(tokens_file, "get_account")

    assert result.is_error
    assert json.loads(result.content[0].text) == {
        "error": {"type": "internal", "message": "unexpected internal error: RuntimeError", "exit_code": 1}
    }
    assert SECRET not in result.content[0].text and NIF not in result.content[0].text


@pytest.mark.anyio
async def test_account_argument_overrides_the_server_default(tokens_file, dxl):
    wrong_default = await _call(tokens_file, "list_lines", default_account="999999999")
    assert json.loads(wrong_default.content[0].text)["error"]["type"] == "usage"

    explicit = await _call(tokens_file, "list_lines", {"account": ACCOUNT}, default_account="999999999")
    assert not explicit.is_error
    assert explicit.structured_content["account_id"] == ACCOUNT


@pytest.mark.anyio
async def test_parallel_tool_calls_run_one_at_a_time(tokens_file, monkeypatch):
    guard = threading.Lock()
    state = {"active": 0, "peak": 0}

    def slow_me(client):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with guard:
            state["active"] -= 1
        return MeOutput(accounts=[])

    monkeypatch.setattr(api, "get_me", slow_me)
    async with Client(build_server(tokens_file)) as client:
        async with anyio.create_task_group() as tg:
            for _ in range(3):
                tg.start_soon(client.call_tool, "get_account", {})

    assert state["peak"] == 1


def test_httpx_request_logging_is_capped(tokens_file):
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    build_server(tokens_file)

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


@pytest.mark.anyio
async def test_stdio_entry_point_serves_the_tools(tmp_path):
    # A missing token file keeps this offline: tools fail at token loading, before any request.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from vodafone.cli import main; main()", "--tokens", str(tmp_path / "missing.json"), "mcp"],
    )
    async with Client(params, read_timeout_seconds=30) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        result = await client.call_tool("get_account", {})

    assert names == set(TOOL_NAMES)
    assert result.is_error
    assert json.loads(result.content[0].text)["error"]["type"] == "auth_expired"

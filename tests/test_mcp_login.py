"""The MCP server's `login` and `auth_status` tools, and its not-signed-in errors: in-process
Clients against build_server, the sign-in page driven with httpx, a mocked Vodafone API. All values
are synthetic."""

import base64
import json
import re
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import anyio
import httpx
import pytest
from mcp import Client
from mcp.types import ElicitCompleteNotification, ElicitRequestURLParams, ElicitResult, ErrorData
from typer.testing import CliRunner

from vodafone import api, auth, cli, config, signin
from vodafone.client import DxlClient
from vodafone.mcp_server import INSTRUCTIONS, READ_TOOLS, build_server

USERNAME, PASSWORD, OTP_CODE = "600000001", "s3cr3t-pw", "123456"
FF_ACCESS, NEW_ACCESS, NEW_REFRESH = "F" * 28, "B" * 28, "s" * 32
OLD_ACCESS, OLD_REFRESH, CSRF = "A" * 28, "r" * 32, "c" * 36
LINE = "6*****001"
ACCOUNT = "100000001"
URL = re.compile(r"http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9_-]{43}/")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _jws(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def _token_body(access: str) -> dict:
    return {"access_token": access, "expires_at": "1893553445", "refresh_token": NEW_REFRESH, "refresh_token_expires": "1901329445"}


@pytest.fixture
def vodafone(monkeypatch):
    """The sign-in and refresh grants (auth.DxlClient) and the account data (api.DxlClient)."""
    state = {"refresh": (200, _token_body(NEW_ACCESS)), "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        path = request.url.path
        if path == config.TOKEN_PATH:
            if b"grant_type=password" in request.content:
                return httpx.Response(200, json={**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE]})})
            return httpx.Response(state["refresh"][0], json=state["refresh"][1])
        if path == config.OTP_AUTHORIZE_PATH:
            return httpx.Response(202, json={"message": "Te hemos enviado un SMS"})
        if path == config.OTP_TOKEN_PATH:
            return httpx.Response(200, json=_token_body(NEW_ACCESS))
        if path == "/v2/subscription/subscriptions/":
            return httpx.Response(200, json={}, headers={"authorization": "h.eyJ4IjoxfQ.s"})
        if path == "/v2/customer/customerAccounts":
            return httpx.Response(200, json={"items": [{"id": ACCOUNT, "status": "Activo"}]})
        if path == f"/v2/customer/customerAccounts/{ACCOUNT}":
            return httpx.Response(200, json={"id": ACCOUNT, "firstName": "Ana"})
        raise AssertionError(f"unexpected request {path}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(auth, "DxlClient", lambda tokens, **kw: DxlClient(tokens, spacing=0, transport=transport, **kw))
    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=transport))
    return state


@pytest.fixture
def tokens_path(tmp_path):
    return tmp_path / "config" / "tokens.json"


class Opener:
    def __init__(self, opens: bool = True):
        self.opens, self.urls = opens, []

    def __call__(self, url: str) -> bool:
        self.urls.append(url)
        return self.opens


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    monkeypatch.setattr(signin, "open_browser", lambda url: pytest.fail("a test opened a real browser"))


def _sign_in_in_the_browser(url: str, *, delay: float = 0.0) -> None:
    time.sleep(delay)
    origin = url.split("/", 3)
    base, path = "/".join(origin[:3]), "/" + origin[3]
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=10, trust_env=False) as browser:
        assert browser.get(path).status_code == 200
        browser.post(path + "start", data={"username": USERNAME, "password": PASSWORD})
        browser.post(path + "send", data={"line": LINE})
        assert "Signed in" in browser.post(path + "finish", data={"code": OTP_CODE}).text


def _in_background(url: str, delay: float = 0.1) -> threading.Thread:
    thread = threading.Thread(target=_sign_in_in_the_browser, args=(url,), kwargs={"delay": delay}, daemon=True)
    thread.start()
    return thread


def _error(result) -> dict:
    assert result.is_error
    return json.loads(result.content[0].text)["error"]


# --- login without URL elicitation: the browser, plus the link -------------------------------------


@pytest.mark.anyio
async def test_login_opens_the_browser_returns_the_link_and_the_tools_then_work(tokens_path, vodafone):
    opener = Opener()
    async with Client(build_server(tokens_path, open_browser=opener, login_wait=30)) as client:
        before = _error(await client.call_tool("get_account", {}))
        started = time.monotonic()
        result = await client.call_tool("login", {})
        elapsed = time.monotonic() - started

        text = result.content[0].text
        url = URL.search(text).group(0)
        assert not result.is_error and elapsed < 10  # promptly, not after login_wait
        assert opener.urls == [url] and "Opened the Vodafone sign-in page" in text
        assert (await client.call_tool("auth_status", {})).structured_content["signed_in"] is False

        await anyio.to_thread.run_sync(_sign_in_in_the_browser, url)

        account = await client.call_tool("get_account", {})
        status = (await client.call_tool("auth_status", {})).structured_content

    assert before["type"] == "auth_expired" and before["message"].startswith("Not signed in to Vodafone")
    assert not account.is_error and account.structured_content["accounts"][0]["id"] == ACCOUNT
    assert status["signed_in"] is True and status["reason"] is None
    assert json.loads(tokens_path.read_text())["accessToken"] == NEW_ACCESS
    for secret in (PASSWORD, OTP_CODE, NEW_ACCESS, FF_ACCESS):
        assert secret not in text


@pytest.mark.anyio
async def test_login_gives_the_link_when_no_browser_opens(tokens_path, vodafone):
    async with Client(build_server(tokens_path, open_browser=Opener(opens=False))) as client:
        result = await client.call_tool("login", {})

    assert not result.is_error
    assert "Could not open a browser" in result.content[0].text and URL.search(result.content[0].text)


@pytest.mark.anyio
async def test_login_again_returns_the_same_link_while_it_is_open(tokens_path, vodafone):
    opener = Opener()
    async with Client(build_server(tokens_path, open_browser=opener)) as client:
        await client.call_tool("login", {})
        again = (await client.call_tool("login", {})).content[0].text

    assert len(opener.urls) == 1  # no second browser window
    assert again.startswith("Still waiting") and opener.urls[0] in again


def test_the_default_opener_runs_webbrowser_in_a_child_with_the_url_on_stdin(monkeypatch):
    seen = {}

    class FakeChild:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.update(args=args, **kwargs)

        def communicate(self, data, timeout):
            seen["stdin"] = data

    monkeypatch.undo()  # the real open_browser, with a fake Popen
    monkeypatch.setattr(subprocess, "Popen", FakeChild)
    url = "http://127.0.0.1:1/" + "S" * 43 + "/"

    assert signin.open_browser(url) is True
    assert seen["stdin"] == url.encode() and not any(url in arg for arg in seen["args"])
    assert "webbrowser.open" in seen["args"][-1] and "-I" in seen["args"]
    assert seen["stdout"] is subprocess.DEVNULL and seen["stderr"] is subprocess.DEVNULL
    FakeChild.returncode = 1
    assert signin.open_browser(url) is False


# --- login with URL elicitation ----------------------------------------------------------------------


def _elicitation(action: str, *, sign_in: bool):
    seen = {"params": []}

    async def callback(context, params):
        seen["params"].append(params)
        if sign_in and action == "accept":
            seen["thread"] = _in_background(params.url)
        return ElicitResult(action=action)

    return seen, callback


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])  # 2025-11-25 elicitation/create; 2026-07-28 InputRequiredResult
async def test_login_uses_url_elicitation_when_the_client_offers_it(tokens_path, vodafone, mode):
    seen, callback = _elicitation("accept", sign_in=True)
    notifications = []

    async def on_message(message):
        notifications.append(message)

    opener = Opener()
    server = build_server(tokens_path, open_browser=opener, login_wait=10)
    async with Client(server, mode=mode, elicitation_callback=callback, message_handler=on_message) as client:
        result = await client.call_tool("login", {})
        account = await client.call_tool("get_account", {})

    params = seen["params"]
    assert len(params) == 1 and isinstance(params[0], ElicitRequestURLParams)
    assert URL.fullmatch(params[0].url) and "password" in params[0].message
    assert opener.urls == []  # the host opened the link
    assert not result.is_error and result.content[0].text.startswith("Signed in to Vodafone.")
    assert "2030-01-02T03:04:05.000Z" in result.content[0].text
    assert not account.is_error
    if mode == "legacy":
        assert params[0].elicitation_id and params[0].elicitation_id not in params[0].url
        assert any(isinstance(n, ElicitCompleteNotification) and n.params.elicitation_id == params[0].elicitation_id for n in notifications)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_a_declined_elicitation_cancels_the_sign_in(tokens_path, vodafone, mode, action):
    seen, callback = _elicitation(action, sign_in=False)
    async with Client(build_server(tokens_path, open_browser=Opener()), mode=mode, elicitation_callback=callback) as client:
        result = await client.call_tool("login", {})

    assert "Sign-in cancelled" in result.content[0].text
    base = seen["params"][0].url.split("/", 3)
    with httpx.Client(base_url="/".join(base[:3]), trust_env=False) as browser:
        assert browser.get("/" + base[3]).status_code == 410  # the link is dead
    assert not tokens_path.exists() and vodafone["requests"] == []


def _browser_for(url: str) -> tuple[httpx.Client, str]:
    origin = url.split("/", 3)
    base = "/".join(origin[:3])
    return httpx.Client(base_url=base, headers={"Origin": base}, timeout=10, trust_env=False), "/" + origin[3]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("second_answer", ["decline", "cancel", "accept"])
async def test_login_again_mid_sign_in_neither_prompts_nor_breaks_the_page(tokens_path, vodafone, mode, second_answer):
    answers, prompts = ["accept", second_answer], []

    async def callback(context, params):
        prompts.append(params)
        return ElicitResult(action=answers.pop(0))

    async with Client(build_server(tokens_path, login_wait=0.2), mode=mode, elicitation_callback=callback) as client:
        assert (await client.call_tool("login", {})).content[0].text.startswith("Still waiting")
        url = prompts[0].url
        browser, path = _browser_for(url)
        with browser:
            assert browser.post(path + "start", data={"username": USERNAME, "password": PASSWORD}).status_code == 200

            again = (await client.call_tool("login", {})).content[0].text

            assert len(prompts) == 1  # not asked a second time, so a dismissal can't end the page
            assert again.startswith("Still waiting") and url in again
            assert browser.post(path + "send", data={"line": LINE}).status_code == 200
            assert "Signed in" in browser.post(path + "finish", data={"code": OTP_CODE}).text
        account = await client.call_tool("get_account", {})

    assert not account.is_error


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_a_dismissed_prompt_leaves_a_page_a_browser_already_uses(tokens_path, vodafone, mode):
    tab = {}

    async def opens_the_page_then_dismisses(context, params):
        # The user already has the page open (from an earlier link) and got past page 1.
        tab["browser"], tab["path"] = _browser_for(params.url)
        start = lambda: tab["browser"].post(tab["path"] + "start", data={"username": USERNAME, "password": PASSWORD})
        assert (await anyio.to_thread.run_sync(start)).status_code == 200
        return ElicitResult(action="decline")

    async with Client(build_server(tokens_path), mode=mode, elicitation_callback=opens_the_page_then_dismisses) as client:
        result = (await client.call_tool("login", {})).content[0].text
        browser, path = tab["browser"], tab["path"]  # the same tab, holding the page's cookie
        try:
            assert browser.post(path + "send", data={"line": LINE}).status_code == 200
            assert "Signed in" in browser.post(path + "finish", data={"code": OTP_CODE}).text
        finally:
            browser.close()
        account = await client.call_tool("get_account", {})

    assert "stays open" in result and not account.is_error


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_login_reports_still_waiting_and_the_page_stays_usable(tokens_path, vodafone, mode):
    seen, callback = _elicitation("accept", sign_in=False)
    async with Client(build_server(tokens_path, login_wait=0.3), mode=mode, elicitation_callback=callback) as client:
        waiting = await client.call_tool("login", {})
        await anyio.to_thread.run_sync(_sign_in_in_the_browser, seen["params"][0].url)
        account = await client.call_tool("get_account", {})

    assert waiting.content[0].text.startswith("Still waiting: finish signing in to Vodafone in the browser")
    assert not account.is_error


@pytest.mark.anyio
async def test_a_failed_elicitation_falls_back_to_the_browser(tokens_path, vodafone):
    async def refuses(context, params):
        return ErrorData(code=-32600, message="not now")

    opener = Opener()
    async with Client(build_server(tokens_path, open_browser=opener), mode="legacy", elicitation_callback=refuses) as client:
        result = await client.call_tool("login", {})

    assert len(opener.urls) == 1 and opener.urls[0] in result.content[0].text


# --- auth_status ---------------------------------------------------------------------------------


def _write(path, *, access_in, refresh=OLD_REFRESH, refresh_in=timedelta(days=60)):
    now = datetime.now(UTC)
    raw = {"accessToken": OLD_ACCESS, "userCsrfToken": CSRF, "accessTokenExpirationTime": (now + access_in).isoformat()}
    if refresh:
        raw |= {"refreshToken": refresh, "refreshTokenExpirationTime": (now + refresh_in).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("access_in", "refresh", "refresh_in", "signed_in", "reason"),
    [
        (timedelta(hours=1), OLD_REFRESH, timedelta(days=60), True, None),
        (timedelta(hours=-1), OLD_REFRESH, timedelta(days=60), True, None),  # renewable: the next read refreshes
        (timedelta(hours=1), None, None, True, None),
        (timedelta(hours=-1), None, None, False, "the access token expired and there is no refresh token to renew it"),
        (timedelta(hours=-1), OLD_REFRESH, timedelta(days=-1), False, "the access token and the refresh token have expired"),
    ],
)
async def test_auth_status_says_whether_reads_can_run_offline(tokens_path, vodafone, access_in, refresh, refresh_in, signed_in, reason):
    _write(tokens_path, access_in=access_in, refresh=refresh, refresh_in=refresh_in)
    before = tokens_path.read_bytes()

    async with Client(build_server(tokens_path)) as client:
        result = await client.call_tool("auth_status", {})
    cli_status = json.loads(CliRunner().invoke(cli.app, ["--tokens", str(tokens_path), "auth", "status"]).stdout)

    status = result.structured_content
    assert not result.is_error and json.loads(result.content[0].text) == status
    assert (status["signed_in"], status["reason"]) == (signed_in, reason)
    assert {k: v for k, v in status.items() if k not in ("signed_in", "reason")} == {
        **cli_status, "access_token": {**cli_status["access_token"], "seconds_remaining": status["access_token"]["seconds_remaining"]}
    }
    assert vodafone["requests"] == [] and tokens_path.read_bytes() == before  # offline, never refreshes
    for secret in (OLD_ACCESS, OLD_REFRESH, CSRF):
        assert secret not in result.content[0].text


@pytest.mark.anyio
async def test_auth_status_without_a_token_file_is_not_signed_in_not_an_error(tokens_path, vodafone):
    async with Client(build_server(tokens_path)) as client:
        result = await client.call_tool("auth_status", {})

    assert not result.is_error
    assert result.structured_content == {
        "signed_in": False, "reason": "there is no token file", "store": "file", "tokens_file": str(tokens_path),
        "exported_at": None, "access_token": None, "refresh_token": None, "jwt": None,
    }


# --- not signed in ---------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("tool", READ_TOOLS)
async def test_read_tools_without_a_token_file_say_not_signed_in(tokens_path, vodafone, tool):
    async with Client(build_server(tokens_path)) as client:
        error = _error(await client.call_tool(tool, {}))

    assert error["type"] == "auth_expired" and error["exit_code"] == 3
    assert error["message"] == (
        "Not signed in to Vodafone: there is no token file. Ask to sign in to Vodafone: the login tool opens a "
        "sign-in page in the browser (in a terminal, `vodafone auth login`)."
    )
    assert vodafone["requests"] == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("refresh", "refresh_in", "grant", "reason"),
    [
        (None, None, None, "there is no refresh token to renew it"),
        (OLD_REFRESH, timedelta(days=-1), None, "and the refresh token at"),
        (OLD_REFRESH, timedelta(days=60), (400, {"error": "invalid_grant", "error_description": OLD_REFRESH}), "Vodafone refused to renew the session"),
    ],
)
async def test_an_expired_session_that_cannot_be_renewed_says_not_signed_in(tokens_path, vodafone, refresh, refresh_in, grant, reason):
    _write(tokens_path, access_in=timedelta(hours=-1), refresh=refresh, refresh_in=refresh_in)
    if grant:
        vodafone["refresh"] = grant
    before = tokens_path.read_bytes()

    async with Client(build_server(tokens_path)) as client:
        error = _error(await client.call_tool("get_account", {}))

    assert error["type"] == "auth_expired"
    assert error["message"].startswith("Not signed in to Vodafone: ") and reason in error["message"]
    assert "login tool" in error["message"] and OLD_REFRESH not in error["message"]
    assert tokens_path.read_bytes() == before
    assert len(vodafone["requests"]) == (1 if grant else 0)


@pytest.mark.anyio
async def test_vodafone_down_during_a_refresh_is_not_reported_as_signed_out(tokens_path, vodafone):
    _write(tokens_path, access_in=timedelta(hours=-1))
    vodafone["refresh"] = (503, {"message": "down"})
    before = tokens_path.read_bytes()

    async with Client(build_server(tokens_path)) as client:
        error = _error(await client.call_tool("get_account", {}))

    assert error["type"] == "upstream_unavailable" and error["exit_code"] == 5
    assert "Not signed in" not in error["message"] and "login" not in error["message"]
    assert tokens_path.read_bytes() == before


@pytest.mark.anyio
async def test_an_expired_session_is_renewed_before_a_tool_call(tokens_path, vodafone):
    _write(tokens_path, access_in=timedelta(hours=-1))

    async with Client(build_server(tokens_path)) as client:
        result = await client.call_tool("get_account", {})

    assert not result.is_error
    assert [r.url.path for r in vodafone["requests"]][0] == config.TOKEN_PATH
    assert json.loads(tokens_path.read_text())["accessToken"] == NEW_ACCESS


def test_the_instructions_point_at_the_login_tool():
    assert "`login` tool" in INSTRUCTIONS and "never ask for either in the chat" in INSTRUCTIONS
    assert "auth_status" in INSTRUCTIONS and "auth_expired" in INSTRUCTIONS

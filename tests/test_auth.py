"""`auth login` and `auth refresh` against a mock token endpoint. All values are synthetic."""

import base64
import contextlib
import json
import stat
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from typer.testing import CliRunner

from vodafone import auth, cli, config
from vodafone.client import DxlClient
from vodafone.errors import ApiShapeError, AuthError
from vodafone.tokens import FileTokenStore

USERNAME = "600000001"
PASSWORD = "s3cr3t-pw"
OTP_CODE = "123456"
FF_ACCESS = "F" * 28  # first-factor access token (password grant)
NEW_ACCESS, NEW_REFRESH = "B" * 28, "s" * 32
OLD_ACCESS, OLD_REFRESH = "A" * 28, "r" * 32
CSRF = "c" * 36
ACCESS_EXP = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
REFRESH_EXP = datetime(2030, 4, 2, 3, 4, 5, tzinfo=UTC)


def _jwt(marker: str) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc({'marker': marker})}.sig"


OLD_JWT, NEW_JWT = _jwt("old"), _jwt("new")
LINE, LINE_2 = "6*****001", "7*****002"  # masked numbers, as the password grant's jws lists them


def _jws(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc({'iss': 'VodafoneDXL', 'mailList': [], **claims})}.sig"


def _token_body(access: str, *, refresh: str | None = NEW_REFRESH) -> dict:
    body = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": "86400",
        "expires_at": str(int(ACCESS_EXP.timestamp())),
    }
    if refresh:
        body["refresh_token"] = refresh
        body["refresh_token_expires"] = str(int(REFRESH_EXP.timestamp()))
    return body


# --- login ---------------------------------------------------------------------------------------


@pytest.fixture
def login_endpoint(monkeypatch):
    """Mock the full login flow. Tweak state to change responses; inspect captured requests."""
    state = {
        "password_grant": (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE]})}),
        "authorize": (202, {"message": "Te hemos enviado un SMS al 6XX XXX X01"}),
        "otp_token": (200, _token_body(NEW_ACCESS)),
        "jwt": httpx.Response(200, json={}, headers={"authorization": NEW_JWT}),
        # Cookies the real flow sets: Imperva's on the grant, the OTP session on the challenge.
        "set_cookie": {"password_grant": "visid_incap_1=v1; Path=/", "authorize": "JSESSIONID=sess-1; Path=/"},
        "requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        route = {
            config.TOKEN_PATH: "password_grant",
            config.OTP_AUTHORIZE_PATH: "authorize",
            config.OTP_TOKEN_PATH: "otp_token",
        }.get(request.url.path)
        if route:
            reply = state[route](request) if callable(state[route]) else state[route]
            if isinstance(reply, httpx.Response):  # a raw reply, e.g. a non-JSON body
                return reply
            status, body = reply
            cookie = state["set_cookie"].get(route)
            return httpx.Response(status, json=body, headers={"set-cookie": cookie} if cookie else None)
        if request.url.path == "/v2/subscription/subscriptions/":
            return state["jwt"]
        raise AssertionError(f"unexpected request {request.url.path}")

    monkeypatch.setattr(
        auth, "DxlClient", lambda tokens, **kw: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler), **kw)
    )
    return state


def _store(tmp_path):
    return FileTokenStore(tmp_path / "sub" / "tokens.json")  # nonexistent dir: login must create it


def test_login_runs_password_grant_then_otp_then_jwt(login_endpoint, tmp_path):
    out = auth.login(_store(tmp_path), USERNAME, PASSWORD, lambda: OTP_CODE)

    reqs = login_endpoint["requests"]
    paths = [r.url.path for r in reqs]
    assert paths == [config.TOKEN_PATH, config.OTP_AUTHORIZE_PATH, config.OTP_TOKEN_PATH, "/v2/subscription/subscriptions/"]

    grant = reqs[0]
    assert dict(parse_qsl(grant.content.decode())) == {
        "username": USERNAME, "password": PASSWORD, "client_id": config.OAUTH_CLIENT_ID,
        "scope": config.OAUTH_SCOPE, "grant_type": "password",
    }
    assert grant.headers["recaptcha_enterprise"] == "true"
    assert "authorization" not in grant.headers  # password grant carries no bearer
    csrf = grant.headers["vf-csrf-token"]
    assert len(csrf) == 36 and csrf.count("-") == 4  # client-minted UUID
    assert all(r.headers.get("vf-csrf-token") == csrf for r in reqs)  # same csrf throughout
    # Each POST pairs a CSRF cookie with the header, after the cookies set so far: the OTP token step
    # must carry the challenge's JSESSIONID. The identity GET gets the jar but no CSRF cookie.
    assert [r.headers.get("cookie") for r in reqs] == [
        f"csrf-token={csrf}",
        f"visid_incap_1=v1; csrf-token={csrf}",
        f"visid_incap_1=v1; JSESSIONID=sess-1; csrf-token={csrf}",
        "visid_incap_1=v1; JSESSIONID=sess-1",
    ]

    authorize = reqs[1]
    assert json.loads(authorize.content) == {"response_type": "code", "login_hint": LINE}
    assert authorize.headers["otpversion"] == config.OTP_VERSION
    assert authorize.headers["authorization"] == f"Bearer {FF_ACCESS}"  # first-factor bearer

    otp = reqs[2]
    assert json.loads(otp.content) == {"grant_type": "authorization_code", "code": OTP_CODE}
    assert otp.headers["authorization"] == f"Bearer {FF_ACCESS}"
    assert reqs[3].headers["authorization"] == f"Bearer {NEW_ACCESS}"  # JWT fetched with the final token

    assert out.jwt_fetched is True
    assert out.access_token_expires_at == "2030-01-02T03:04:05.000Z"


def test_login_lets_the_user_choose_among_several_masked_lines(login_endpoint, tmp_path):
    login_endpoint["password_grant"] = (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE, LINE_2]})})
    offered = []

    auth.login(_store(tmp_path), USERNAME, PASSWORD, lambda: OTP_CODE, lambda lines: offered.append(lines) or LINE_2)

    assert offered == [[LINE, LINE_2]]
    assert json.loads(login_endpoint["requests"][1].content)["login_hint"] == LINE_2


@pytest.mark.parametrize("jws", [None, "not-a-jwt", _jws({}), _jws({"msisdnList": [USERNAME, "6*****"]})])
def test_login_stops_before_the_sms_without_a_masked_line(login_endpoint, tmp_path, jws):
    body = _token_body(FF_ACCESS)
    if jws:
        body["jws"] = jws
    login_endpoint["password_grant"] = (200, body)
    store = _store(tmp_path)

    with pytest.raises(ApiShapeError, match="no phone number"):
        auth.login(store, USERNAME, PASSWORD, lambda: pytest.fail("no SMS was sent"))

    assert [r.url.path for r in login_endpoint["requests"]] == [config.TOKEN_PATH]
    assert not store.path.exists()


def test_login_writes_a_fresh_token_file_the_cli_can_load(login_endpoint, tmp_path):
    store = _store(tmp_path)
    out = auth.login(store, USERNAME, PASSWORD, lambda: OTP_CODE)

    raw = json.loads(store.path.read_text())
    assert raw == {
        "accessToken": NEW_ACCESS,
        "accessTokenExpirationTime": "2030-01-02T03:04:05.000Z",
        "userCsrfToken": raw["userCsrfToken"],
        "exportedAt": raw["exportedAt"],
        "refreshToken": NEW_REFRESH,
        "refreshTokenExpirationTime": "2030-04-02T03:04:05.000Z",
        "jwt": [NEW_JWT],
    }
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    loaded = store.load()
    assert loaded.access_token == NEW_ACCESS and loaded.jwt == NEW_JWT
    assert out.tokens_file == str(store.path)


MASK_SHAPED_PASSWORD = "XXxx1234"  # shaped like a masked phone number, e.g. "6XX XXX X01"


@pytest.mark.parametrize("password", [PASSWORD, MASK_SHAPED_PASSWORD])
def test_no_text_from_the_otp_challenge_is_shown(login_endpoint, tmp_path, password):
    echoed = f"Te hemos enviado un SMS al 6XX XXX X01 {password} {USERNAME} {FF_ACCESS}"
    login_endpoint["authorize"] = (202, {"message": echoed, "destination": password, "msisdn": password})
    store = _store(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["--tokens", str(store.path), "auth", "login"], input=f"{USERNAME}\n{password}\n{OTP_CODE}\n"
    )

    assert result.exit_code == 0, result.output
    assert "Te hemos enviado un código por SMS." in result.stderr
    for secret in (password, "XXxx", "6XX XXX X01", FF_ACCESS):
        assert secret not in result.output
    assert USERNAME not in result.stderr  # stdout only carries CliRunner's echo of the typed username


def test_login_writes_nothing_when_the_password_grant_is_refused(login_endpoint, tmp_path):
    login_endpoint["password_grant"] = (401, {"error": "invalid_grant", "error_description": "Bad credentials"})
    store = _store(tmp_path)

    with pytest.raises(AuthError, match="invalid_grant") as exc:
        auth.login(store, USERNAME, PASSWORD, lambda: OTP_CODE)
    assert "Bad credentials" not in exc.value.message  # free text is never echoed, only the code
    assert not store.path.exists()
    assert [r.url.path for r in login_endpoint["requests"]] == [config.TOKEN_PATH]  # stopped before OTP


def test_login_writes_nothing_when_the_otp_code_is_rejected(login_endpoint, tmp_path):
    login_endpoint["otp_token"] = (401, {"error": "invalid_grant", "error_description": "Invalid code"})
    store = _store(tmp_path)

    with pytest.raises(AuthError, match="invalid_grant"):
        auth.login(store, USERNAME, PASSWORD, lambda: "000000")
    assert not store.path.exists()


def _echo_everything(status: int):
    """An error body whose every field reflects what the request carried."""

    def respond(request: httpx.Request) -> tuple[int, dict]:
        sent = f"{request.content.decode()} {request.headers.get('authorization', '')} {request.headers['vf-csrf-token']}"
        return status, {
            "error": f"bad {sent}",
            "error_description": sent,
            "description": sent,
            "message": sent,
            "reason": sent,
            "transactionId": request.headers["vf-csrf-token"],
        }

    return respond


@pytest.mark.parametrize("route", ["password_grant", "authorize", "otp_token"])
@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_auth_errors_never_echo_credentials_or_tokens(login_endpoint, tmp_path, route, status):
    login_endpoint[route] = _echo_everything(status)
    store = _store(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["--tokens", str(store.path), "auth", "login"], input=f"{USERNAME}\n{PASSWORD}\n{OTP_CODE}\n"
    )

    assert result.exit_code == (5 if status == 500 else 3)
    error = json.loads(result.stderr[result.stderr.index("{"):])["error"]
    failing = login_endpoint["requests"][-1]
    assert error["transaction_id"] == failing.headers["vf-trace-transaction-id"]  # ours, not the echoed csrf
    csrf = login_endpoint["requests"][0].headers["vf-csrf-token"]
    for secret in (USERNAME, PASSWORD, OTP_CODE, FF_ACCESS, csrf, "username=", "password="):
        assert secret not in result.stderr
    for secret in (PASSWORD, FF_ACCESS, csrf):  # stdout only carries the echoed username and SMS code
        assert secret not in result.output
    assert not store.path.exists()


@pytest.mark.parametrize("server_tid", ["a1b2c3d4-0000-4000-8000-00000000000a", USERNAME, PASSWORD, "t-1"])
def test_errors_report_the_requests_own_trace_id_never_the_servers(login_endpoint, tmp_path, server_tid):
    login_endpoint["password_grant"] = (400, {"error": "invalid_grant", "transactionId": server_tid})

    with pytest.raises(AuthError) as exc:
        auth.login(_store(tmp_path), USERNAME, PASSWORD, lambda: OTP_CODE)
    assert exc.value.transaction_id == login_endpoint["requests"][0].headers["vf-trace-transaction-id"]
    assert server_tid not in str(exc.value.to_dict())
    assert exc.value.message == f"{config.TOKEN_PATH} refused the request (HTTP 400: invalid_grant (credentials, code or refresh token rejected))"


@pytest.mark.parametrize("route", ["password_grant", "authorize", "otp_token"])
@pytest.mark.parametrize("status", [200, 400, 500])
def test_a_non_json_auth_response_never_echoes_its_headers(login_endpoint, tmp_path, route, status):
    def respond(request: httpx.Request) -> httpx.Response:
        sent = f"{request.content.decode()} {request.headers.get('authorization', '')}"
        return httpx.Response(
            status,
            content=f"not json: {sent}".encode(),
            headers={"content-type": f"text/plain; detail={sent.replace(' ', '_')}", "x-echo": sent.replace(" ", "_")},
        )

    login_endpoint[route] = respond
    store = _store(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["--tokens", str(store.path), "auth", "login"], input=f"{USERNAME}\n{PASSWORD}\n{OTP_CODE}\n"
    )

    assert result.exit_code == {200: 7, 400: 3, 500: 5}[status], result.output
    error = json.loads(result.stderr[result.stderr.index("{"):])["error"]
    if status == 200:
        assert "non-JSON body (plain text)" in error["message"]
    for secret in (USERNAME, PASSWORD, OTP_CODE, FF_ACCESS, "detail="):
        assert secret not in result.stderr
    assert not store.path.exists()


def _valid_store(tmp_path) -> FileTokenStore:
    store = FileTokenStore(tmp_path / "tokens.json")
    store.path.write_text(json.dumps({"accessToken": OLD_ACCESS, "userCsrfToken": CSRF, "refreshToken": OLD_REFRESH}))
    return store


@pytest.mark.parametrize("bad", ["", "   ", "a b", "tok\r\nx-injected: 1", "tok\u00e9n", None, 42])
@pytest.mark.parametrize("route", ["password_grant", "otp_token"])
def test_a_blank_or_unsafe_access_token_is_rejected_without_writing(login_endpoint, tmp_path, route, bad):
    login_endpoint[route] = (200, {**_token_body(NEW_ACCESS), "access_token": bad})
    store = _valid_store(tmp_path)
    before = store.path.read_bytes()

    with pytest.raises(ApiShapeError, match="access_token"):
        auth.login(store, USERNAME, PASSWORD, lambda: OTP_CODE)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["expires_at", "refresh_token_expires"])
def test_an_out_of_range_expiry_is_rejected_without_writing(login_endpoint, tmp_path, field):
    login_endpoint["otp_token"] = (200, {**_token_body(NEW_ACCESS), field: "999999999999"})
    store = _valid_store(tmp_path)
    before = store.path.read_bytes()

    with pytest.raises(ApiShapeError, match=field):
        auth.login(store, USERNAME, PASSWORD, lambda: OTP_CODE)
    assert store.path.read_bytes() == before


def test_login_still_succeeds_when_the_jwt_fetch_fails(login_endpoint, tmp_path):
    login_endpoint["jwt"] = httpx.Response(500, json={"message": "boom"})
    store = _store(tmp_path)

    out = auth.login(store, USERNAME, PASSWORD, lambda: OTP_CODE)

    raw = json.loads(store.path.read_text())
    assert raw["accessToken"] == NEW_ACCESS and "jwt" not in raw
    assert out.jwt_fetched is False


def test_login_cli_lists_the_masked_lines_and_asks_which(login_endpoint, tmp_path):
    login_endpoint["password_grant"] = (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE, LINE_2]})})
    store = _store(tmp_path)
    result = CliRunner().invoke(
        cli.app, ["--tokens", str(store.path), "auth", "login"], input=f"{USERNAME}\n{PASSWORD}\n3\n2\n{OTP_CODE}\n"
    )

    assert result.exit_code == 0, result.output
    assert f"1. {LINE}" in result.stderr and f"2. {LINE_2}" in result.stderr
    assert "Elige un número del 1 al 2." in result.stderr  # 3 is out of range: asked again
    assert json.loads(login_endpoint["requests"][1].content)["login_hint"] == LINE_2


def test_login_cli_prompts_and_hides_secrets(login_endpoint, tmp_path):
    store = _store(tmp_path)
    result = CliRunner().invoke(
        cli.app, ["--tokens", str(store.path), "auth", "login"], input=f"{USERNAME}\n{PASSWORD}\n{OTP_CODE}\n"
    )

    assert result.exit_code == 0, result.output
    # CliRunner echoes piped stdin onto stdout, so the JSON follows the echoed prompts; a real
    # (piped) run gets pure JSON. Parse from the first object brace.
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload["jwt_fetched"] is True
    assert "Te hemos enviado un código por SMS." in result.stderr
    assert "6XX XXX X01" not in result.output  # no text from the server's challenge
    # The password is hidden (never echoed) and no token value is ever printed. The single-use SMS
    # code is shown as typed: CliRunner's echo of the inputs is the username and the code only.
    assert result.stdout[: result.stdout.index("{")].split() == [USERNAME, OTP_CODE]
    csrf = login_endpoint["requests"][0].headers["vf-csrf-token"]
    for secret in (PASSWORD, FF_ACCESS, NEW_ACCESS, NEW_REFRESH, NEW_JWT, csrf):
        assert secret not in result.output


@pytest.mark.parametrize(
    ("keys", "picked"),
    [
        (["\r"], 0),  # Enter keeps the first line
        (["\x1b[B", "\r"], 1),  # ↓
        (["\x1b[A", "\r"], 2),  # ↑ from the first wraps to the last
        (["3", "\x1b[A", "\n"], 1),  # a digit jumps, then ↑
        (["x", "9", "\x1bOB", "\r"], 1),  # unknown keys and out-of-range digits are ignored
    ],
)
def test_the_line_menu_follows_arrow_keys_and_digits_on_a_terminal(monkeypatch, keys, picked):
    pressed = iter(keys)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli.typer, "getchar", lambda: next(pressed))

    assert cli._choose("¿A qué línea?", [LINE, LINE_2, "6*****003"]) == picked
    assert next(pressed, None) is None  # Enter ended the menu: no key left unread


# --- step-wise login (LoginFlow) ------------------------------------------------------------------


def test_the_flow_runs_one_request_per_step_and_writes_only_at_the_end(login_endpoint, tmp_path):
    login_endpoint["password_grant"] = (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE, LINE_2]})})
    store = _store(tmp_path)
    flow = auth.LoginFlow(store)
    paths = lambda: [r.url.path for r in login_endpoint["requests"]]

    assert flow.start(USERNAME, PASSWORD) == [LINE, LINE_2]
    assert paths() == [config.TOKEN_PATH] and flow.lines == [LINE, LINE_2] and not flow.code_sent
    flow.send_code(LINE_2)
    assert paths()[1:] == [config.OTP_AUTHORIZE_PATH] and flow.code_sent
    assert not store.path.exists()

    out = flow.finish(f"  {OTP_CODE}\n")

    assert paths()[2:] == [config.OTP_TOKEN_PATH, "/v2/subscription/subscriptions/"]
    reqs = login_endpoint["requests"]
    assert json.loads(reqs[1].content)["login_hint"] == LINE_2
    assert json.loads(reqs[2].content)["code"] == OTP_CODE
    assert reqs[2].headers["cookie"].startswith("visid_incap_1=v1; JSESSIONID=sess-1; ")  # jar carried across steps
    assert store.load().access_token == NEW_ACCESS and out.jwt_fetched


def test_the_flow_keeps_no_credential_after_the_password_grant(login_endpoint, tmp_path):
    flow = auth.LoginFlow(_store(tmp_path))
    flow.start(USERNAME, PASSWORD)

    assert PASSWORD not in repr(vars(flow)) and USERNAME not in repr(vars(flow))


@pytest.mark.parametrize("line", [USERNAME, "6*****003", "", LINE + " "])
def test_the_flow_only_sends_the_code_to_an_offered_masked_line(login_endpoint, tmp_path, line):
    flow = auth.LoginFlow(_store(tmp_path))
    flow.start(USERNAME, PASSWORD)

    with pytest.raises(AuthError, match="no line chosen"):
        flow.send_code(line)
    assert [r.url.path for r in login_endpoint["requests"]] == [config.TOKEN_PATH]  # no SMS sent
    assert not flow.code_sent


def test_the_flow_refuses_steps_out_of_order(login_endpoint, tmp_path):
    store = _store(tmp_path)
    flow = auth.LoginFlow(store)

    with pytest.raises(AuthError, match="usuario and password first"):
        flow.send_code(LINE)
    with pytest.raises(AuthError, match="no SMS code was sent"):
        flow.finish(OTP_CODE)
    flow.start(USERNAME, PASSWORD)
    with pytest.raises(AuthError, match="no SMS code was sent"):
        flow.finish(OTP_CODE)
    flow.send_code(LINE)
    with pytest.raises(AuthError, match="no SMS code entered"):
        flow.finish("   ")

    assert [r.url.path for r in login_endpoint["requests"]] == [config.TOKEN_PATH, config.OTP_AUTHORIZE_PATH]
    assert not store.path.exists()


def test_a_rejected_code_can_be_retried_and_a_finished_flow_is_spent(login_endpoint, tmp_path):
    replies = iter([(401, {"error": "invalid_grant", "error_description": f"bad code 000000 {PASSWORD}"}), (200, _token_body(NEW_ACCESS))])
    login_endpoint["otp_token"] = lambda request: next(replies)
    store = _store(tmp_path)
    flow = auth.LoginFlow(store)
    flow.start(USERNAME, PASSWORD)
    flow.send_code(LINE)

    with pytest.raises(AuthError, match="invalid_grant") as exc:
        flow.finish("000000")
    assert "000000" not in exc.value.message and PASSWORD not in exc.value.message
    assert not store.path.exists()

    flow.finish(OTP_CODE)
    assert store.load().access_token == NEW_ACCESS
    sent = len(login_endpoint["requests"])
    with pytest.raises(AuthError):
        flow.finish(OTP_CODE)
    assert len(login_endpoint["requests"]) == sent


def test_the_token_file_is_written_while_holding_its_lock(login_endpoint, tmp_path, monkeypatch):
    events = []
    real_lock, real_write = FileTokenStore.lock, FileTokenStore.write

    @contextlib.contextmanager
    def spy_lock(self):
        with real_lock(self):
            events.append("locked")
            yield
            events.append("unlocked")

    monkeypatch.setattr(FileTokenStore, "lock", spy_lock)
    monkeypatch.setattr(FileTokenStore, "write", lambda self, raw: events.append("write") or real_write(self, raw))

    auth.login(_store(tmp_path), USERNAME, PASSWORD, lambda: OTP_CODE)

    assert events == ["locked", "write", "unlocked"]


def test_starting_again_discards_the_previous_attempt(login_endpoint, tmp_path):
    flow = auth.LoginFlow(_store(tmp_path))
    flow.start(USERNAME, PASSWORD)
    flow.send_code(LINE)
    login_endpoint["password_grant"] = (401, {"error": "invalid_grant"})

    with pytest.raises(AuthError, match="invalid_grant"):
        flow.start(USERNAME, "wrong-pw")
    assert flow.lines == [] and not flow.code_sent
    with pytest.raises(AuthError, match="no SMS code was sent"):
        flow.finish(OTP_CODE)


# --- refresh -------------------------------------------------------------------------------------


@pytest.fixture
def tokens_file(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({
        "accessToken": OLD_ACCESS,
        "accessTokenExpirationTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "refreshToken": OLD_REFRESH,
        "refreshTokenExpirationTime": (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "jwt": [OLD_JWT],
        "userCsrfToken": CSRF,
        "serviceAES256": "k" * 22,
        "reese84": {"token": "x"},
        "exportedAt": "2030-01-01T00:00:00.000Z",
    }))
    return path


@pytest.fixture
def refresh_endpoint(monkeypatch):
    state = {"grant": (200, _token_body(NEW_ACCESS)), "jwt": httpx.Response(200, json={}, headers={"authorization": NEW_JWT}), "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if request.url.path == config.TOKEN_PATH:
            status, body = state["grant"]
            return httpx.Response(status, json=body)
        if request.url.path == "/v2/subscription/subscriptions/":
            return state["jwt"]
        raise AssertionError(f"unexpected request {request.url.path}")

    monkeypatch.setattr(
        auth, "DxlClient", lambda tokens, **kw: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler), **kw)
    )
    return state


def test_refresh_sends_grant_with_scope_and_recaptcha_header(tokens_file, refresh_endpoint):
    auth.refresh(FileTokenStore(tokens_file))

    grant = refresh_endpoint["requests"][0]
    assert dict(parse_qsl(grant.content.decode())) == {
        "grant_type": "refresh_token", "refresh_token": OLD_REFRESH,
        "client_id": config.OAUTH_CLIENT_ID, "scope": config.OAUTH_SCOPE,
    }
    assert grant.headers["recaptcha_enterprise"] == "true"
    assert grant.headers["vf-csrf-token"] == CSRF
    assert grant.headers["cookie"] == f"csrf-token={CSRF}"
    assert "authorization" not in grant.headers


def test_refresh_persists_rotated_tokens_and_keeps_other_fields(tokens_file, refresh_endpoint):
    before = json.loads(tokens_file.read_text())

    out = auth.refresh(FileTokenStore(tokens_file))

    after = json.loads(tokens_file.read_text())
    assert after["accessToken"] == NEW_ACCESS
    assert after["accessTokenExpirationTime"] == "2030-01-02T03:04:05.000Z"
    assert after["refreshToken"] == NEW_REFRESH
    assert after["jwt"] == [NEW_JWT]
    for key in ("userCsrfToken", "serviceAES256", "reese84", "exportedAt"):
        assert after[key] == before[key]
    assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
    assert (out.refresh_token_rotated, out.jwt_refreshed) == (True, True)


def test_refused_refresh_leaves_the_token_file_untouched(tokens_file, refresh_endpoint):
    refresh_endpoint["grant"] = (403, {"error": "access_denied", "error_description": "Access is denied"})
    before = tokens_file.read_bytes()

    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "auth", "refresh"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "auth_expired" and "access_denied" in error["message"]
    assert "Access is denied" not in error["message"]
    assert tokens_file.read_bytes() == before


@pytest.mark.parametrize("bad", ["", "   ", "a b"])
def test_refresh_with_a_blank_access_token_leaves_the_token_file_untouched(tokens_file, refresh_endpoint, bad):
    refresh_endpoint["grant"] = (200, {**_token_body(NEW_ACCESS), "access_token": bad})
    before = tokens_file.read_bytes()

    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "auth", "refresh"])

    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["type"] == "api_changed"
    assert tokens_file.read_bytes() == before


def test_refresh_errors_never_echo_the_refresh_token(tokens_file, refresh_endpoint):
    echoed = f"invalid refresh_token={OLD_REFRESH} csrf={CSRF}"
    refresh_endpoint["grant"] = (400, {"error": "invalid_grant", "error_description": echoed, "transactionId": OLD_REFRESH})

    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "auth", "refresh"])

    assert result.exit_code == 3
    error = json.loads(result.stderr)["error"]
    assert error["transaction_id"] == refresh_endpoint["requests"][0].headers["vf-trace-transaction-id"]
    for secret in (OLD_REFRESH, CSRF, OLD_ACCESS):
        assert secret not in result.output


def test_expired_refresh_token_is_never_sent(tokens_file, refresh_endpoint):
    raw = json.loads(tokens_file.read_text())
    raw["refreshTokenExpirationTime"] = "2020-01-01T00:00:00.000Z"
    tokens_file.write_text(json.dumps(raw))

    with pytest.raises(AuthError, match="refresh token expired"):
        auth.refresh(FileTokenStore(tokens_file))
    assert refresh_endpoint["requests"] == []

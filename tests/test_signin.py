"""The browser sign-in page, driven with httpx against 127.0.0.1, with a mocked Vodafone token
endpoint. All values are synthetic."""

import base64
import json
import time

import httpx
import pytest

from vodafone import auth, config
from vodafone.client import DxlClient
from vodafone.signin import MAX_CODE_ATTEMPTS, SignIn, SignInManager
from vodafone.tokens import FileTokenStore

USERNAME, PASSWORD, OTP_CODE = "600000001", "s3cr3t-pw", "123456"
FF_ACCESS, NEW_ACCESS, NEW_REFRESH = "F" * 28, "B" * 28, "s" * 32
LINE, LINE_2 = "6*****001", "7*****002"


def _jws(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def _token_body(access: str) -> dict:
    return {"access_token": access, "expires_at": "1893553445", "refresh_token": NEW_REFRESH, "refresh_token_expires": "1901329445"}


@pytest.fixture
def vodafone(monkeypatch):
    """The token endpoint's three sign-in steps plus the identity-JWT call."""
    state = {
        "password_grant": (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE]})}),
        "authorize": (202, {"message": "Te hemos enviado un SMS"}),
        "otp_token": (200, _token_body(NEW_ACCESS)),
        "requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        route = {config.TOKEN_PATH: "password_grant", config.OTP_AUTHORIZE_PATH: "authorize", config.OTP_TOKEN_PATH: "otp_token"}
        if request.url.path in route:
            reply = state[route[request.url.path]]
            status, body = reply(request) if callable(reply) else reply
            return httpx.Response(status, json=body)
        if request.url.path == "/v2/subscription/subscriptions/":
            return httpx.Response(200, json={}, headers={"authorization": "h.eyJ4IjoxfQ.s"})
        raise AssertionError(f"unexpected request {request.url.path}")

    monkeypatch.setattr(
        auth, "DxlClient", lambda tokens, **kw: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler), **kw)
    )
    state["paths"] = lambda: [r.url.path for r in state["requests"]]
    return state


@pytest.fixture
def store(tmp_path):
    return FileTokenStore(tmp_path / "config" / "tokens.json")


@pytest.fixture
def signin(store, vodafone):
    s = SignIn(store)
    yield s
    s.stop()
    assert s.closed.wait(5)


def _browser(s: SignIn, **headers) -> httpx.Client:
    return httpx.Client(base_url=s.origin, headers={"Origin": s.origin, **headers}, timeout=10, trust_env=False)


def _path(s: SignIn, action: str = "") -> str:
    return f"/{s.secret}/{action}"


def _sign_in(browser: httpx.Client, s: SignIn, *, line: str = LINE, code: str = OTP_CODE) -> httpx.Response:
    assert browser.get(_path(s)).status_code == 200
    assert browser.post(_path(s, "start"), data={"username": USERNAME, "password": PASSWORD}).status_code == 200
    assert browser.post(_path(s, "send"), data={"line": line}).status_code == 200
    return browser.post(_path(s, "finish"), data={"code": code})


def test_the_three_pages_sign_in_and_write_the_token_file(signin, store, vodafone):
    with _browser(signin) as browser:
        first = browser.get(_path(signin))
        assert 'name="username"' in first.text and 'name="password"' in first.text
        second = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        assert f'value="{LINE}"' in second.text and "Send the code" in second.text
        assert not store.path.exists()
        third = browser.post(_path(signin, "send"), data={"line": LINE})
        assert 'name="code"' in third.text and LINE in third.text
        done = browser.post(_path(signin, "finish"), data={"code": OTP_CODE})

    assert done.status_code == 200 and "Signed in, you can close this tab" in done.text
    assert signin.done.is_set() and signin.result.tokens_file == str(store.path)
    assert vodafone["paths"]() == [config.TOKEN_PATH, config.OTP_AUTHORIZE_PATH, config.OTP_TOKEN_PATH, "/v2/subscription/subscriptions/"]
    grant = vodafone["requests"][0].content.decode()
    assert f"username={USERNAME}" in grant and f"password={PASSWORD}" in grant
    assert json.loads(vodafone["requests"][2].content)["code"] == OTP_CODE
    assert store.load().access_token == NEW_ACCESS


def test_the_line_page_offers_each_masked_number(signin, store, vodafone):
    vodafone["password_grant"] = (200, {**_token_body(FF_ACCESS), "jws": _jws({"msisdnList": [LINE, LINE_2, USERNAME]})})
    with _browser(signin) as browser:
        page = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        assert f'value="{LINE}"' in page.text and f'value="{LINE_2}"' in page.text
        assert page.text.count('type="radio"') == 2  # the unmasked entry is dropped
        unlisted = browser.post(_path(signin, "send"), data={"line": USERNAME})
        assert "Choose one of the numbers listed." in unlisted.text
        assert vodafone["paths"]() == [config.TOKEN_PATH]  # no SMS for an unlisted number
        browser.post(_path(signin, "send"), data={"line": LINE_2})

    assert json.loads(vodafone["requests"][1].content)["login_hint"] == LINE_2


# --- security ------------------------------------------------------------------------------------


def test_it_binds_127_0_0_1_on_a_random_port_with_a_long_secret(store, vodafone):
    first, second = SignIn(store), SignIn(store)
    try:
        assert first._httpd.server_address[0] == "127.0.0.1"
        assert first.port != second.port and first.secret != second.secret
        assert len(first.secret) >= 43 and first.id != first.secret
        assert first.url == f"http://127.0.0.1:{first.port}/{first.secret}/"
    finally:
        first.stop()
        second.stop()


@pytest.mark.parametrize("path", ["/", "/favicon.ico", "/{wrong}/", "/{secret}", "/{prefix}/", "/{secret}x/", "//{secret}/", "/{secret}/nope"])
def test_a_wrong_or_missing_secret_is_not_found(signin, vodafone, path):
    path = path.format(secret=signin.secret, prefix=signin.secret[:-1], wrong="A" * len(signin.secret))
    with _browser(signin) as browser:
        got = browser.get(path)
        posted = browser.post(path + "start" if path.endswith("/") else path, data={"username": USERNAME, "password": PASSWORD})

    assert got.status_code == 404 and signin.secret not in got.text
    assert posted.status_code == 404
    assert vodafone["requests"] == []


@pytest.mark.parametrize("host", ["localhost:{port}", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "", "127.0.0.1:{port}.evil.example"])
def test_another_host_header_is_refused(signin, vodafone, host):
    with _browser(signin, Host=host.format(port=signin.port)) as browser:
        got = browser.get(_path(signin))
        posted = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})

    assert got.status_code == posted.status_code == 400
    assert "Sign in" not in got.text
    assert vodafone["requests"] == []


@pytest.mark.parametrize("origin", [None, "null", "http://evil.example", "http://localhost:{port}", "https://127.0.0.1:{port}", "http://127.0.0.1:1"])
def test_a_post_from_another_origin_is_refused(signin, vodafone, origin):
    with httpx.Client(base_url=signin.origin, trust_env=False) as browser:
        headers = {"Origin": origin.format(port=signin.port)} if origin else {}
        got = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD}, headers=headers)

    assert got.status_code == 403
    assert vodafone["requests"] == []


def test_secrets_are_refused_in_a_query_string(signin, vodafone):
    with _browser(signin) as browser:
        assert browser.get(_path(signin), params={"x": "1"}).status_code == 400
        assert browser.get(_path(signin) + "?").status_code == 400
        got = browser.post(_path(signin, "start"), params={"username": USERNAME, "password": PASSWORD}, data={})

    assert got.status_code == 400
    assert vodafone["requests"] == []


@pytest.mark.parametrize(
    ("headers", "content", "status"),
    [
        ({"Content-Type": "application/json"}, json.dumps({"username": USERNAME, "password": PASSWORD}), 415),
        ({"Content-Type": "text/plain"}, f"username={USERNAME}&password={PASSWORD}", 415),
        ({"Content-Type": "application/x-www-form-urlencoded"}, "username=" + "x" * 5000 + "&password=p", 413),
    ],
)
def test_only_a_small_urlencoded_form_is_read(signin, vodafone, headers, content, status):
    with _browser(signin) as browser:
        got = browser.post(_path(signin, "start"), content=content, headers=headers)

    assert got.status_code == status
    assert vodafone["requests"] == []


def test_every_page_sends_a_strict_policy_and_loads_nothing_external(signin, vodafone):
    with _browser(signin) as browser:
        pages = [
            browser.get(_path(signin)),
            browser.get("/"),
            browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD}),
            browser.post(_path(signin, "send"), data={"line": LINE}),
        ]

    for page in pages:
        csp = page.headers["content-security-policy"]
        assert csp.startswith("default-src 'none'; style-src 'sha256-") and "form-action 'self'" in csp
        assert "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
        assert page.headers["cache-control"] == "no-store"
        assert page.headers["referrer-policy"] == "same-origin"
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["server"] == "vodafone-cli"
        for marker in ("<script", "src=", "href=", "http://", "https://", "url("):
            assert marker not in page.text


def test_it_expires_and_stops_serving(store, vodafone):
    s = SignIn(store, ttl=0.3)
    assert s.active

    assert s.closed.wait(5)
    assert not s.active
    with _browser(s) as browser, pytest.raises(httpx.ConnectError):
        browser.get(_path(s))
    assert vodafone["requests"] == []


def test_a_request_after_the_deadline_gets_expired_and_stops_the_server(signin, vodafone):
    signin._deadline = time.monotonic() - 1  # the timer has not fired yet

    with _browser(signin) as browser:
        got = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})

    assert got.status_code == 410 and "expired" in got.text
    assert signin.closed.wait(5)
    assert vodafone["requests"] == []


def test_the_link_is_single_use(signin, store, vodafone):
    with _browser(signin) as browser:
        assert "Signed in" in _sign_in(browser, signin).text

        assert signin.closed.wait(5)
        with pytest.raises(httpx.ConnectError):
            browser.get(_path(signin))


def test_once_a_step_succeeds_only_that_browser_can_continue(signin, vodafone):
    with _browser(signin) as browser, _browser(signin) as other:
        assert other.get(_path(signin)).status_code == 200  # anyone with the link, until a step succeeds
        page = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        cookie = page.headers["set-cookie"]
        assert cookie.startswith("vodafone_signin=") and f"Path=/{signin.secret}/" in cookie
        assert "HttpOnly" in cookie and "SameSite=Lax" in cookie

        for response in (
            other.get(_path(signin)),
            other.post(_path(signin, "send"), data={"line": LINE}),
            other.post(_path(signin, "start"), data={"username": "600000002", "password": "other"}),
        ):
            assert response.status_code == 403 and LINE not in response.text

    assert vodafone["paths"]() == [config.TOKEN_PATH]


def test_steps_out_of_order_are_refused_without_a_request(signin, vodafone):
    with _browser(signin) as browser:
        for action, data in (("send", {"line": LINE}), ("finish", {"code": OTP_CODE})):
            got = browser.post(_path(signin, action), data=data)
            assert got.status_code == 409 and 'name="password"' in got.text

    assert vodafone["requests"] == []


def _echo_everything(status: int):
    def respond(request: httpx.Request) -> tuple[int, dict]:
        sent = f"{request.content.decode()} {request.headers.get('authorization', '')}"
        return status, {"error": f"bad {sent}", "error_description": sent, "message": sent, "ecode": OTP_CODE}

    return respond


@pytest.mark.parametrize("route", ["password_grant", "authorize", "otp_token"])
@pytest.mark.parametrize("status", [400, 401, 500])
def test_pages_never_echo_what_was_typed_or_what_the_server_said(signin, store, vodafone, route, status):
    vodafone[route] = _echo_everything(status)
    with _browser(signin) as browser:
        pages = [browser.get(_path(signin))]
        pages.append(browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD}))
        if route != "password_grant":
            pages.append(browser.post(_path(signin, "send"), data={"line": LINE}))
        if route == "otp_token":
            pages.append(browser.post(_path(signin, "finish"), data={"code": OTP_CODE}))

    failed = pages[-1].text
    assert 'class="error"' in failed
    for page in pages:
        for secret in (USERNAME, PASSWORD, OTP_CODE, FF_ACCESS, "username=", "bad ", "error_description"):
            assert secret not in page.text
    assert not store.path.exists()


def test_a_wrong_password_shows_the_allowlisted_code_only(signin, vodafone):
    vodafone["password_grant"] = (401, {"error": "invalid_grant", "error_description": "Bad credentials", "ecode": "1005"})
    with _browser(signin) as browser:
        page = browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})

    assert "Vodafone did not accept that usuario and password." in page.text
    assert "invalid_grant" in page.text and "ecode 1005" in page.text
    assert "Bad credentials" not in page.text and 'name="password"' in page.text


def test_a_wrong_code_can_be_retried_then_the_sign_in_starts_over(signin, store, vodafone):
    vodafone["otp_token"] = (401, {"error": "invalid_grant"})
    with _browser(signin) as browser:
        browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        browser.post(_path(signin, "send"), data={"line": LINE})
        pages = [browser.post(_path(signin, "finish"), data={"code": "000000"}) for _ in range(MAX_CODE_ATTEMPTS)]

    for page in pages[:-1]:
        assert "Vodafone did not accept that code." in page.text and 'name="code"' in page.text
    assert "Too many attempts" in pages[-1].text and 'name="password"' in pages[-1].text
    assert "000000" not in "".join(p.text for p in pages)
    assert not store.path.exists()


def test_blank_fields_are_asked_again_without_a_request(signin, vodafone):
    with _browser(signin) as browser:
        page = browser.post(_path(signin, "start"), data={"username": " ", "password": PASSWORD})
        assert "Enter your usuario and password." in page.text
        browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        browser.post(_path(signin, "send"), data={"line": LINE})
        page = browser.post(_path(signin, "finish"), data={"code": "  "})

    assert "Enter the code from the SMS." in page.text
    assert vodafone["paths"]() == [config.TOKEN_PATH, config.OTP_AUTHORIZE_PATH]


def test_start_over_goes_back_to_the_first_page(signin, vodafone):
    with _browser(signin) as browser:
        browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        # An empty form, as a browser posts it: an empty body that still names its type.
        page = browser.post(_path(signin, "restart"), content=b"", headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert 'name="password"' in page.text
        assert browser.post(_path(signin, "send"), data={"line": LINE}).status_code == 409
        assert "Signed in" in _sign_in(browser, signin).text


def test_nothing_is_logged(signin, vodafone, capfd):
    vodafone["otp_token"] = (401, {"error": "invalid_grant"})
    with _browser(signin) as browser:
        browser.get("/nope")
        browser.get(_path(signin), headers={"Host": "evil.example"})
        browser.post(_path(signin, "start"), data={"username": USERNAME, "password": PASSWORD})
        browser.post(_path(signin, "send"), data={"line": LINE})
        browser.post(_path(signin, "finish"), data={"code": OTP_CODE})
        browser.request("PUT", _path(signin))

    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_the_manager_runs_one_sign_in_at_a_time(store, vodafone):
    manager = SignInManager(store)
    try:
        first = manager.begin()
        assert manager.begin() is first  # the same link while it is in progress
        assert manager.find(first.id) is first and manager.find("other") is None and manager.find(None) is None

        with _browser(first) as browser:
            _sign_in(browser, first)
        assert first.closed.wait(5)

        second = manager.begin()
        assert second is not first and second.secret != first.secret
        manager.cancel(second)
        assert second.closed.wait(5) and manager.find(second.id) is None
    finally:
        manager.close()

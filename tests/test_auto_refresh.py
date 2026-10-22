"""Automatic refresh in the shared read path (api.run_with_client), and the token-file lock.
Mock token endpoint and DXL API; all values are synthetic."""

import json
import stat
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from vodafone import api, auth, cli, config
from vodafone.client import DxlClient
from vodafone.errors import NotSignedInError
from vodafone.tokens import FileTokenStore

OLD_ACCESS, OLD_REFRESH = "A" * 28, "r" * 32
NEW_ACCESS, NEW_REFRESH = "B" * 28, "s" * 32
OTHER_ACCESS = "C" * 28  # what "another process" refreshed to
CSRF = "c" * 36
ACCESS_EXP = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
REFRESH_EXP = datetime(2030, 4, 2, 3, 4, 5, tzinfo=UTC)
ACCOUNT = "100000001"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write(path, *, access_in: timedelta | None, refresh: str | None = OLD_REFRESH, refresh_in=timedelta(days=60), access=OLD_ACCESS):
    now = datetime.now(UTC)
    raw = {"accessToken": access, "userCsrfToken": CSRF, "jwt": ["h.e30.s"], "serviceAES256": "k" * 22}
    if access_in is not None:
        raw["accessTokenExpirationTime"] = _iso(now + access_in)
    if refresh:
        raw["refreshToken"] = refresh
        raw["refreshTokenExpirationTime"] = _iso(now + refresh_in)
    path.write_text(json.dumps(raw))
    path.chmod(0o600)


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.json"
    _write(path, access_in=timedelta(hours=-1))
    return path


@pytest.fixture
def endpoint(monkeypatch):
    """One mock for the token endpoint (auth.DxlClient) and the data API (api.DxlClient)."""
    state = {
        "grant": (200, {"access_token": NEW_ACCESS, "expires_at": str(int(ACCESS_EXP.timestamp())),
                        "refresh_token": NEW_REFRESH, "refresh_token_expires": str(int(REFRESH_EXP.timestamp()))}),
        "requests": [],
    }
    guard = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with guard:
            state["requests"].append(request)
        if request.url.path == config.TOKEN_PATH:
            time.sleep(0.05)  # long enough for a concurrent caller to queue on the lock
            status, body = state["grant"]
            return httpx.Response(status, json=body)
        if request.url.path == "/v2/subscription/subscriptions/":
            return httpx.Response(200, json={}, headers={"authorization": "h.eyJuZXciOjF9.s"})
        if request.url.path == "/v2/customer/customerAccounts":
            return httpx.Response(200, json={"items": [{"id": ACCOUNT}]})
        if request.url.path == f"/v2/customer/customerAccounts/{ACCOUNT}":
            return httpx.Response(200, json={"id": ACCOUNT})
        return httpx.Response(200, json={"bearer": request.headers.get("authorization")})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(auth, "DxlClient", lambda tokens, **kw: DxlClient(tokens, spacing=0, transport=transport, **kw))
    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=transport))
    state["paths"] = lambda: [r.url.path for r in state["requests"]]
    state["grants"] = lambda: [r for r in state["requests"] if r.url.path == config.TOKEN_PATH]
    return state


def _read(path):
    """A read that reports the bearer it was sent with."""
    return api.run_with_client(path, lambda client: client.get_json("/data")["bearer"])


def test_an_expired_access_token_is_refreshed_once_then_used(tokens_file, endpoint):
    before = json.loads(tokens_file.read_text())

    assert _read(tokens_file) == f"Bearer {NEW_ACCESS}"

    assert endpoint["paths"]() == [config.TOKEN_PATH, "/v2/subscription/subscriptions/", "/data"]
    grant = endpoint["requests"][0].content.decode()
    assert "grant_type=refresh_token" in grant and f"refresh_token={OLD_REFRESH}" in grant
    after = json.loads(tokens_file.read_text())
    assert (after["accessToken"], after["refreshToken"]) == (NEW_ACCESS, NEW_REFRESH)
    assert after["accessTokenExpirationTime"] == "2030-01-02T03:04:05.000Z"
    assert after["jwt"] == ["h.eyJuZXciOjF9.s"]
    assert after["serviceAES256"] == before["serviceAES256"]  # an update, not a fresh login
    assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
    lock = FileTokenStore(tokens_file).lock_path
    assert lock.name == "tokens.json.lock" and stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_token_expiring_within_the_margin_is_refreshed(tokens_file, endpoint):
    _write(tokens_file, access_in=auth.REFRESH_MARGIN - timedelta(seconds=10))

    assert _read(tokens_file) == f"Bearer {NEW_ACCESS}"
    assert len(endpoint["grants"]()) == 1


@pytest.mark.parametrize("access_in", [timedelta(hours=1), auth.REFRESH_MARGIN + timedelta(seconds=30), None])
def test_a_valid_or_undated_access_token_is_used_untouched(tokens_file, endpoint, access_in):
    _write(tokens_file, access_in=access_in)
    before = tokens_file.read_bytes()

    assert _read(tokens_file) == f"Bearer {OLD_ACCESS}"
    assert endpoint["paths"]() == ["/data"]
    assert tokens_file.read_bytes() == before


@pytest.mark.parametrize(
    ("refresh", "refresh_in", "reason"),
    [
        (None, None, "no refresh token"),
        (OLD_REFRESH, timedelta(days=-1), "and the refresh token at"),
    ],
)
def test_an_expired_session_without_a_usable_refresh_token_is_not_signed_in(tokens_file, endpoint, refresh, refresh_in, reason):
    _write(tokens_file, access_in=timedelta(hours=-1), refresh=refresh, refresh_in=refresh_in)
    before = tokens_file.read_bytes()

    with pytest.raises(NotSignedInError) as exc:
        _read(tokens_file)

    assert exc.value.kind == "auth_expired" and exc.value.exit_code == 3
    assert reason in exc.value.reason and "vodafone auth login" in exc.value.message
    assert endpoint["requests"] == []
    assert tokens_file.read_bytes() == before


def test_a_token_about_to_expire_without_a_refresh_token_is_still_used(tokens_file, endpoint):
    _write(tokens_file, access_in=timedelta(seconds=30), refresh=None)

    assert _read(tokens_file) == f"Bearer {OLD_ACCESS}"
    assert endpoint["paths"]() == ["/data"]


@pytest.mark.parametrize(
    ("grant", "reason"),
    [
        ((400, {"error": "invalid_grant", "error_description": f"bad {OLD_REFRESH}"}), "refused to renew"),
        ((403, {"error": "access_denied"}), "refused to renew"),
        ((503, {"message": f"down {OLD_REFRESH}"}), "renewing the session failed (upstream_unavailable)"),
        ((200, {"access_token": "has a space"}), "renewing the session failed (api_changed)"),
    ],
)
def test_a_failed_refresh_is_not_signed_in_tried_once_and_leaves_the_file(tokens_file, endpoint, grant, reason):
    endpoint["grant"] = grant
    before = tokens_file.read_bytes()

    with pytest.raises(NotSignedInError) as exc:
        _read(tokens_file)

    assert exc.value.kind == "auth_expired" and reason in exc.value.reason
    if grant[0] != 200:  # an HTTP error carries the request's own trace id; a shape error has none
        assert exc.value.transaction_id == endpoint["requests"][0].headers["vf-trace-transaction-id"]
    for secret in (OLD_REFRESH, OLD_ACCESS, CSRF):
        assert secret not in str(exc.value.to_dict()) and secret not in exc.value.reason
    assert endpoint["paths"]() == [config.TOKEN_PATH]  # once, and no data call
    assert tokens_file.read_bytes() == before


def test_tokens_another_process_refreshed_between_check_and_lock_are_used(tokens_file, endpoint, monkeypatch):
    real_lock = FileTokenStore.lock

    def lock_after_another_refresh(self):
        _write(self.path, access_in=timedelta(hours=24), refresh="t" * 32, access=OTHER_ACCESS)
        return real_lock(self)

    monkeypatch.setattr(FileTokenStore, "lock", lock_after_another_refresh)

    assert _read(tokens_file) == f"Bearer {OTHER_ACCESS}"
    assert endpoint["grants"]() == []


def test_a_reader_waits_for_a_refresh_in_progress_and_uses_its_tokens(tokens_file, endpoint):
    held, release = threading.Event(), threading.Event()
    store = FileTokenStore(tokens_file)

    def other_process():  # a second descriptor on the lock file, as another process has
        with store.lock():
            held.set()
            release.wait(5)
            _write(tokens_file, access_in=timedelta(hours=24), refresh="t" * 32, access=OTHER_ACCESS)

    holder = threading.Thread(target=other_process)
    holder.start()
    assert held.wait(5)
    result = []
    reader = threading.Thread(target=lambda: result.append(_read(tokens_file)))
    reader.start()
    time.sleep(0.2)
    assert reader.is_alive() and endpoint["requests"] == []  # blocked on the lock
    release.set()
    holder.join(5)
    reader.join(5)

    assert result == [f"Bearer {OTHER_ACCESS}"]
    assert endpoint["grants"]() == []


def test_concurrent_readers_spend_the_rotating_refresh_token_once(tokens_file, endpoint):
    results = []
    threads = [threading.Thread(target=lambda: results.append(_read(tokens_file))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert results == [f"Bearer {NEW_ACCESS}"] * 4
    assert len(endpoint["grants"]()) == 1


def test_auth_refresh_takes_the_lock(tokens_file, endpoint, monkeypatch):
    entered = []
    real_lock = FileTokenStore.lock
    monkeypatch.setattr(FileTokenStore, "lock", lambda self: entered.append(self.path) or real_lock(self))

    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "auth", "refresh"])

    assert result.exit_code == 0, result.output
    assert entered == [tokens_file]


def test_the_cli_reads_refresh_and_keep_their_output(tokens_file, endpoint):
    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "me"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert list(json.loads(result.stdout)) == ["accounts"]
    assert len(endpoint["grants"]()) == 1
    assert json.loads(tokens_file.read_text())["accessToken"] == NEW_ACCESS


def test_auth_status_never_refreshes(tokens_file, endpoint):
    before = tokens_file.read_bytes()

    result = CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "auth", "status"])

    assert result.exit_code == 3  # the access token has expired, as before
    assert json.loads(result.stdout)["access_token"]["expired"] is True
    assert endpoint["requests"] == []
    assert tokens_file.read_bytes() == before


def test_a_missing_token_file_is_not_signed_in(tmp_path, endpoint):
    with pytest.raises(NotSignedInError) as exc:
        _read(tmp_path / "missing.json")

    assert exc.value.reason == "there is no token file"
    assert "vodafone auth login" in exc.value.message
    assert endpoint["requests"] == []

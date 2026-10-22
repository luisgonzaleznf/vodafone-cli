"""Native login and access-token refresh against the DXL OAuth2 token endpoint.

`login` reproduces the web app's sign-in: a password grant, the SMS-OTP second factor sent to one of
the masked numbers the grant offers, then a fetch of the identity JWT — no browser. `LoginFlow` runs
the same sign-in one step at a time, for the MCP server's browser page. `refresh`
exchanges the stored refresh token for a new access token, and `ensure_fresh` does so automatically
before a read when the access token is about to expire. All of them write the token set the rest of
the CLI reads, under the token file's lock; none ever logs a credential or token.
"""

import base64
import contextlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

from . import config
from .client import DxlClient
from .errors import ApiShapeError, AuthError, NotSignedInError, UsageError, VodafoneError
from .models import (
    AccessTokenStatus,
    AuthStatusOutput,
    JwtStatus,
    LoginOutput,
    RefreshOutput,
    RefreshTokenStatus,
    TokenStatusOutput,
    WireTokenResponse,
)
from .tokens import FileTokenStore, TokenSet

# The identity JWT is the bare value of the `authorization` response header here (not the grant's jws).
_JWT_SOURCE_PATH = "/v2/subscription/subscriptions/"
_RECAPTCHA_HEADER = {"recaptcha_enterprise": "true"}  # a literal flag; the web app sends no captcha token here
_OTP_HEADERS = {"otpversion": config.OTP_VERSION}
_EPOCH = re.compile(r"[0-9]{1,12}")
# A masked phone number as the grant's jws lists it (e.g. 6*****789). Only these are ever shown.
_MASKED_MSISDN = re.compile(r"[0-9]\*{3,8}[0-9]{2,4}")
# Reads refresh an access token this close to expiry, so it can't lapse in the middle of a command.
REFRESH_MARGIN = timedelta(seconds=60)
NO_IDENTITY = "the token file has no readable identity JWT"  # lines, usage and invoices need it


def _iso(dt: datetime) -> str:
    """The token file's timestamp format, e.g. 2030-01-02T03:04:05.000Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _epoch(value: str | None, field: str) -> datetime | None:
    if value is None or not _EPOCH.fullmatch(str(value)):
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (OverflowError, OSError, ValueError):
        raise ApiShapeError(f"the token endpoint's {field} is out of range") from None


def _grant(data: object) -> WireTokenResponse:
    """The token response; a missing, blank or non-header-safe access_token is rejected here, so
    nothing is written."""
    try:
        return WireTokenResponse.model_validate(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise ApiShapeError(f"unexpected token endpoint response shape (fields: {', '.join(fields) or 'body'})") from None


def _expiries(grant: WireTokenResponse, now: datetime) -> tuple[datetime, datetime | None]:
    access = _epoch(grant.expires_at, "expires_at")
    if access is None and _EPOCH.fullmatch(str(grant.expires_in or "")):
        try:
            access = now + timedelta(seconds=int(grant.expires_in))
        except OverflowError:
            raise ApiShapeError("the token endpoint's expires_in is out of range") from None
    if access is None:
        raise ApiShapeError("the token endpoint response has neither expires_at nor expires_in")
    return access, _epoch(grant.refresh_token_expires, "refresh_token_expires")


def _sms_lines(grant: WireTokenResponse) -> list[str]:
    """The masked numbers the password grant's jws offers for the SMS code; the web app sends the
    chosen one as the OTP login_hint. The jws is only read for these, so its signature is not
    checked, and an entry that is not a masked number is dropped."""
    try:
        payload = (grant.jws or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        claims = None
    listed = claims.get("msisdnList") if isinstance(claims, dict) else None
    if not isinstance(listed, list):
        listed = []
    lines = [m for m in listed if isinstance(m, str) and _MASKED_MSISDN.fullmatch(m)]
    if not lines:
        raise ApiShapeError("the password grant offered no phone number to send the SMS code to")
    return lines


def _fetch_jwt(access_token: str, csrf: str, cookies: httpx.Cookies) -> str | None:
    """Best-effort identity JWT: the `authorization` response header of the subscriptions call."""
    try:
        with DxlClient(TokenSet(access_token=access_token, csrf_token=csrf), cookies=cookies) as client:
            jwt = client.get_response(_JWT_SOURCE_PATH).headers.get("authorization", "")
    except VodafoneError:
        return None
    return jwt if jwt.count(".") == 2 and " " not in jwt else None


class LoginFlow:
    """The native sign-in as three resumable steps, for callers that collect the user's answers
    between requests: the CLI's prompts, or the MCP server's browser page.

    `start` runs the password grant and returns the masked numbers it offers for the SMS code,
    `send_code` has the code sent to one of them, and `finish` exchanges the code and writes the
    token set. The client-minted CSRF token and the cookie jar carry across the steps, as in the
    browser. Nothing is written unless every step succeeds, the password is not kept after `start`,
    and no error carries text from the server's responses. A failed step can be tried again (a
    mistyped code, say), and `start` begins afresh; once `finish` succeeds the flow is spent.
    """

    def __init__(self, store: FileTokenStore):
        self.store = store
        self._reset()

    def _reset(self) -> None:
        self._csrf = ""
        self._first_factor: str | None = None
        self._cookies: httpx.Cookies | None = None
        self._lines: list[str] = []
        self._code_sent = False

    @property
    def lines(self) -> list[str]:
        """The masked numbers offered by the password grant, e.g. 6*****789; empty before `start`."""
        return list(self._lines)

    @property
    def code_sent(self) -> bool:
        return self._code_sent

    def start(self, username: str, password: str) -> list[str]:
        """Password grant; returns the masked numbers the SMS code can be sent to."""
        self._reset()
        csrf = str(uuid.uuid4())  # the web app's vf-csrf-token is a client-minted UUID, echoed on every call
        with DxlClient(TokenSet(access_token="", csrf_token=csrf)) as client:
            first = _grant(
                client.post(
                    config.TOKEN_PATH,
                    data={
                        "username": username,
                        "password": password,
                        "client_id": config.OAUTH_CLIENT_ID,
                        "scope": config.OAUTH_SCOPE,
                        "grant_type": "password",
                    },
                    headers=_RECAPTCHA_HEADER,
                    bearer=False,
                )
            )
            cookies = client.cookies  # carried on to the OTP steps, as the browser does

        lines = _sms_lines(first)
        self._csrf, self._first_factor, self._cookies, self._lines = csrf, first.access_token, cookies, lines
        return list(lines)

    def send_code(self, line: str) -> None:
        """Have Vodafone send the SMS code to `line`, one of the masked numbers `start` returned. The
        OTP challenge's response is free text the endpoint controls (it can echo the username or
        password), so nothing from it is returned."""
        if self._first_factor is None:
            raise AuthError("sign in with usuario and password first; login aborted (nothing was saved)")
        if line not in self._lines:
            raise AuthError("no line chosen for the SMS code; login aborted (nothing was saved)")
        with DxlClient(self._first_factor_tokens(), cookies=self._cookies) as client:
            client.post(
                config.OTP_AUTHORIZE_PATH,
                json={"response_type": "code", "login_hint": line},
                headers=_OTP_HEADERS,
            )
            self._cookies = client.cookies
        self._code_sent = True

    def finish(self, code: str) -> LoginOutput:
        """Exchange the SMS code, fetch the identity JWT, and write the token set."""
        if self._first_factor is None or not self._code_sent:
            raise AuthError("no SMS code was sent yet; login aborted (nothing was saved)")
        code = code.strip()
        if not code:
            raise AuthError("no SMS code entered; login aborted (nothing was saved)")
        csrf = self._csrf
        with DxlClient(self._first_factor_tokens(), cookies=self._cookies) as client:
            final = _grant(
                client.post(
                    config.OTP_TOKEN_PATH,
                    json={"grant_type": "authorization_code", "code": code},
                    headers=_OTP_HEADERS,
                )
            )
            cookies = client.cookies

        now = datetime.now(UTC)
        access_expires, refresh_expires = _expiries(final, now)
        jwt = _fetch_jwt(final.access_token, csrf, cookies)

        token_set: dict[str, object] = {
            "accessToken": final.access_token,
            "accessTokenExpirationTime": _iso(access_expires),
            "userCsrfToken": csrf,
            "exportedAt": _iso(now),
        }
        if final.refresh_token:
            token_set["refreshToken"] = final.refresh_token
        if refresh_expires:
            token_set["refreshTokenExpirationTime"] = _iso(refresh_expires)
        if jwt:
            token_set["jwt"] = [jwt]
        with self.store.lock():  # never in the middle of another process's refresh
            self.store.write(token_set)
        self._reset()  # spent: the first-factor token and cookies are dropped

        return LoginOutput(
            tokens_file=str(self.store.path),
            access_token_expires_at=_iso(access_expires),
            refresh_token_expires_at=_iso(refresh_expires) if refresh_expires else None,
            jwt_fetched=jwt is not None,
        )

    def _first_factor_tokens(self) -> TokenSet:
        return TokenSet(access_token=self._first_factor or "", csrf_token=self._csrf)


def login(
    store: FileTokenStore,
    username: str,
    password: str,
    request_code: Callable[[], str],
    choose_line: Callable[[list[str]], str] | None = None,
) -> LoginOutput:
    """Password grant -> SMS-OTP -> identity JWT, then write the token set (the steps of LoginFlow).

    `choose_line` picks which of several masked numbers gets the SMS (without it, the first).
    `request_code` is called once the SMS is sent and must return the code the user typed. It gets
    nothing from the OTP challenge: that response is free text the endpoint controls, and it can
    echo the username or password. Nothing is written unless every step succeeds.
    """
    flow = LoginFlow(store)
    lines = flow.start(username, password)
    flow.send_code(choose_line(lines) if choose_line and len(lines) > 1 else lines[0])
    return flow.finish(request_code())


def refresh(store: FileTokenStore) -> RefreshOutput:
    """`vodafone auth refresh`: exchange the refresh token for a new access token and save the
    rotated set, under the token file's lock. The set is checked before the lock is taken, so a
    missing or unusable one creates nothing, and read again once the lock is held."""
    _check_renewable(store, store.load())
    with store.lock():
        tokens = store.load()
        _check_renewable(store, tokens)
        return _refresh(store, tokens)


def _check_renewable(store: FileTokenStore, tokens: TokenSet) -> None:
    if not tokens.refresh_token:
        raise NotSignedInError(
            f"the token file has no refreshToken: {store.path}; run `vodafone auth login`",
            reason="the token file has no refresh token",
        )
    if tokens.is_refresh_expired():
        raise NotSignedInError(
            f"the refresh token expired at {_iso(tokens.refresh_expires_at)}; run `vodafone auth login` "
            "(password + SMS code)",
            reason=f"the refresh token expired at {_iso(tokens.refresh_expires_at)}",
        )


def ensure_fresh(store: FileTokenStore) -> TokenSet:
    """The stored token set, refreshed first if its access token has expired or expires within
    REFRESH_MARGIN and a refresh token is present and unexpired. Used by every read (CLI and MCP);
    `auth status` never calls it. A token whose expiry is unknown is used as it is.

    The refresh runs under the token file's lock, and the file is re-read once the lock is held:
    if another process refreshed meanwhile, its tokens are used and nothing is sent. Nothing is
    locked when the set can't be renewed. A refresh is tried once, never retried, and a failure
    leaves the file as it was. While the access token is still valid, the read goes ahead with it
    whatever the refresh did. Once it has expired, a refused or impossible refresh is a
    NotSignedInError (auth_expired, sign in again), and any other failure keeps its own type: a
    Vodafone outage or block is exit 5, as for any request, not a lost session.
    """
    tokens = store.load()
    if not _due(tokens):
        return tokens
    if not _renewable(tokens):
        return _unrenewed(store, tokens)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(store.lock())
        except UsageError:  # the lock file can't be made, so nothing could be saved either
            if not tokens.is_access_expired():
                return tokens
            raise
        tokens = store.load()  # another process may have refreshed while we waited for the lock
        if not _due(tokens):
            return tokens
        if not _renewable(tokens):
            return _unrenewed(store, tokens)
        try:
            _refresh(store, tokens)
        except Exception as exc:
            if not tokens.is_access_expired():
                return store.load()  # still valid: the read goes ahead, and a later one tries again
            if isinstance(exc, VodafoneError):
                raise _refresh_failed(tokens, exc) from None
            raise
        return store.load()


def _due(tokens: TokenSet) -> bool:
    return tokens.is_access_expired(datetime.now(UTC) + REFRESH_MARGIN)


def _renewable(tokens: TokenSet) -> bool:
    return tokens.refresh_token is not None and not tokens.is_refresh_expired()


def _unrenewed(store: FileTokenStore, tokens: TokenSet) -> TokenSet:
    """A due token set that can't be renewed: used while it is still valid, else not signed in."""
    if not tokens.is_access_expired():
        return tokens
    raise _not_renewable(store, tokens)


def _expiry(tokens: TokenSet) -> str:
    return f"the access token expired at {_iso(tokens.access_expires_at)}"


def _not_renewable(store: FileTokenStore, tokens: TokenSet) -> NotSignedInError:
    if not tokens.refresh_token:
        return NotSignedInError(
            f"{_expiry(tokens)} and the token file has no refreshToken: {store.path}; run `vodafone auth login`",
            reason=f"{_expiry(tokens)} and there is no refresh token to renew it",
        )
    return NotSignedInError(
        f"{_expiry(tokens)} and the refresh token at {_iso(tokens.refresh_expires_at)}; run `vodafone auth login` "
        "(password + SMS code)",
        reason=f"{_expiry(tokens)} and the refresh token at {_iso(tokens.refresh_expires_at)}",
    )


def _refresh_failed(tokens: TokenSet, exc: VodafoneError) -> VodafoneError:
    """A failed automatic refresh of an expired access token. Only the original error's own
    (allowlisted) message is carried."""
    status = {"status": exc.status, "transaction_id": exc.transaction_id}
    if isinstance(exc, AuthError):  # the token endpoint refused the refresh token: only a sign-in helps
        return NotSignedInError(
            f"{_expiry(tokens)} and renewing it was refused: {exc.message}; run `vodafone auth login`",
            reason="Vodafone refused to renew the session",
            **status,
        )
    # Unavailable, blocked or an unexpected answer: the same error as for any other request.
    return type(exc)(f"{_expiry(tokens)} and renewing it failed: {exc.message}", **status)


def _refresh(store: FileTokenStore, tokens: TokenSet) -> RefreshOutput:
    """The refresh-token grant, then the rotated tokens and a new identity JWT saved into the file.
    The caller holds the lock."""
    with DxlClient(tokens) as client:
        grant = _grant(
            client.post(
                config.TOKEN_PATH,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": config.OAUTH_CLIENT_ID,
                    "scope": config.OAUTH_SCOPE,
                },
                headers=_RECAPTCHA_HEADER,
                bearer=False,
            )
        )
        cookies = client.cookies

    now = datetime.now(UTC)
    access_expires, refresh_expires = _expiries(grant, now)
    changes: dict[str, object] = {
        "accessToken": grant.access_token,
        "accessTokenExpirationTime": _iso(access_expires),
        "refreshedAt": _iso(now),
    }
    if grant.refresh_token:
        changes["refreshToken"] = grant.refresh_token
    if refresh_expires:
        changes["refreshTokenExpirationTime"] = _iso(refresh_expires)
    store.update(changes)

    jwt_refreshed = False
    new_jwt = _fetch_jwt(grant.access_token, tokens.csrf_token, cookies)
    if new_jwt:
        store.update({"jwt": [new_jwt]})
        jwt_refreshed = True

    final = store.load()
    return RefreshOutput(
        tokens_file=str(store.path),
        access_token_expires_at=_iso(final.access_expires_at),
        refresh_token_rotated=bool(grant.refresh_token) and grant.refresh_token != tokens.refresh_token,
        refresh_token_expires_at=_iso(final.refresh_expires_at) if final.refresh_expires_at else None,
        jwt_refreshed=jwt_refreshed,
    )


# --- status (offline: never refreshes) ------------------------------------------------------------


def _iso_or_none(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def token_status(store: FileTokenStore, now: datetime) -> TokenStatusOutput:
    """`vodafone auth status`: the token set's expiry times. Offline; never refreshes."""
    return _token_status(store, store.load(), now)


def _token_status(store: FileTokenStore, tokens: TokenSet, now: datetime) -> TokenStatusOutput:
    access_left = (tokens.access_expires_at - now).total_seconds() if tokens.access_expires_at else None
    refresh_left = (tokens.refresh_expires_at - now).total_seconds() if tokens.refresh_expires_at else None
    return TokenStatusOutput(
        store="file",
        tokens_file=str(store.path),
        exported_at=_iso_or_none(tokens.exported_at),
        access_token=AccessTokenStatus(
            expires_at=_iso_or_none(tokens.access_expires_at),
            expired=tokens.is_access_expired(now),
            seconds_remaining=max(0, int(access_left)) if access_left is not None else None,
        ),
        refresh_token=RefreshTokenStatus(
            present=tokens.refresh_token is not None,
            expires_at=_iso_or_none(tokens.refresh_expires_at),
            expired=tokens.is_refresh_expired(now),
            days_remaining=round(max(0.0, refresh_left) / 86400, 1) if refresh_left is not None else None,
        ),
        jwt=JwtStatus(present=tokens.jwt is not None, expires_at=_iso_or_none(tokens.jwt_expires_at())),
    )


def sign_in_status(store: FileTokenStore, now: datetime) -> AuthStatusOutput:
    """The MCP server's auth_status: token_status plus whether the read tools can run, i.e. the
    access token is valid or a refresh token can renew it, and the identity JWT the line, usage and
    invoice reads need is readable. A missing or unreadable token file means not signed in, which
    is an answer, not an error. The reasons are the ones the read tools give. Offline; never
    refreshes."""
    try:
        tokens = store.load()
    except NotSignedInError as err:
        return AuthStatusOutput(
            signed_in=False, reason=err.reason, store="file", tokens_file=str(store.path),
            exported_at=None, access_token=None, refresh_token=None, jwt=None,
        )
    status = _token_status(store, tokens, now)
    reason = None
    if status.access_token.expired and not status.refresh_token.present:
        reason = "the access token expired and there is no refresh token to renew it"
    elif status.access_token.expired and status.refresh_token.expired:
        reason = "the access token and the refresh token have expired"
    elif not tokens.claims():
        reason = NO_IDENTITY
    return AuthStatusOutput(signed_in=reason is None, reason=reason, **dict(status))

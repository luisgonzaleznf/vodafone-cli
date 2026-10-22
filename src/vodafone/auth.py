"""Native login and access-token refresh against the DXL OAuth2 token endpoint.

`login` reproduces the web app's sign-in: a password grant, the SMS-OTP second factor sent to one of
the masked numbers the grant offers, then a fetch of the identity JWT — no browser. `LoginFlow` runs
the same sign-in one step at a time, for the MCP server's browser page. `refresh`
exchanges the stored refresh token for a new access token. Both write the token set the rest of the
CLI reads; neither ever logs a credential or token.
"""

import base64
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError

from . import config
from .client import DxlClient
from .errors import ApiShapeError, AuthError, VodafoneError
from .models import LoginOutput, RefreshOutput, WireTokenResponse
from .tokens import FileTokenStore, TokenSet

# The identity JWT is the bare value of the `authorization` response header here (not the grant's jws).
_JWT_SOURCE_PATH = "/v2/subscription/subscriptions/"
_RECAPTCHA_HEADER = {"recaptcha_enterprise": "true"}  # a literal flag; the web app sends no captcha token here
_OTP_HEADERS = {"otpversion": config.OTP_VERSION}
_EPOCH = re.compile(r"[0-9]{1,12}")
# A masked phone number as the grant's jws lists it (e.g. 6*****789). Only these are ever shown.
_MASKED_MSISDN = re.compile(r"[0-9]\*{3,8}[0-9]{2,4}")


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
    tokens = store.load()
    if not tokens.refresh_token:
        raise AuthError(f"the token file has no refreshToken: {store.path}; run `vodafone auth login`")
    if tokens.is_refresh_expired():
        raise AuthError(
            f"the refresh token expired at {_iso(tokens.refresh_expires_at)}; run `vodafone auth login` "
            "(password + SMS code)"
        )

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

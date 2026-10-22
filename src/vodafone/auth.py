"""Native login and access-token refresh against the DXL OAuth2 token endpoint.

`login` reproduces the web app's sign-in: a password grant, the SMS-OTP second factor, then a fetch
of the identity JWT — no browser. `refresh` exchanges the stored refresh token for a new access
token. Both write the token set the rest of the CLI reads; neither ever logs a credential or token.
"""

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from . import config
from .client import DxlClient
from .errors import ApiShapeError, AuthError, VodafoneError
from .models import LoginOutput, RefreshOutput, WireTokenResponse
from .tokens import FileTokenStore, TokenSet

# The identity JWT is the bare value of the `authorization` response header here (not the grant's jws).
_JWT_SOURCE_PATH = "/v2/subscription/subscriptions/"
_RECAPTCHA_HEADER = {"recaptcha_enterprise": "true"}  # a literal flag; the web app sends no captcha token here
_EPOCH = re.compile(r"[0-9]{1,12}")


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


def _fetch_jwt(access_token: str, csrf: str) -> str | None:
    """Best-effort identity JWT: the `authorization` response header of the subscriptions call."""
    try:
        with DxlClient(TokenSet(access_token=access_token, csrf_token=csrf)) as client:
            jwt = client.get_response(_JWT_SOURCE_PATH).headers.get("authorization", "")
    except VodafoneError:
        return None
    return jwt if jwt.count(".") == 2 and " " not in jwt else None


def login(store: FileTokenStore, username: str, password: str, request_code: Callable[[], str]) -> LoginOutput:
    """Password grant -> SMS-OTP -> identity JWT, then write the token set.

    `request_code` is called once the SMS is sent and must return the code the user typed. It gets
    nothing from the OTP challenge: that response is free text the endpoint controls, and it can
    echo the username or password. Nothing is written unless every step succeeds.
    """
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

    first_factor = TokenSet(access_token=first.access_token, csrf_token=csrf)
    otp_headers = {"otpversion": config.OTP_VERSION}
    with DxlClient(first_factor) as client:
        client.post(
            config.OTP_AUTHORIZE_PATH,
            json={"response_type": "code", "login_hint": username},
            headers=otp_headers,
        )
        code = request_code().strip()
        if not code:
            raise AuthError("no SMS code entered; login aborted (nothing was saved)")
        final = _grant(
            client.post(
                config.OTP_TOKEN_PATH,
                json={"grant_type": "authorization_code", "code": code},
                headers=otp_headers,
            )
        )

    now = datetime.now(UTC)
    access_expires, refresh_expires = _expiries(final, now)
    jwt = _fetch_jwt(final.access_token, csrf)

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
    store.write(token_set)

    return LoginOutput(
        tokens_file=str(store.path),
        access_token_expires_at=_iso(access_expires),
        refresh_token_expires_at=_iso(refresh_expires) if refresh_expires else None,
        jwt_fetched=jwt is not None,
    )


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
    new_jwt = _fetch_jwt(grant.access_token, tokens.csrf_token)
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

"""DxlClient: a plain httpx client for api.dxl.local.vodafone.es.

Data GETs need the bearer + vf-* headers and a browser User-Agent. Auth POSTs also need a
csrf-token cookie matching vf-csrf-token, plus the cookies the server set earlier in the flow (such
as the OTP challenge's JSESSIONID), which the client's cookie jar keeps. Requests are issued one at
a time with a minimum spacing, and nothing is retried automatically: on a block, rate limit or
stall we stop and report.
"""

import math
import re
import time
import uuid
from datetime import UTC
from typing import Any

import httpx

from . import config
from .errors import (
    AccessDeniedError,
    ApiShapeError,
    AuthError,
    BlockedError,
    UpstreamError,
    scrub,
)
from .tokens import TokenSet

# OAuth2 error codes (RFC 6749 section 5.2) an auth-endpoint error may name. Nothing else from those
# error bodies is echoed: their free-text fields can reflect the submitted credentials.
_OAUTH_ERRORS = {
    "invalid_request": "malformed request",
    "invalid_client": "client not accepted",
    "invalid_grant": "credentials, code or refresh token rejected",
    "unauthorized_client": "client not allowed this grant",
    "unsupported_grant_type": "grant type not supported",
    "invalid_scope": "scope not accepted",
    "access_denied": "access denied",
}
_RETRY_AFTER = re.compile(r"[0-9]{1,4}")
# vf-csrf-token is sent as a cookie too: only a plain token can go there unquoted and still match.
_CSRF_TOKEN = re.compile(r"[A-Za-z0-9-]{1,128}")
_ECODE = re.compile(r"[0-9]{1,5}")  # DXL's numeric error code, e.g. 1007
# Fixed labels for a response's media type: the raw content-type header is never echoed.
_MEDIA_TYPES = (("json", "JSON"), ("html", "HTML"), ("pdf", "PDF"), ("xml", "XML"), ("text/plain", "plain text"))


class DxlClient:
    def __init__(
        self,
        tokens: TokenSet,
        *,
        base_url: str = config.API_BASE,
        spacing: float = config.REQUEST_SPACING,
        timeout: float = config.REQUEST_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        cookies: httpx.Cookies | None = None,
    ):
        self._tokens = tokens
        self._spacing = spacing
        self._last_request: float | None = None
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport, cookies=cookies, follow_redirects=False
        )

    def __enter__(self) -> "DxlClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    @property
    def cookies(self) -> httpx.Cookies:
        """The cookies the server has set, to carry on to the next client of a multi-step login."""
        return self._http.cookies

    def _headers(self, *, bearer: bool = True) -> dict[str, str]:
        auth = {"authorization": f"Bearer {self._tokens.access_token}"} if bearer and self._tokens.access_token else {}
        return {
            **auth,
            "vf-csrf-token": self._tokens.csrf_token,
            "vf-country-code": config.COUNTRY_CODE,
            "vf-target-environment": config.TARGET_ENVIRONMENT,
            "vf-trace-transaction-id": str(uuid.uuid4()),
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": config.USER_AGENT,
            "accept-language": config.ACCEPT_LANGUAGE,
        }

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._spacing - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)

    def get_json(
        self, path: str, params: dict[str, str] | None = None, *, headers: dict[str, str] | None = None
    ) -> Any:
        """GET `path` (no query string; pass `params`) and return the decoded JSON body."""
        return _json_body(self._get(path, params, headers, label=path), f"GET {path}")

    def get_bytes(self, path: str, *, label: str) -> tuple[bytes, str]:
        """GET `path` and return (body, content-type). `label` replaces the path in error messages,
        for paths that embed a download token."""
        response = self._get(path, None, None, label=label)
        return response.content, response.headers.get("content-type", "")

    def get_response(self, path: str) -> httpx.Response:
        """GET `path` and return the checked response, for callers that read its headers."""
        return self._get(path, None, None, label=path)

    def post(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        bearer: bool = True,
    ) -> Any:
        """POST a form (`data`) or JSON (`json`) and return the decoded JSON body.

        Used for the OAuth2 login/refresh and OTP steps. A 4xx (other than 429) is reported as an
        AuthError, since at these endpoints it means the credentials/grant were refused rather than
        a per-resource denial. Errors name at most an allowlisted OAuth2 error code: these endpoints'
        messages can echo the username, password, OTP code or tokens that were sent.
        """
        extra = {"origin": config.WEB_ORIGIN, "referer": f"{config.WEB_ORIGIN}/", **(headers or {})}
        request_headers = {**self._headers(bearer=bearer), **extra}
        # The auth endpoints compare a csrf-token cookie with the header before checking the grant.
        csrf = request_headers["vf-csrf-token"]
        if not _CSRF_TOKEN.fullmatch(csrf):
            raise AuthError("the CSRF token is not a plain token (letters, digits, '-'); run `vodafone auth login`")
        if data is not None:
            request_headers["content-type"] = "application/x-www-form-urlencoded"
        self._pace()
        try:
            # build_request adds the jar's cookies (path-matched); the CSRF cookie is added per
            # request, so it never lands in the jar or on a GET.
            request = self._http.build_request("POST", path, data=data, json=json, headers=request_headers)
            jar = request.headers.get("cookie")
            request.headers["cookie"] = f"{jar}; csrf-token={csrf}" if jar else f"csrf-token={csrf}"
            response = self._http.send(request)
        except httpx.TimeoutException:
            raise UpstreamError(f"POST {path} timed out; the backend may be stalled, wait before retrying")
        except httpx.HTTPError as exc:
            # str(exc) can include the full URL or body, so only report the exception type.
            raise UpstreamError(f"POST {path} failed: {type(exc).__name__}")
        finally:
            self._last_request = time.monotonic()

        status = response.status_code
        secrets = self._secrets(data, json)
        if 400 <= status < 500 and status != 429 and "html" not in response.headers.get("content-type", ""):
            detail = _upstream_detail(response, secrets, free_text=False)
            raise AuthError(
                f"{path} refused the request (HTTP {status}{': ' + detail if detail else ''})",
                status=status,
                transaction_id=_trace_id(response),
            )
        self._check(response, path, method="POST", secrets=secrets, free_text=False)
        return _json_body(response, f"POST {path}")

    def _secrets(self, *payloads: Any) -> frozenset[str]:
        """Values an error must never echo: the session's tokens and every string sent in `payloads`
        (username, password, OTP code, refresh token, query values such as the identity document),
        except the public client id and scope."""
        values = [self._tokens.access_token, self._tokens.csrf_token, self._tokens.refresh_token]
        for payload in payloads:
            if isinstance(payload, dict):
                values += [v for v in payload.values() if isinstance(v, str)]
        return frozenset(v for v in values if v and len(v) >= 4) - {config.OAUTH_CLIENT_ID, config.OAUTH_SCOPE}

    def _get(
        self, path: str, params: dict[str, str] | None, headers: dict[str, str] | None, *, label: str
    ) -> httpx.Response:
        self._pace()
        try:
            response = self._http.get(path, params=params, headers={**self._headers(), **(headers or {})})
        except httpx.TimeoutException:
            raise UpstreamError(f"GET {label} timed out; the backend may be stalled, wait before retrying")
        except httpx.HTTPError as exc:
            # str(exc) can include the full URL (query may hold identifiers), so only report the type.
            raise UpstreamError(f"GET {label} failed: {type(exc).__name__}")
        finally:
            self._last_request = time.monotonic()
        secrets = self._secrets(params, headers)
        if label != path:  # the path itself is sensitive, e.g. a download token
            secrets |= {path, path.rsplit("/", 1)[-1]}
        self._check(response, label, secrets=secrets)
        return response

    def _check(
        self,
        response: httpx.Response,
        path: str,
        method: str = "GET",
        *,
        secrets: frozenset[str],
        free_text: bool = True,
    ) -> None:
        status = response.status_code
        content_type = response.headers.get("content-type", "")

        if "html" in content_type or ("pdf" not in content_type and "_Incapsula_Resource" in response.text):
            raise BlockedError(
                f"{method} {path} returned an Imperva bot-protection page (HTTP {status}); "
                "the API now gates non-browser clients and needs a reese84 cookie or TLS impersonation",
                status=status,
            )

        if 200 <= status < 300:
            return

        detail = _upstream_detail(response, secrets, free_text=free_text)
        suffix = f": {detail}" if detail else ""
        transaction_id = _trace_id(response)

        if status == 401:
            expires = self._tokens.access_expires_at
            if self._tokens.is_access_expired():
                raise AuthError(
                    f"access token expired at {expires.astimezone(UTC).isoformat()}; run `vodafone auth login` "
                    "(or re-export .local-secrets/tokens.json from a logged-in browser)",
                    status=status,
                    transaction_id=transaction_id,
                )
            valid = f"valid until {expires.astimezone(UTC).isoformat()}" if expires else "expiry unknown"
            raise AccessDeniedError(
                f"{method} {path} was refused with 401 although the access token is still {valid}; "
                f"the requested account/line was denied, not the session{suffix}",
                status=status,
                transaction_id=transaction_id,
            )
        if status == 403:
            raise BlockedError(f"{method} {path} was forbidden (HTTP 403){suffix}", status=status, transaction_id=transaction_id)
        if status == 429:
            # Shown only as a small number that is no part of anything this request sent.
            retry = response.headers.get("retry-after", "")
            shown = _RETRY_AFTER.fullmatch(retry) and int(retry) <= 3600 and not any(retry in s for s in secrets)
            hint = f"; retry after {retry}s" if shown else "; back off before retrying"
            raise BlockedError(f"{method} {path} was rate-limited (HTTP 429){hint}", status=status, transaction_id=transaction_id)
        if status >= 500:
            raise UpstreamError(
                f"Vodafone backend error on {method} {path} (HTTP {status}){suffix}", status=status, transaction_id=transaction_id
            )
        raise ApiShapeError(f"{method} {path} returned HTTP {status}{suffix}", status=status, transaction_id=transaction_id)


class _NonFiniteNumber(ValueError):
    pass


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # e.g. 1e400
        raise _NonFiniteNumber(text)
    return value


def _no_constant(name: str) -> float:
    raise _NonFiniteNumber(name)  # NaN / Infinity / -Infinity, which Python's json accepts by default


def _json_body(response: httpx.Response, what: str) -> Any:
    """The decoded JSON body. A non-finite number is a shape error: JSON has none, and the CLI's
    output must stay strict JSON."""
    try:
        return response.json(parse_float=_finite_float, parse_constant=_no_constant)
    except _NonFiniteNumber:
        raise ApiShapeError(f"{what} returned a non-finite number (NaN or Infinity)", status=response.status_code)
    except ValueError:
        raise ApiShapeError(
            f"{what} returned HTTP {response.status_code} with a non-JSON body "
            f"({describe_media_type(response.headers.get('content-type', ''))})",
            status=response.status_code,
        )


def describe_media_type(content_type: str) -> str:
    """A fixed label for a response's content-type header. The raw header is never echoed: a server
    can put anything in it, including a value the request sent."""
    media = content_type.split(";", 1)[0].strip().lower()
    if not media:
        return "no content type"
    return next((label for marker, label in _MEDIA_TYPES if marker in media), "another content type")


def _trace_id(response: httpx.Response) -> str | None:
    """The vf-trace-transaction-id this client minted for the failing request (a random UUID), for
    matching Vodafone-side logs. A server-supplied transactionId is never reported: it can echo
    anything the request carried, such as the identity document."""
    return response.request.headers.get("vf-trace-transaction-id")


def _upstream_detail(response: httpx.Response, secrets: frozenset[str], *, free_text: bool = True) -> str:
    """Read either DXL error shape: {message, description, error} or csm-api's {code, reason,
    message, status}.

    With free_text=False (the auth endpoints) the detail is only an allowlisted OAuth2 error code
    and DXL's numeric `ecode`; otherwise any of `secrets` in it is masked.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    if not free_text:
        parts = []
        code = body.get("error")
        if isinstance(code, str) and code in _OAUTH_ERRORS:
            parts.append(f"{code} ({_OAUTH_ERRORS[code]})")
        ecode = str(body.get("ecode", ""))
        if _ECODE.fullmatch(ecode) and ecode not in secrets:
            parts.append(f"ecode {ecode}")
        return ", ".join(parts)
    message = (
        body.get("description")
        or body.get("error_description")  # OAuth2 token-endpoint errors
        or body.get("message")
        or body.get("reason")
        or body.get("error")
        or ""
    )
    message = str(message)
    for secret in sorted(secrets, key=len, reverse=True):  # longest first: no fragments of a longer one survive
        message = message.replace(secret, "…")
    return scrub(message).strip()

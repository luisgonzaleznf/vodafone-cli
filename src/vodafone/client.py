"""DxlClient: a plain httpx client for api.dxl.local.vodafone.es.

Authenticated DXL calls need only the bearer + vf-* headers and a browser User-Agent; no cookies
(in particular no Imperva reese84 token). Requests are issued one at a time with a minimum
spacing, and nothing is retried automatically: on a block, rate limit or stall we stop and report.
"""

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


class DxlClient:
    def __init__(
        self,
        tokens: TokenSet,
        *,
        base_url: str = config.API_BASE,
        spacing: float = config.REQUEST_SPACING,
        timeout: float = config.REQUEST_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        self._tokens = tokens
        self._spacing = spacing
        self._last_request: float | None = None
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport, follow_redirects=False)

    def __enter__(self) -> "DxlClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._tokens.access_token}",
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
        response = self._get(path, params, headers, label=path)
        try:
            return response.json()
        except ValueError:
            content_type = response.headers.get("content-type", "")
            raise ApiShapeError(
                f"GET {path} returned HTTP {response.status_code} with a non-JSON body ({content_type})",
                status=response.status_code,
            )

    def get_bytes(self, path: str, *, label: str) -> tuple[bytes, str]:
        """GET `path` and return (body, content-type). `label` replaces the path in error messages,
        for paths that embed a download token."""
        response = self._get(path, None, None, label=label)
        return response.content, response.headers.get("content-type", "")

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
        self._check(response, label)
        return response

    def _check(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        content_type = response.headers.get("content-type", "")

        if "html" in content_type or ("pdf" not in content_type and "_Incapsula_Resource" in response.text):
            raise BlockedError(
                f"GET {path} returned an Imperva bot-protection page (HTTP {status}); "
                "the API now gates non-browser clients and needs a reese84 cookie or TLS impersonation",
                status=status,
            )

        if 200 <= status < 300:
            return

        detail, transaction_id = _upstream_detail(response)
        suffix = f": {detail}" if detail else ""

        if status == 401:
            expires = self._tokens.access_expires_at
            if self._tokens.is_access_expired():
                raise AuthError(
                    f"access token expired at {expires.astimezone(UTC).isoformat()}; re-export "
                    ".local-secrets/tokens.json from a logged-in browser (`auth refresh` is not implemented yet)",
                    status=status,
                    transaction_id=transaction_id,
                )
            valid = f"valid until {expires.astimezone(UTC).isoformat()}" if expires else "expiry unknown"
            raise AccessDeniedError(
                f"GET {path} was refused with 401 although the access token is still {valid}; "
                f"the requested account/line was denied, not the session{suffix}",
                status=status,
                transaction_id=transaction_id,
            )
        if status == 403:
            raise BlockedError(f"GET {path} was forbidden (HTTP 403){suffix}", status=status, transaction_id=transaction_id)
        if status == 429:
            retry = response.headers.get("retry-after")
            hint = f"; retry after {retry}s" if retry else "; back off before retrying"
            raise BlockedError(f"GET {path} was rate-limited (HTTP 429){hint}", status=status, transaction_id=transaction_id)
        if status >= 500:
            raise UpstreamError(
                f"Vodafone backend error on GET {path} (HTTP {status}){suffix}", status=status, transaction_id=transaction_id
            )
        raise ApiShapeError(f"GET {path} returned HTTP {status}{suffix}", status=status, transaction_id=transaction_id)


def _upstream_detail(response: httpx.Response) -> tuple[str, str | None]:
    """Read either DXL error shape: {message, description, error, transactionId} or csm-api's
    {code, reason, message, status}."""
    try:
        body = response.json()
    except ValueError:
        return "", None
    if not isinstance(body, dict):
        return "", None
    message = body.get("description") or body.get("message") or body.get("reason") or body.get("error") or ""
    transaction_id = body.get("transactionId")
    return scrub(str(message)).strip(), transaction_id if isinstance(transaction_id, str) else None

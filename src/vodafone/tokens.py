"""Token set loading. For now the store is the gitignored .local-secrets/tokens.json export;
an OS keychain backend can replace it later behind the same TokenStore interface.
"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import AuthError


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TokenSet:
    access_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    jwt: str | None = field(default=None, repr=False)
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    exported_at: datetime | None = None

    def is_access_expired(self, now: datetime | None = None) -> bool:
        if self.access_expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.access_expires_at

    def is_refresh_expired(self, now: datetime | None = None) -> bool:
        if self.refresh_expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.refresh_expires_at

    def claims(self) -> dict:
        """The identity JWT's payload. Not signature-checked: we only read our own token's claims."""
        if not self.jwt:
            return {}
        try:
            payload = self.jwt.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError, binascii.Error):
            return {}
        return claims if isinstance(claims, dict) else {}


class TokenStore(Protocol):
    def load(self) -> TokenSet: ...


class FileTokenStore:
    """Reads the localStorage export (mcare-LocalStore.key.* values) written to tokens.json."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> TokenSet:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise AuthError(f"token file not found: {self.path} (export the token set first, see README)")
        except (OSError, ValueError):
            raise AuthError(f"token file unreadable or not JSON: {self.path}")
        if not isinstance(raw, dict):
            raise AuthError(f"token file has an unexpected shape: {self.path}")

        missing = [k for k in ("accessToken", "userCsrfToken") if not raw.get(k)]
        if missing:
            raise AuthError(f"token file is missing {', '.join(missing)}: {self.path}")

        jwt = raw.get("jwt")
        if isinstance(jwt, list):  # the export stores it as a one-element list
            jwt = jwt[0] if jwt else None

        return TokenSet(
            access_token=raw["accessToken"],
            csrf_token=raw["userCsrfToken"],
            refresh_token=raw.get("refreshToken") or None,
            jwt=jwt if isinstance(jwt, str) else None,
            access_expires_at=_parse_iso(raw.get("accessTokenExpirationTime")),
            refresh_expires_at=_parse_iso(raw.get("refreshTokenExpirationTime")),
            exported_at=_parse_iso(raw.get("exportedAt")),
        )

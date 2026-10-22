"""Token set loading. For now the store is the gitignored .local-secrets/tokens.json export;
an OS keychain backend can replace it later behind the same TokenStore interface.
"""

import base64
import binascii
import errno
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import NotSignedInError, UsageError

# A credential sent in a request header: one run of visible ASCII (no whitespace or control chars).
HEADER_SAFE = re.compile(r"[!-~]+")


def _parse_iso(value: Any) -> datetime | None:
    """A timestamp as UTC; None if absent, malformed or out of range."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


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

    def jwt_expires_at(self) -> datetime | None:
        """The identity JWT's `exp`, or None if absent, not a number or out of range."""
        exp = self.claims().get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            return None
        try:
            return datetime.fromtimestamp(exp, UTC)
        except (OverflowError, OSError, ValueError):
            return None


class TokenStore(Protocol):
    def load(self) -> TokenSet: ...

    def write(self, token_set: dict[str, Any]) -> None: ...

    def update(self, changes: dict[str, Any]) -> None: ...


class FileTokenStore:
    """Reads the localStorage export (mcare-LocalStore.key.* values) written to tokens.json."""

    def __init__(self, path: Path):
        self.path = path

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Hold an exclusive flock on `<tokens file>.lock` (created empty, mode 0600).

        Taken around every refresh and sign-in write: the CLI and any number of MCP servers can run
        at once, and the refresh token rotates, so two of them must not spend it together. Readers
        don't lock (writes are atomic renames). The lock file is never removed, which would let two
        processes lock different files; the lock goes with the descriptor, also if the process dies.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise self._lock_error(exc) from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise self._lock_error(exc) from None
            yield
        finally:
            os.close(fd)

    def _lock_error(self, exc: OSError) -> UsageError:
        reason = errno.errorcode.get(exc.errno, type(exc).__name__) if exc.errno else type(exc).__name__
        return UsageError(
            f"cannot lock the token file through {self.lock_path} ({reason}); its folder must be writable "
            "to save tokens"
        )

    def _atomic_write(self, raw: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A unique temp file created exclusively with mode 0600 (mkstemp uses O_EXCL): a fixed name
        # could be precreated with looser permissions, or as a symlink to an unrelated file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(raw, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def write(self, token_set: dict[str, Any]) -> None:
        """Replace the token file with a fresh set (mode 0600, atomic). Used by `auth login`, which
        must not carry stale fields (serviceAES256, reese84) from a previous session."""
        self._atomic_write(token_set)

    def update(self, changes: dict[str, Any]) -> None:
        """Merge `changes` into the file, keeping every other key (mode 0600, atomic). Used by
        `auth refresh`, which rotates the tokens but preserves jwt/serviceAES256/reese84."""
        raw = json.loads(self.path.read_text())
        raw.update(changes)
        self._atomic_write(raw)

    def load(self) -> TokenSet:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raise NotSignedInError(
                f"token file not found: {self.path}; run `vodafone auth login` (or export it, see README)",
                reason="there is no token file",
            )
        except (OSError, ValueError):
            raise NotSignedInError(
                f"token file unreadable or not JSON: {self.path}", reason="the token file is unreadable or not JSON"
            )
        if not isinstance(raw, dict):
            raise NotSignedInError(
                f"token file has an unexpected shape: {self.path}", reason="the token file has an unexpected shape"
            )

        missing = [k for k in ("accessToken", "userCsrfToken") if not raw.get(k)]
        if missing:
            raise NotSignedInError(
                f"token file is missing {', '.join(missing)}: {self.path}; run `vodafone auth login`",
                reason=f"the token file is missing {', '.join(missing)}",
            )

        jwt = raw.get("jwt")
        if isinstance(jwt, list):  # the export stores it as a one-element list
            jwt = jwt[0] if jwt else None
        # Credentials must be strings, header-safe where sent as headers: an object or list is no token.
        invalid = [
            k for k in ("accessToken", "userCsrfToken") if not isinstance(raw[k], str) or not HEADER_SAFE.fullmatch(raw[k])
        ]
        optional = {"refreshToken": raw.get("refreshToken"), "jwt": jwt}
        invalid += [k for k, v in optional.items() if v is not None and not isinstance(v, str)]
        if invalid:
            raise NotSignedInError(
                f"token file has an invalid {', '.join(invalid)}: {self.path}; run `vodafone auth login`",
                reason=f"the token file has an invalid {', '.join(invalid)}",
            )

        return TokenSet(
            access_token=raw["accessToken"],
            csrf_token=raw["userCsrfToken"],
            refresh_token=raw.get("refreshToken") or None,
            jwt=jwt or None,
            access_expires_at=_parse_iso(raw.get("accessTokenExpirationTime")),
            refresh_expires_at=_parse_iso(raw.get("refreshTokenExpirationTime")),
            exported_at=_parse_iso(raw.get("exportedAt")),
        )

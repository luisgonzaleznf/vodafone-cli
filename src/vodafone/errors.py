"""Typed errors. Each carries the CLI exit code it maps to.

Messages must never contain token, csrf or cookie values, nor request query strings (they can hold
the holder's NIF), nor server-supplied header values or ids. `transaction_id` is the
vf-trace-transaction-id the client minted for the failing request, never a value from the server.
"""

import re

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_BLOCKED = 5
EXIT_SHAPE = 7

_LONG_DIGITS = re.compile(r"\d{6,}")


def scrub(text: str) -> str:
    """Mask account ids / MSISDNs that upstream error messages echo back."""
    return _LONG_DIGITS.sub("…", text)


class VodafoneError(Exception):
    exit_code = 1
    kind = "internal"

    def __init__(self, message: str, *, status: int | None = None, transaction_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.transaction_id = transaction_id

    def to_dict(self) -> dict:
        out: dict = {"type": self.kind, "message": self.message, "exit_code": self.exit_code}
        if self.status is not None:
            out["http_status"] = self.status
        if self.transaction_id:
            out["transaction_id"] = self.transaction_id
        return out


class UsageError(VodafoneError):
    exit_code = EXIT_USAGE
    kind = "usage"


class CancelledError(UsageError):
    """A write that was not confirmed at the prompt; nothing was sent."""

    kind = "cancelled"


class AuthError(VodafoneError):
    """401 with an expired (or missing) access token: run `vodafone auth login`."""

    exit_code = EXIT_AUTH
    kind = "auth_expired"


class NotSignedInError(AuthError):
    """No usable session: the token file is missing or unreadable, or the access token expired and
    cannot be renewed. `message` keeps the CLI's remedy; `reason` says what is wrong in a few words
    without it."""

    def __init__(self, message: str, *, reason: str, status: int | None = None, transaction_id: str | None = None):
        super().__init__(message, status=status, transaction_id=transaction_id)
        self.reason = reason


class AccessDeniedError(AuthError):
    """401 while the access token is still valid: the resource/id was refused, not the session."""

    kind = "access_denied"


class BlockedError(VodafoneError):
    """Imperva challenge, 403 or 429: stop and back off."""

    exit_code = EXIT_BLOCKED
    kind = "blocked"


class UpstreamError(BlockedError):
    """Backend 5xx, timeout or stall. Shares exit code 5 with blocks and rate limits."""

    kind = "upstream_unavailable"


class OutcomeUnknownError(UpstreamError):
    """A write that may have reached Vodafone without a usable answer: a timeout or dropped
    connection after sending started, a 5xx, or a success reply that can't be read. It may or may
    not have taken effect, so it is never retried; the caller checks before trying again."""

    kind = "outcome_unknown"


class ClaimAlreadyOpenError(VodafoneError):
    """The billing account already has an open or reopened bill review, so no new one is sent."""

    exit_code = EXIT_BLOCKED
    kind = "claim_already_open"


class ApiShapeError(VodafoneError):
    """Unexpected status/body shape: the DXL API probably changed."""

    exit_code = EXIT_SHAPE
    kind = "api_changed"

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["hint"] = "The Vodafone DXL API may have changed; re-capture the endpoint and update vodafone/api.py."
        return out

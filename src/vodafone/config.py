"""Hosts, header constants and file locations for the DXL API."""

import os
from pathlib import Path

API_BASE = "https://api.dxl.local.vodafone.es"

COUNTRY_CODE = "ES"
TARGET_ENVIRONMENT = "aws-prod-es"
# A browser identity, as the web app sends (httpx would otherwise send python-httpx/...).
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "es-ES,es;q=0.9"

REQUEST_TIMEOUT = 25.0
# Minimum gap between consecutive requests; every command issues its calls one at a time.
REQUEST_SPACING = 0.4

TOKENS_FILE_ENV = "VODAFONE_TOKENS_FILE"
TOKENS_FILE_RELATIVE = Path(".local-secrets") / "tokens.json"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_tokens_path() -> Path:
    """$VODAFONE_TOKENS_FILE, else ./.local-secrets/tokens.json, else the checkout's copy."""
    if env := os.environ.get(TOKENS_FILE_ENV):
        return Path(env).expanduser()
    cwd_path = Path.cwd() / TOKENS_FILE_RELATIVE
    if cwd_path.exists():
        return cwd_path
    return _PROJECT_ROOT / TOKENS_FILE_RELATIVE

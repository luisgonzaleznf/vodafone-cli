"""JSON rendering for the CLI's output and error payloads.

Strict JSON: NaN/Infinity raise ValueError instead of being written as non-standard literals.
"""

import json
from typing import Any

from .errors import VodafoneError


def render(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)


def render_error(err: VodafoneError) -> str:
    return json.dumps({"error": err.to_dict()}, ensure_ascii=False, allow_nan=False)

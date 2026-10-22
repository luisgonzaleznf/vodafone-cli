"""JSON rendering shared by the CLI and the MCP server, so both emit byte-identical text."""

import json
from typing import Any

from .errors import VodafoneError


def render(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_error(err: VodafoneError) -> str:
    return json.dumps({"error": err.to_dict()}, ensure_ascii=False)

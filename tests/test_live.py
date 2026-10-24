"""Read-only checks against the real account, using the default token file.

Skipped unless VODAFONE_LIVE=1. `auth refresh` is not covered: a successful refresh would rotate
the token set on every run. Nor is `invoices claim`, not even --dry-run: it is the one command that
can send a claim, so it never runs against the account from a test.
"""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vodafone import cli

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("VODAFONE_LIVE") != "1", reason="set VODAFONE_LIVE=1 to run against the account"),
]


def _run(*args: str) -> dict:
    result = CliRunner().invoke(cli.app, list(args))
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("command", [["me"], ["lines"], ["usage"], ["invoices", "claims"]])
def test_read_commands(command):
    _run(*command)


def test_invoices_list():
    invoices = _run("invoices", "list")["invoices"]
    assert invoices
    assert all(inv["amount"] is not None and inv["id"] for inv in invoices)


def test_invoices_download_latest(tmp_path):
    [saved] = _run("invoices", "download", "latest", "--dir", str(tmp_path))["invoices"]
    data = Path(saved["file"]).read_bytes()
    assert saved["status"] == "downloaded"
    assert data.startswith(b"%PDF-") and len(data) == saved["bytes"]

"""`invoices download` against a mock DXL. All values are synthetic."""

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from vodafone import api, cli, config
from vodafone.client import DxlClient

ACCOUNT = "100000001"
PDF = b"%PDF-1.7\nsynthetic invoice\n%%EOF\n"


def _hash(n: int) -> str:
    return f"{n:064x}"


def _bill(bill_no: str, month: int, n: int, *, account: str = ACCOUNT, host: str = "api.dxl.local.vodafone.es") -> dict:
    return {
        "billNo": bill_no,
        "creationDate": f"2026-{month:02d}-11T09:30",
        "status": "ready",
        "billingCycle": {"billingYear": "2026", "billingMonth": str(month)},
        "billingAccount": {"id": account},
        "documents": [{"links": {"download": {"href": f"https://{host}/v2/download/{_hash(n)}", "method": "GET"}}}],
    }


# Oldest first, as the API returns them.
BILLS = [_bill("B0000000000008", 8, 8), _bill("B0000000000009", 9, 9), _bill("B0000000000010", 10, 10)]


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({
        "accessToken": "A" * 28,
        "userCsrfToken": "c" * 36,
        "accessTokenExpirationTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }))
    return path


@pytest.fixture
def dxl(monkeypatch):
    """Mock DXL. Tweak state["bills"] / state["pdf"][hash]; inspect downloads, sleeps and bill_params."""
    state = {"bills": list(BILLS), "pdf": {}, "downloads": [], "sleeps": [], "bill_params": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/customer/customerAccounts":
            return httpx.Response(200, json={"items": [{"id": ACCOUNT}]})
        if path == "/v3/payment/customerBills":
            state["bill_params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": state["bills"]})
        if path.startswith("/v2/download/"):
            digest = path.rsplit("/", 1)[1]
            state["downloads"].append(digest)
            return state["pdf"].get(digest) or httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        raise AssertionError(f"unexpected request {path}")

    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api.time, "sleep", state["sleeps"].append)
    return state


def _download(tokens_file, out_dir, *args):
    return CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "invoices", "download", *args, "--dir", str(out_dir)])


def test_latest_downloads_the_newest_bill(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["invoices"] == [{
        "id": "B0000000000010", "month": "2026-10", "file": str(out_dir / "factura_2026-10.pdf"),
        "bytes": len(PDF), "status": "downloaded",
    }]
    assert (out_dir / "factura_2026-10.pdf").read_bytes() == PDF
    assert dxl["bill_params"] == {"type": "bill"}
    assert dxl["downloads"] == [_hash(10)]


def test_all_runs_newest_first_spaced_and_skips_existing_files(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "factura_2026-09.pdf").write_bytes(b"%PDF- already here")

    result = _download(tokens_file, out_dir, "all")

    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)["invoices"]
    assert [(r["month"], r["status"]) for r in rows] == [
        ("2026-10", "downloaded"), ("2026-09", "skipped"), ("2026-08", "downloaded"),
    ]
    assert dxl["downloads"] == [_hash(10), _hash(8)]
    assert dxl["sleeps"] == [config.DOWNLOAD_SPACING]  # between the two real downloads only
    assert (out_dir / "factura_2026-09.pdf").read_bytes() == b"%PDF- already here"


def test_download_by_id_and_unknown_id(tokens_file, dxl, tmp_path):
    by_id = _download(tokens_file, tmp_path, "B0000000000008")
    assert by_id.exit_code == 0
    assert json.loads(by_id.stdout)["invoices"][0]["file"].endswith("factura_2026-08.pdf")

    unknown = _download(tokens_file, tmp_path, "B9999999999999")
    assert unknown.exit_code == 2
    assert json.loads(unknown.stderr)["error"]["type"] == "usage"


def test_bills_sharing_a_month_get_bill_number_suffixes(tokens_file, dxl, tmp_path):
    dxl["bills"] = [_bill("B000000000000A", 10, 1), _bill("B000000000000B", 10, 2)]

    result = _download(tokens_file, tmp_path, "all")

    names = sorted(r["file"].rsplit("/", 1)[1] for r in json.loads(result.stdout)["invoices"])
    assert names == ["factura_2026-10_B000000000000A.pdf", "factura_2026-10_B000000000000B.pdf"]


def test_bills_of_other_accounts_are_ignored(tokens_file, dxl, tmp_path):
    dxl["bills"] = [*BILLS, _bill("B0000000000011", 11, 11, account="999999999")]

    result = _download(tokens_file, tmp_path, "all")

    assert "B0000000000011" not in {r["id"] for r in json.loads(result.stdout)["invoices"]}


def test_a_non_pdf_response_is_rejected_without_writing(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    dxl["pdf"][_hash(10)] = httpx.Response(200, json={"message": "not a pdf"})

    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["type"] == "api_changed"
    assert list(out_dir.iterdir()) == []  # no PDF and no .part file


def test_a_failure_mid_run_stops_and_reports_what_was_saved(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    dxl["pdf"][_hash(9)] = httpx.Response(500, json={"message": "stalled"})

    result = _download(tokens_file, out_dir, "all")

    assert result.exit_code == 5
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "upstream_unavailable"
    assert "B0000000000009" in error["message"] and "factura_2026-10.pdf" in error["message"]
    assert re.search(r"[0-9a-f]{64}", result.stderr) is None  # download tokens never reach errors
    assert (out_dir / "factura_2026-10.pdf").exists()
    assert dxl["downloads"] == [_hash(10), _hash(9)]  # stopped; August was never requested


def test_a_download_link_off_the_api_host_is_rejected(tokens_file, dxl, tmp_path):
    dxl["bills"] = [_bill("B0000000000010", 10, 10, host="evil.example")]

    result = _download(tokens_file, tmp_path, "latest")

    assert result.exit_code == 7
    assert dxl["downloads"] == []

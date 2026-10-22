"""`invoices download` against a mock DXL. All values are synthetic."""

import errno
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from vodafone import api, cli, config
from vodafone.client import DxlClient
from vodafone.errors import UsageError

ACCOUNT = "100000001"
OTHER_ACCOUNT = "100000002"
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
    state = {"accounts": [ACCOUNT], "bills": list(BILLS), "pdf": {}, "downloads": [], "sleeps": [], "bill_params": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/customer/customerAccounts":
            return httpx.Response(200, json={"items": [{"id": a} for a in state["accounts"]]})
        if path == "/v3/payment/customerBills":
            state["bill_params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": state["bills"]})
        if path.startswith("/v2/download/"):
            digest = path.rsplit("/", 1)[1]
            state["downloads"].append(digest)
            reply = state["pdf"].get(digest)
            reply = reply() if callable(reply) else reply
            return reply or httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
        raise AssertionError(f"unexpected request {path}")

    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api.time, "sleep", state["sleeps"].append)
    return state


def _download(tokens_file, out_dir, *args, account=None):
    root = ["--tokens", str(tokens_file), *(["--account", account] if account else [])]
    return CliRunner().invoke(cli.app, [*root, "invoices", "download", *args, "--dir", str(out_dir)])


def _tree(root):
    """Every path under `root`, as relative strings (symlinks not followed)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_latest_downloads_the_newest_bill(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["invoices"] == [{
        "id": "B0000000000010", "month": "2026-10", "file": str(out_dir / "factura_2026-10_B0000000000010.pdf"),
        "bytes": len(PDF), "status": "downloaded",
    }]
    assert (out_dir / "factura_2026-10_B0000000000010.pdf").read_bytes() == PDF
    assert _tree(out_dir) == ["factura_2026-10_B0000000000010.pdf"]  # no temp file left behind
    assert dxl["bill_params"] == {"type": "bill"}
    assert dxl["downloads"] == [_hash(10)]


def test_all_runs_newest_first_spaced_and_skips_existing_files(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "factura_2026-09_B0000000000009.pdf").write_bytes(b"%PDF- already here")

    result = _download(tokens_file, out_dir, "all")

    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)["invoices"]
    assert [(r["month"], r["status"]) for r in rows] == [
        ("2026-10", "downloaded"), ("2026-09", "skipped"), ("2026-08", "downloaded"),
    ]
    assert dxl["downloads"] == [_hash(10), _hash(8)]
    assert dxl["sleeps"] == [config.DOWNLOAD_SPACING]  # between the two real downloads only
    assert (out_dir / "factura_2026-09_B0000000000009.pdf").read_bytes() == b"%PDF- already here"


def test_download_by_id_and_unknown_id(tokens_file, dxl, tmp_path):
    by_id = _download(tokens_file, tmp_path, "B0000000000008")
    assert by_id.exit_code == 0
    assert json.loads(by_id.stdout)["invoices"][0]["file"].endswith("factura_2026-08_B0000000000008.pdf")

    unknown = _download(tokens_file, tmp_path, "B9999999999999")
    assert unknown.exit_code == 2
    assert json.loads(unknown.stderr)["error"]["type"] == "usage"


def test_bills_sharing_a_month_get_distinct_files(tokens_file, dxl, tmp_path):
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
    assert "B0000000000009" in error["message"] and "factura_2026-10_B0000000000010.pdf" in error["message"]
    assert re.search(r"[0-9a-f]{64}", result.stderr) is None  # download tokens never reach errors
    assert (out_dir / "factura_2026-10_B0000000000010.pdf").exists()
    assert dxl["downloads"] == [_hash(10), _hash(9)]  # stopped; August was never requested


def test_a_download_link_off_the_api_host_is_rejected(tokens_file, dxl, tmp_path):
    dxl["bills"] = [_bill("B0000000000010", 10, 10, host="evil.example")]

    result = _download(tokens_file, tmp_path, "latest")

    assert result.exit_code == 7
    assert dxl["downloads"] == []


def test_two_accounts_with_a_bill_in_the_same_month_never_share_a_file(tokens_file, dxl, tmp_path):
    dxl["accounts"] = [ACCOUNT, OTHER_ACCOUNT]
    dxl["bills"] = [_bill("B00000000000A1", 10, 1), _bill("B00000000000B1", 10, 2, account=OTHER_ACCOUNT)]
    dxl["pdf"][_hash(2)] = httpx.Response(200, content=b"%PDF- account B", headers={"content-type": "application/pdf"})

    first = _download(tokens_file, tmp_path, "latest", account=ACCOUNT)
    second = _download(tokens_file, tmp_path, "latest", account=OTHER_ACCOUNT)

    [a] = json.loads(first.stdout)["invoices"]
    [b] = json.loads(second.stdout)["invoices"]
    assert (a["status"], b["status"]) == ("downloaded", "downloaded")
    assert a["file"] != b["file"] and b["id"] == "B00000000000B1"
    assert Path(b["file"]).read_bytes() == b"%PDF- account B"
    assert dxl["downloads"] == [_hash(1), _hash(2)]


@pytest.mark.parametrize(
    ("cycle", "creation", "name"),
    [
        ({"billingYear": "x/../../escaped", "billingMonth": "02"}, "2001-02-03T09:30", "factura_2001-02_B0000000000010.pdf"),
        ({"billingYear": "2001", "billingMonth": "10/../../../escaped"}, "2001-02-03T09:30", "factura_2001-02_B0000000000010.pdf"),
        ({"billingYear": "2026", "billingMonth": "13"}, "../../escaped", "factura_B0000000000010.pdf"),
        ({"billingYear": "\uff12\uff10\uff12\uff16", "billingMonth": "\u0661\u0660"}, None, "factura_B0000000000010.pdf"),
        (None, "2026-1/../../x", "factura_B0000000000010.pdf"),
    ],
)
def test_untrusted_dates_never_shape_the_path(tokens_file, dxl, tmp_path, cycle, creation, name):
    out_dir = tmp_path / "out"
    (out_dir / "factura_x").mkdir(parents=True)  # lets a traversal resolve, as in the report
    bill = _bill("B0000000000010", 10, 10)
    bill["billingCycle"], bill["creationDate"] = cycle, creation
    dxl["bills"] = [bill]
    outside = _tree(tmp_path)

    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["invoices"][0]["file"] == str(out_dir / name)
    assert sorted(set(_tree(tmp_path)) - set(outside)) == [f"out/{name}"]


@pytest.mark.parametrize("bill_no", ["../../escaped", "x/../../escaped", "..", ".hidden", "a\\b", "", "B0000000000010\n"])
def test_an_unsafe_bill_number_is_rejected_before_any_download(tokens_file, dxl, tmp_path, bill_no):
    out_dir = tmp_path / "out"
    dxl["bills"] = [_bill(bill_no, 10, 10)]
    before = _tree(tmp_path)

    result = _download(tokens_file, out_dir, "all")

    assert result.exit_code == 7
    assert json.loads(result.stderr)["error"]["type"] == "api_changed"
    assert dxl["downloads"] == []
    assert _tree(tmp_path) == before


def test_a_precreated_temp_symlink_is_never_written_through(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    victim = tmp_path / "unrelated.txt"
    victim.write_text("keep me")
    for stale in (".factura_2026-10_B0000000000010.pdf.part", ".factura_2026-10.pdf.part"):
        (out_dir / stale).symlink_to(victim)

    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert victim.read_text() == "keep me"
    saved = out_dir / "factura_2026-10_B0000000000010.pdf"
    assert saved.is_file() and not saved.is_symlink() and saved.read_bytes() == PDF


def test_an_existing_symlink_at_the_destination_is_skipped_not_followed(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = tmp_path / "elsewhere.pdf"  # dangling: writing through the link would create it
    (out_dir / "factura_2026-10_B0000000000010.pdf").symlink_to(target)

    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["invoices"][0]["status"] == "skipped"
    assert not target.exists() and dxl["downloads"] == []


def test_a_file_that_appears_during_the_download_is_not_overwritten(tokens_file, dxl, tmp_path):
    out_dir = tmp_path / "out"
    dest = out_dir / "factura_2026-10_B0000000000010.pdf"

    def racing_writer():
        dest.write_bytes(b"%PDF- written meanwhile")
        return None  # then serve the normal PDF

    dxl["pdf"][_hash(10)] = racing_writer

    result = _download(tokens_file, out_dir, "latest")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["invoices"][0]["status"] == "skipped"
    assert dest.read_bytes() == b"%PDF- written meanwhile"
    assert _tree(out_dir) == [dest.name]


def test_without_hard_links_nothing_is_published_and_a_retry_recovers(tokens_file, dxl, tmp_path, monkeypatch):
    real_link = os.link

    def no_links(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    out_dir = tmp_path / "out"

    refused = _download(tokens_file, out_dir, "latest")

    assert refused.exit_code == 2
    error = json.loads(refused.stderr)["error"]
    assert error["type"] == "usage" and "EPERM" in error["message"] and "--dir" in error["message"]
    assert _tree(out_dir) == []  # no final name, no temp file

    monkeypatch.setattr(os, "link", real_link)
    retried = _download(tokens_file, out_dir, "latest")
    assert json.loads(retried.stdout)["invoices"][0]["status"] == "downloaded"
    assert (out_dir / "factura_2026-10_B0000000000010.pdf").read_bytes() == PDF


class _Killed(BaseException):
    """Stands in for SIGKILL or power loss: nothing after it runs, including cleanup."""


@pytest.mark.parametrize("kill_at", ["fsync", "link", "write_after_link_refused"])
def test_an_interrupted_save_leaves_no_final_file_and_the_next_run_downloads_it(tokens_file, dxl, tmp_path, monkeypatch, kill_at):
    out_dir = tmp_path / "out"
    final = out_dir / "factura_2026-10_B0000000000010.pdf"
    real = {"fsync": os.fsync, "link": os.link, "unlink": os.unlink, "fdopen": os.fdopen}

    def killed(*args, **kwargs):
        raise _Killed

    if kill_at == "write_after_link_refused":
        # Codex's reproduction: no hard links, then killed while writing any further file.
        def no_links(src, dst):
            raise OSError(errno.EPERM, "Operation not permitted")

        opened = []

        def fdopen(fd, *args, **kwargs):
            opened.append(fd)
            if len(opened) > 1:  # anything written after the temp file
                raise _Killed
            return real["fdopen"](fd, *args, **kwargs)

        monkeypatch.setattr(os, "link", no_links)
        monkeypatch.setattr(os, "fdopen", fdopen)
    else:
        monkeypatch.setattr(os, kill_at, killed)
    monkeypatch.setattr(os, "unlink", lambda *args, **kwargs: None)  # a killed process cleans nothing up
    with pytest.raises((_Killed, UsageError)):
        api.run_with_client(tokens_file, lambda client: api.download_invoices(client, "latest", out_dir))

    assert not final.exists() and not final.is_symlink()  # at most an orphaned hidden .part file

    for name, fn in real.items():
        monkeypatch.setattr(os, name, fn)
    result = _download(tokens_file, out_dir, "latest")
    assert json.loads(result.stdout)["invoices"][0]["status"] == "downloaded"  # not skipped as "complete"
    assert final.read_bytes() == PDF


def test_the_final_name_only_ever_appears_complete(tokens_file, dxl, tmp_path, monkeypatch):
    real_link = os.link
    links = []

    def checked_link(src, dst):
        links.append((Path(src).read_bytes(), os.path.lexists(dst)))
        real_link(src, dst)

    monkeypatch.setattr(os, "link", checked_link)
    result = _download(tokens_file, tmp_path / "out", "all")

    assert result.exit_code == 0, result.stderr
    assert links == [(PDF, False)] * 3  # complete content, linked only onto a free name


def test_a_non_pdf_reply_never_echoes_its_content_type(tokens_file, dxl, tmp_path):
    access = "A" * 28
    dxl["pdf"][_hash(10)] = httpx.Response(200, content=b"nope", headers={"content-type": f"text/plain; x={access}"})

    result = _download(tokens_file, tmp_path / "out", "latest")

    assert result.exit_code == 7
    message = json.loads(result.stderr)["error"]["message"]
    assert "(plain text)" in message and access not in message and "x=" not in message

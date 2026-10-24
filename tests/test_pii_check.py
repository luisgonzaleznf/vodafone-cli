"""The personal-data check (.githooks/pii_check.py): patterns, known values, modes, agent hook, git hooks.

No network: the model layer is stubbed, and urlopen fails the test if anything reaches it. Every
PII-shaped value is generated at runtime (fake_digits) or is an obviously synthetic shape, so this file
never holds a realistic value and passes the check itself.
"""

import base64
import importlib.util
import io
import json
import os
import random
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / ".githooks"
GMAIL = "gm" + "ail.com"


def load():
    spec = importlib.util.spec_from_file_location("pii_check", HOOKS / "pii_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["pii_check"] = module  # dataclasses look the module up while it executes
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pc(monkeypatch, tmp_path):
    """A fresh module (empty caches), no needles file, no API key, no `claude` CLI and no network."""
    module = load()
    monkeypatch.setenv(module.NEEDLES_ENV, str(tmp_path / "no-needles.json"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(module, "_api_key", lambda: None)
    monkeypatch.setattr(module, "_claude_cli", lambda: None)

    def no_network(*args, **kwargs):
        raise AssertionError("the tests must not reach the network")

    monkeypatch.setattr(module.urllib.request, "urlopen", no_network)
    return module


def fake_digits(n: int, seed: int) -> str:
    """n invented digits that aren't an obviously synthetic shape, so the pattern layer must flag them."""
    pc = load()
    rng = random.Random(seed)
    while True:
        digits = "".join(rng.choice("0123456789") for _ in range(n))
        if not pc.synthetic(digits):
            return digits


def with_nif_letter(number: str) -> str:
    return number + "TRWAGMYFPDXBNJZSQVHLCKE"[int(number) % 23]


def iban_es(bban: str) -> str:
    rearranged = "".join(str(int(c, 36)) for c in bban + "ES00")
    return f"ES{98 - int(rearranged) % 97:02d}{bban}"


def ccc(entity: str, office: str, account: str) -> str:
    pc = load()
    return f"{entity}{office}{pc._ccc_digit('00' + entity + office)}{pc._ccc_digit(account)}{account}"


MOBILE = "6" + fake_digits(8, 1)
LANDLINE = "91" + fake_digits(7, 2)
NIF = with_nif_letter(fake_digits(8, 3))
NIE_DIGITS = fake_digits(7, 4)
NIE = "Y" + NIE_DIGITS + "TRWAGMYFPDXBNJZSQVHLCKE"[int("1" + NIE_DIGITS) % 23]
IBAN = iban_es(fake_digits(20, 5))
CCC_NUMBER = ccc(fake_digits(4, 6), fake_digits(4, 7), fake_digits(10, 8))
INVOICE = "AB12-" + fake_digits(9, 9)
US_LINE = fake_digits(4, 10)
EMAIL = f"ana.ejemplo.{fake_digits(3, 11)}@{GMAIL}"
FLAGGED = {
    "mobile": f"llama al {MOBILE}",
    "mobile 3-3-3": f"tel. {MOBILE[:3]} {MOBILE[3:6]} {MOBILE[6:]}",
    "mobile 3-2-2-2": f"tel. {MOBILE[:3]} {MOBILE[3:5]} {MOBILE[5:7]} {MOBILE[7:]}",
    "+34": f"+34 {MOBILE}",
    "+34 grouped": f"+34-{MOBILE[:3]}-{MOBILE[3:6]}-{MOBILE[6:]}",
    "0034": f"0034{MOBILE}",
    "landline": f"fijo: {LANDLINE}",
    "+1": f"+1 (415) 8{fake_digits(2, 12)}-{US_LINE}",
    "+1 compact": f"+14158{fake_digits(2, 13)}{US_LINE}",
    "DNI": f"NIF {NIF}",
    "DNI dash": f"DNI {NIF[:8]}-{NIF[8]}",
    "NIE": f"NIE {NIE}",
    "IBAN": f"IBAN {IBAN}",
    "IBAN grouped": "IBAN " + " ".join(IBAN[i:i + 4] for i in range(0, 24, 4)),
    "CCC": f"cuenta {CCC_NUMBER}",
    "email": f"escribe a {EMAIL}",
    "invoice": f"factura {INVOICE}",
    "street": "vive en Calle de " + "Alcalá 42",
    "avenue": "Avda. de la " + "Constitución, 7",
    "C/": "C/ " + "Mayor 3",
    "caps street": "CL " + "MAYOR 12, 3 B",
    "postal code": "28001 " + "Madrid",
}
NOT_FLAGGED = {
    "synthetic phones": "600000000 600000001 612345678 600123456 699999999 600000999",
    "service number": "900" + "123123",
    "hex hash": "sha256:a6" + "71829304b",
    "timestamp": "expires_at 1700000000",
    "decimal": "x = 6.28318530",
    "longer run": "id 1" + MOBILE + "0",
    "DNI bad letter": "NIF " + NIF[:8] + ("A" if NIF[8] != "A" else "B"),
    "DNI synthetic": "00000000T 12345678Z",
    "example IBAN": "ES9121000418450200051332",
    "IBAN bad check": "ES00" + IBAN[4:],
    "CCC bad check digits": "cuenta " + CCC_NUMBER[:8] + "99" + CCC_NUMBER[10:],
    "placeholder emails": "user@example.com you@example.org a@b.co git@github.com x@vodafone-cli.git",
    "role emails": f"support@{GMAIL} noreply@anthropic.com 1+luis@users.noreply.github.com",
    "invoice synthetic": "AB12-012345678 XX00-000000001",
    "placeholder street": "Calle Falsa 123",
    "postal placeholder": "12345 Ejemplo",
    "masked number": "6*****789 (pattern layer leaves masked numbers to the known-values layer)",
    "allow marker": f"tel {MOBILE}  # pii:allow synthetic fixture",
    "prose": "Plaza de toros, the Madrid office and invoice numbers stay out of git.",
}


# --- patterns ------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(FLAGGED))
def test_pattern_flags(pc, case):
    assert pc.scan_line(FLAGGED[case]), case


@pytest.mark.parametrize("case", sorted(NOT_FLAGGED))
def test_pattern_allows_synthetic_and_unrelated(pc, case):
    assert pc.scan_line(NOT_FLAGGED[case]) == [], case


def test_pattern_kinds(pc):
    assert pc.scan_line(FLAGGED["mobile"]) == ["phone number"]
    assert pc.scan_line(FLAGGED["NIE"]) == ["DNI/NIF/NIE"]
    assert pc.scan_line(FLAGGED["IBAN"]) == ["IBAN"]
    assert pc.scan_line(FLAGGED["invoice"]) == ["invoice number"]
    assert pc.scan_line(f"{FLAGGED['street']}, {FLAGGED['postal code']}") == ["street address", "postal code + town"]


@pytest.mark.parametrize("path", ["capture.har", "x/.local-secrets/tokens.json", "tokens.json",
                                  "tokens.before-refresh.json", "Factura.PDF", "lines.csv", "export.xlsx",
                                  "dump.jsonl", "bills.zip", ".env", ".env.local", "pii-needles.json"])
def test_blocked_paths(pc, path):
    assert pc.blocked_path(path)


@pytest.mark.parametrize("path", ["src/vodafone/tokens.py", "tests/test_tokens.py", "README.md", ".env.example",
                                  "uv.lock", "Formula/vodafone.rb"])
def test_allowed_paths(pc, path):
    assert not pc.blocked_path(path)


def test_self_test_passes(pc):
    assert pc.self_test() == 0


# --- known values (needles) ----------------------------------------------------------------------

# A fake account. Values are synthetic shapes (the pattern layer passes them), which is the point: the
# known-values layer must catch the real account's values even when they look invented.
FAKE_PHONES = ("600000123", "699999456", "910000078")
FAKE_ME = {"accounts": [{
    "id": "100000777", "status": "Active", "address": "CL FALSA 123, 00000 VILLAEJEMPLO",
    "holder": {"first_name": "ANA", "family_name": "EJEMPLO PRUEBA", "email": "ana.ejemplo@example.com",
               "msisdn": FAKE_PHONES[0]},
}]}
FAKE_LINES = {"account_id": "100000777", "lines": [
    {"id": FAKE_PHONES[0], "kind": "mobile", "type": "Mobile postpaid", "service_code": "PLAN1", "tariff": None},
    {"id": FAKE_PHONES[2], "kind": "landline", "type": "landline", "service_code": "FIJO1", "tariff": None},
    {"id": "FIB000000777", "kind": "fibre", "type": "Fibre", "service_code": "FIB1", "tariff": None},
]}
FAKE_INVOICES = {"account_id": "100000777", "invoices": [
    {"id": "AB12-000000777", "number": "AB12-000000777", "billing_account_id": "100000777", "status": "paid",
     "type": "bill", "issued_at": "2026-01-01", "due_at": "2026-01-15", "amount": 10.0, "net_amount": 8.26,
     "currency": "EUR"},
]}
FAKE_CLAIMS = {"sub": "100000555", "userProfile": {
    "id": "100000555", "userName": FAKE_PHONES[1], "firstName": "ANA", "lastName": "EJEMPLO PRUEBA",
    "email": "ANA.EJEMPLO@EXAMPLE.COM", "msisdn": FAKE_PHONES[1], "document": {"id": "00000000T", "type": "NIF"},
    "sites": [{"id": "100000777"}, {"id": "100000888"}],
    "services": [{"id": FAKE_PHONES[0], "type": "Mobile postpaid", "customerAccountId": "100000777"},
                 {"id": FAKE_PHONES[1], "type": "Mobile postpaid", "customerAccountId": "100000888"}],
}}
FAKE_VALUES = ["100000777", "100000888", "100000555", *FAKE_PHONES, "FIB000000777", "AB12-000000777",
               "ana.ejemplo@example.com", "00000000T", "EJEMPLO PRUEBA", "VILLAEJEMPLO"]


def fake_jwt(claims: dict) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    """A fake `vodafone` on PATH that prints the fixture JSON, a token file with a fake JWT, and the
    needles file path. Returns that path after `needles build` ran."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "outputs.json").write_text(json.dumps({"me": FAKE_ME, "lines": FAKE_LINES, "invoices": FAKE_INVOICES}))
    script = bin_dir / "vodafone"
    script.write_text(f"""#!{sys.executable}
import json, sys
out = json.load(open({str(tmp_path / 'outputs.json')!r}))
args = [a for a in sys.argv[1:] if a != "--account" and not a.isdigit()]
print(json.dumps(out["me" if args == ["me"] else "lines" if args == ["lines"] else "invoices"], indent=2))
""")
    script.chmod(0o755)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"accessToken": "x", "userCsrfToken": "y", "jwt": [fake_jwt(FAKE_CLAIMS)]}))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VODAFONE_TOKENS_FILE", str(tokens))
    needles = tmp_path / "config" / "pii-needles.json"
    monkeypatch.setenv("VODAFONE_PII_NEEDLES", str(needles))
    return needles


def test_needles_build(pc, fake_cli, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    out = capsys.readouterr()
    assert stat.S_IMODE(fake_cli.stat().st_mode) == 0o600
    stored = fake_cli.read_text()
    for value in FAKE_VALUES + ["ana", "ejemplo", "prueba", "falsa", "000000777"]:
        assert value.lower() not in stored.lower()
        assert value.lower() not in (out.out + out.err).lower()
    for kind in ("phone number", "masked phone number", "DNI/NIF/NIE", "account, site or service id",
                 "invoice number", "email", "holder name", "address"):
        assert kind in out.out
    assert "NO HIT" not in out.out and out.err == ""
    assert pc.main(["pii_check.py", "needles", "status"]) == 0


@pytest.mark.parametrize("text, kind", [
    (f"tel {FAKE_PHONES[0]}", "phone number"),
    ("+34 699 99 94 56", "phone number"),
    ("0034 910 000 078", "phone number"),
    ("sms enviado al 6*****123", "masked phone number"),
    ("acaba en ***9456", "masked phone number"),
    ("NIF 00000000-T", "DNI/NIF/NIE"),
    ("/customerAccounts/100000888/bills", "account, site or service id"),
    ("service fib000000777", "account, site or service id"),
    ("bill AB12-000000777.pdf", "invoice number"),
    ("ana.ejemplo@example.com", "email"),
    ("Ana Ejemplo signed the contract", "holder name"),
    ("the holder is Prueba", "holder name"),
    ("Calle Falsa, 123", "address"),
])
def test_needles_find_known_values(pc, fake_cli, text, kind):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert f"known account value: {kind}" in [f.kind for f in pc.known_values([(1, text)], "t")]


@pytest.mark.parametrize("text", ["tel 600000124", "6*****124", "600000001 6*****001", "Lucía Inventada", "Calle Ficticia 124",
                                  "AB12-000000778", "otra@example.com", "100000999"])
def test_needles_ignore_other_values(pc, fake_cli, text):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert pc.known_values([(1, text)], "t") == []


def test_needles_ignore_allow_marker(pc, fake_cli):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    assert pc.known_values([(1, f"tel {FAKE_PHONES[0]}  # pii:allow")], "t")


def test_maintainer_handle_words_only_count_in_pairs(pc):
    tokens = pc.needle_tokens([("first", "Lucía"), ("family", "González Ejemplo")])
    assert "w:gonzalez" not in tokens and "w:lucia" in tokens and "w:ejemplo" in tokens
    assert tokens["p:lucia gonzalez"] == "name"


def test_needles_missing_file_is_a_note(pc, capsys):
    assert pc.known_values([(1, "anything")], "t") == []
    assert "known-values check skipped" in capsys.readouterr().err


def test_needles_build_fails_without_cli(pc, monkeypatch, capsys):
    monkeypatch.setattr(pc.shutil, "which", lambda name: None)
    assert pc.main(["pii_check.py", "needles", "build"]) == 1
    assert "isn't on PATH" in capsys.readouterr().err


# --- modes and exit codes ------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    shutil.copytree(HOOKS, path / ".githooks")
    return path


@pytest.fixture
def model(pc, monkeypatch):
    """Stub the model layer; records the blocks it was asked about and returns `model.result`."""

    class Stub:
        calls: list = []
        result: tuple = ([], None)

        def __call__(self, blocks):
            self.calls.append(blocks)
            return self.result

    stub = Stub()
    stub.calls = []
    monkeypatch.setattr(pc, "model_check", stub)
    return stub


def test_staged_blocks_pattern_hit_without_asking_the_model(pc, model, repo, monkeypatch, capsys):
    (repo / "notes.md").write_text(f"ok line\n{FLAGGED['mobile']}\n")
    git(repo, "add", "notes.md")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    err = capsys.readouterr().err
    assert "notes.md:2  phone number" in err and MOBILE not in err
    assert model.calls == []


def test_staged_clean_asks_the_model_about_added_lines(pc, model, repo, monkeypatch):
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "--no-verify", "-m", "base")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    git(repo, "add", "a.py")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 0
    assert model.calls == [[("a.py", [(2, "y = 2")])]]


def test_staged_model_finding_blocks(pc, model, repo, monkeypatch, capsys):
    (repo / "a.md").write_text("La titular es Ana Ejemplo.\n")
    git(repo, "add", "a.md")
    model.result = ([pc.Finding("a.md:1", "holder name (model)")], None)
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "a.md:1  holder name (model)" in capsys.readouterr().err


def test_staged_blocks_data_files(pc, model, repo, monkeypatch, capsys):
    (repo / "session.har").write_text("{}\n")
    git(repo, "add", "session.har")
    monkeypatch.chdir(repo)
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "session.har  data file" in capsys.readouterr().err


def test_staged_known_value(pc, model, repo, fake_cli, monkeypatch, capsys):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    (repo / "test_x.py").write_text('MASKED = "6*****456"  # pii:allow looks synthetic\n')
    git(repo, "add", "test_x.py")
    monkeypatch.chdir(repo)
    capsys.readouterr()
    assert pc.main(["pii_check.py", "staged"]) == 2
    assert "test_x.py:1  known account value: masked phone number" in capsys.readouterr().err
    assert model.calls == []


def test_msg_mode_skips_comments_and_the_diff_below_the_scissors(pc, model, tmp_path, capsys):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"Fix lines\n\n# {FLAGGED['mobile']}\n# ------------------------ >8 ------------------------\n"
                   f"+{FLAGGED['mobile']}\n")
    assert pc.main(["pii_check.py", "msg", str(msg)]) == 0
    assert model.calls == [[("commit message", [(1, "Fix lines"), (2, "")])]]
    msg.write_text(f"Fix lines\n\nReported from {EMAIL}\n")
    assert pc.main(["pii_check.py", "msg", str(msg)]) == 2
    err = capsys.readouterr().err
    assert "commit message:3  email" in err and EMAIL not in err


def test_text_mode(pc, model, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"PR body\n{FLAGGED['IBAN']}\n"))
    assert pc.main(["pii_check.py", "text"]) == 2
    assert "text:2  IBAN" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("PR body\nall good\n"))
    assert pc.main(["pii_check.py", "text"]) == 0
    assert model.calls == [[("text", [(1, "PR body"), (2, "all good")])]]


def test_files_mode_patterns_only_skips_the_model(pc, model, tmp_path):
    f = tmp_path / "a.md"
    f.write_text("nothing here\n")
    assert pc.main(["pii_check.py", "files", "--patterns-only", str(f)]) == 0
    assert model.calls == []
    assert pc.main(["pii_check.py", "files", str(f)]) == 0
    assert len(model.calls) == 1


def test_unknown_mode_prints_usage(pc, capsys):
    assert pc.main(["pii_check.py", "bogus"]) == 64
    assert "Usage" in capsys.readouterr().err


def test_no_flagged_value_is_ever_printed(pc, model, monkeypatch, capsys):
    text = "\n".join(FLAGGED.values())
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert pc.main(["pii_check.py", "text"]) == 2
    out = capsys.readouterr()
    printed = out.out + out.err
    flagged_lines = {int(n) for n in re.findall(r"\n  text:(\d+)  ", printed)}
    assert flagged_lines == set(range(1, len(FLAGGED) + 1))
    for value in (MOBILE, LANDLINE, NIF, NIE, IBAN, CCC_NUMBER, INVOICE, EMAIL, "Alcalá", "Constitución", "MAYOR"):
        assert value not in printed


# --- model layer (stubbed transport) -------------------------------------------------------------


def fake_claude(tmp_path: Path, stdout: dict | str, code: int = 0) -> Path:
    """A fake `claude` that records its argv and cwd, and prints `stdout`."""
    script = tmp_path / "claude"
    payload = stdout if isinstance(stdout, str) else json.dumps(stdout)
    script.write_text(f"""#!{sys.executable}
import json, os, sys
json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": sys.stdin.read(),
           "claudecode": os.environ.get("CLAUDECODE")}}, open({str(tmp_path / 'call.json')!r}, "w"))
print({payload!r})
sys.exit({code})
""")
    script.chmod(0o755)
    return script


def test_cli_path_runs_isolated_and_parses_the_verdict(pc, tmp_path, monkeypatch):
    verdict = {"personal_data_found": True, "findings": [{"location": "a.md:3", "kind": "holder name"}]}
    exe = fake_claude(tmp_path, {"type": "result", "subtype": "success", "is_error": False,
                                 "structured_output": verdict})
    monkeypatch.setattr(pc, "_claude_cli", lambda: str(exe))
    monkeypatch.setenv("CLAUDECODE", "1")
    findings, note = pc.model_check([("a.md", [(3, "La titular es Ana Ejemplo"), (4, "x  # pii:allow")])])
    assert findings == [pc.Finding("a.md:3", "holder name (model)")] and note is None
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert not Path(call["cwd"]).resolve().is_relative_to(ROOT) and call["claudecode"] is None
    assert "Ana Ejemplo" in call["stdin"] and "pii:allow" not in call["stdin"]


@pytest.mark.parametrize("stdout, code", [
    ({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"}, 1),
    ({"type": "result", "subtype": "error_max_turns", "is_error": False}, 0),
    ("not json", 1),
])
def test_cli_path_fails_open(pc, tmp_path, monkeypatch, stdout, code):
    monkeypatch.setattr(pc, "_claude_cli", lambda: str(fake_claude(tmp_path, stdout, code)))
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "the other checks still ran" in note


def test_no_model_available_is_a_note(pc):
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "model check skipped" in note


def test_api_path(pc, monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout):
        seen["body"], seen["headers"] = json.loads(req.data), dict(req.header_items())
        verdict = {"personal_data_found": False, "findings": []}
        return Response(json.dumps({"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": ""}, {"type": "text", "text": json.dumps(verdict)}]}).encode())

    monkeypatch.setattr(pc, "_api_key", lambda: "sk-test")
    monkeypatch.setattr(pc.urllib.request, "urlopen", urlopen)
    assert pc.model_check([("a.md", [(1, "text")])]) == ([], None)
    assert seen["body"]["model"] == "claude-haiku-5-5"
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"
    assert seen["headers"]["X-api-key"] == "sk-test"

    def http_error(req, timeout):
        raise pc.urllib.error.HTTPError(pc.API_URL, 529, "overloaded", {}, None)

    monkeypatch.setattr(pc.urllib.request, "urlopen", http_error)
    findings, note = pc.model_check([("a.md", [(1, "text")])])
    assert findings == [] and "HTTP 529" in note


# --- agent hook ----------------------------------------------------------------------------------


def run_hook(pc, monkeypatch, capsys, payload: dict) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = pc.main(["pii_check.py", "agent-hook"])
    out = capsys.readouterr()
    return code, out.out, out.err


def bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def test_agent_hook_installs_the_git_hooks(pc, model, repo, monkeypatch, capsys):
    assert run_hook(pc, monkeypatch, capsys, bash("ls", repo))[0] == 0
    for name in ("pre-commit", "commit-msg"):
        shim = repo / ".git" / "hooks" / name
        assert pc.SHIM_MARKER in shim.read_text() and os.access(shim, os.X_OK)


def test_agent_hook_leaves_other_hooks_alone(pc, model, repo, monkeypatch, capsys):
    (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\necho mine\n")
    run_hook(pc, monkeypatch, capsys, bash("ls", repo))
    assert (repo / ".git" / "hooks" / "pre-commit").read_text() == "#!/bin/sh\necho mine\n"


@pytest.mark.parametrize("command", ["git commit --no-verify -m x", "git commit -nm x", "git -C . commit -an -m x",
                                     "gh pr create --body-file -"])
def test_agent_hook_refuses(pc, model, repo, monkeypatch, capsys, command):
    code, _, err = run_hook(pc, monkeypatch, capsys, bash(command, repo))
    assert code == 2 and err


@pytest.mark.parametrize("command", ["git commit -m 'msg -n here'", "git commit -am fix", "ls -la", f"echo {EMAIL}"])
def test_agent_hook_allows_without_the_model(pc, model, repo, monkeypatch, capsys, command):
    assert run_hook(pc, monkeypatch, capsys, bash(command, repo))[0] == 0
    assert model.calls == []


@pytest.mark.parametrize("command", [
    f"gh pr create --title t --body 'cc {EMAIL}'",
    f"gh issue comment 3 --body \"llama al {MOBILE}\"",
    f"gh -R luisgonzaleznf/vodafone-cli release create v1 --notes '{FLAGGED['IBAN']}'",
    f"gh api repos/o/r/issues -f body='NIF {NIF}'",
])
def test_agent_hook_blocks_posted_text(pc, model, repo, monkeypatch, capsys, command):
    code, _, err = run_hook(pc, monkeypatch, capsys, bash(command, repo))
    assert code == 2 and "command:1" in err
    for value in (EMAIL, MOBILE, IBAN, NIF):
        assert value not in err


def test_agent_hook_reads_body_files(pc, model, repo, monkeypatch, capsys):
    (repo / "body.md").write_text(f"Summary\n\n{FLAGGED['street']}\n")
    code, _, err = run_hook(pc, monkeypatch, capsys, bash("gh pr edit 4 --body-file body.md", repo))
    assert code == 2 and "body.md:3 (street address)" in err
    (repo / "notes.md").write_text("Release notes\n")
    code, _, _ = run_hook(pc, monkeypatch, capsys, bash("gh release edit v1 --notes-file notes.md", repo))
    assert code == 0
    assert [label for label, _ in model.calls[-1]] == ["command", "notes.md"]


def test_agent_hook_model_verdict_blocks(pc, model, repo, monkeypatch, capsys):
    model.result = ([pc.Finding("command:1", "holder name (model)")], None)
    code, _, err = run_hook(pc, monkeypatch, capsys, bash("gh pr create --body 'Gracias, Ana Ejemplo'", repo))
    assert code == 2 and "holder name (model)" in err


def test_agent_hook_cursor_payload(pc, model, repo, monkeypatch, capsys):
    code, out, _ = run_hook(pc, monkeypatch, capsys, {"command": "git commit --no-verify -m x", "cwd": str(repo)})
    assert code == 2 and json.loads(out)["permission"] == "deny"
    code, out, _ = run_hook(pc, monkeypatch, capsys, {"command": "ls", "cwd": str(repo)})
    assert code == 0 and json.loads(out) == {"permission": "allow"}


def test_agent_hook_ignores_bad_payloads(pc, model, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert pc.main(["pii_check.py", "agent-hook"]) == 0


# --- the git hooks, end to end -------------------------------------------------------------------


def hook_env(tmp_path: Path, needles: Path) -> dict:
    """PATH with python3 and git only (no `claude`), an empty HOME, the given needles file."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "VODAFONE_", "CLAUDE"))}
    env.update(PATH=f"{Path(sys.executable).parent}{os.pathsep}/usr/bin{os.pathsep}/bin", HOME=str(tmp_path / "home"),
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", VODAFONE_PII_NEEDLES=str(needles))
    return env


def commit(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", f"core.hooksPath={repo / '.githooks'}", "commit", *args], cwd=repo, env=env,
                          capture_output=True, text=True)


def test_git_hooks_end_to_end(pc, repo, fake_cli, tmp_path):
    assert pc.main(["pii_check.py", "needles", "build"]) == 0
    env = hook_env(tmp_path, fake_cli)
    (repo / "README.md").write_text("# demo\n")
    git(repo, "add", "README.md")
    clean = commit(repo, env, "-m", "Add a readme")
    assert clean.returncode == 0, clean.stderr
    assert "model check skipped" in clean.stderr  # fails open: no API key, no claude CLI

    blocked_msg = commit(repo, env, "--allow-empty", "-m", f"Fix the line ending in {FAKE_PHONES[1][-3:]}\n\n"
                                                           f"Seen on 6*****{FAKE_PHONES[1][-3:]}")
    assert blocked_msg.returncode != 0 and "commit message:3  known account value" in blocked_msg.stderr

    (repo / "fixture.py").write_text(f'PHONE = "{MOBILE}"\n')
    git(repo, "add", "fixture.py")
    blocked_diff = commit(repo, env, "-m", "Add a fixture")
    assert blocked_diff.returncode != 0 and "fixture.py:1  phone number" in blocked_diff.stderr
    assert MOBILE not in blocked_diff.stdout + blocked_diff.stderr
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"

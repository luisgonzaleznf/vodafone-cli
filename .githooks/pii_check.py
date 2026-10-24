#!/usr/bin/env python3
"""Stop the account holder's personal data before it reaches git or GitHub.

This repository is public and drives one real Vodafone España account; most commits are written by AI
agents that have seen that account's live data. Three layers check the same text (staged changes, a
commit message, PR, issue or release text):

1. Patterns, standard library only, always on: Spanish phone numbers (6xx/7xx/8xx/9xx, with or without
   +34/0034) and +1 ones, DNI/NIF and NIE (checksum letter validated), IBANs (mod-97) and Spanish CCC
   account numbers (check digits), personal emails (any address except example.*, GitHub no-reply and
   role addresses), invoice numbers, Spanish street addresses and postal code + town, and data files
   that never belong in git (*.har, .local-secrets/, tokens*.json, *.pdf, csv/tsv/xlsx/jsonl/zip, ...).
   Obviously synthetic shapes (repeated or sequential digits, the usual example IBANs) are allowed.
2. Known values, always on once built: `needles build` reads the account's real values from the
   installed CLI (`vodafone me`, `lines`, `invoices list`) and the identity JWT, and stores only salted
   SHA-256 hashes of them (phone numbers and their masked forms, NIF, account/site/service ids, invoice
   numbers, emails, holder names, address) in ~/.config/vodafone-cli/pii-needles.json, outside every
   repo. The check hashes candidate tokens of the text and compares, so it catches real values that
   look synthetic, like a masked number in a test that matches a real line. Without the file it's
   skipped with a note.
3. Claude Haiku (`claude-haiku-5-5`) reads the text for what fixed rules can't see (a name, free text
   about the holder's bill). With ANTHROPIC_API_KEY (environment, or the main checkout's gitignored
   .env) it calls the Messages API; otherwise the `claude` CLI on the user's Claude Code login, with no
   tools, settings, hooks or CLAUDE.md. If neither works it prints a note and lets the other layers
   decide: an outage never blocks a commit. It runs only when the other layers found nothing.

No layer prints a matched value. A line containing `pii:allow` is skipped by the pattern and model
layers; the known-values layer ignores the marker, because what it finds is the account's real data.

Usage
  pii_check.py staged          added lines and paths of the staged diff     (.githooks/pre-commit)
  pii_check.py msg <file>      a commit message file                        (.githooks/commit-msg)
  pii_check.py text            text on stdin, e.g. a PR body
  pii_check.py files [--patterns-only] <path>...   whole files (audits, calibration); --patterns-only
                               skips the model (patterns and known values still run)
  pii_check.py agent-hook      an agent's pre-shell-command hook payload on stdin (Claude Code, Codex,
                               Cursor): checks gh pr/issue/release/api text before it's posted, refuses
                               `git commit --no-verify`, and installs the git hooks on first use
  pii_check.py enable          install the git hooks for this clone (humans: once per clone)
  pii_check.py needles build   (re)build the known-values file from the installed `vodafone` CLI
  pii_check.py needles status  where the file is, when it was built, hashes per kind
  pii_check.py --self-test     pattern and known-values layers only, no network

Exit codes: 0 clean, 2 blocked (Claude Code, Codex and Cursor all treat 2 as "deny").
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ALLOW_MARKER = "pii:allow"
MAINTAINER = "luisgonzaleznf"  # the maintainer's public GitHub handle

EMAIL = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
SAFE_DOMAINS = ("example.com", "example.org", "example.net", "example.es", "users.noreply.github.com",
                "noreply.github.com")
SAFE_TLDS = (".example", ".test", ".invalid", ".localhost", ".local")
ROLE_LOCAL_PARTS = {
    "support", "help", "hello", "hi", "info", "contact", "team", "sales", "billing", "security", "privacy",
    "legal", "noreply", "no-reply", "donotreply", "do-not-reply", "notifications", "notification", "admin",
    "postmaster", "abuse", "press", "careers", "jobs", "feedback", "partners", "api", "dev", "developers",
    "ops", "accounts", "receipts", "invoices", "service", "marketing", "dpo", "webmaster", "bot", "alerts",
    "reply", "git", "atencion", "atencioncliente", "atencion.cliente", "clientes", "soporte", "contacto",
    "hola", "facturacion", "privacidad", "prensa", "comercial", "ventas", "reclamaciones",
}
PLACEHOLDER_LOCAL_PARTS = {
    "member", "user", "username", "owner", "someone", "somebody", "name", "first.last", "firstname.lastname",
    "you", "your.name", "yourname", "jane", "john", "jane.doe", "john.doe", "test", "tester", "example",
    "foo", "bar", "email", "client", "customer", "person", "demo", "sample", "holder", "usuario", "nombre",
    "cliente", "titular", "correo", "ejemplo", "prueba", "persona", "ana", "ana.ejemplo",
}

# Spanish numbers: nine digits from 6-9, compact or grouped 3-3-3 / 3-2-2-2, with an optional +34/0034.
ES_PHONE = re.compile(
    r"(?<![\w.*+@-])(?:(?:\+|00)34[ .-]?|\(\+34\)\s?)?"
    r"([6-9]\d{8}|[6-9]\d{2}[ .-]\d{3}[ .-]\d{3}|[6-9]\d{2}[ .-]\d{2}[ .-]\d{2}[ .-]\d{2})"
    r"(?![\w*]|[.,-]\d)"
)
ES_SERVICE_PREFIXES = ("800", "803", "806", "807", "900", "901", "902", "905")  # free-phone and premium lines
US_PHONE = re.compile(
    r"(?<![\w.+-])(?:\+|00)1[ .-]?\(?([2-9]\d{2})\)?[ .-]?([2-9]\d{2})[ .-]?(\d{4})(?![\w-])"
    r"|(?<![\w.+(-])\(([2-9]\d{2})\)[ ]?([2-9]\d{2})[ .-](\d{4})(?![\w-])"
)
US_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}

NIF_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
DNI = re.compile(r"(?<![\w-])(\d{8})-?([A-Za-z])(?![\w-])")
NIE = re.compile(r"(?<![\w-])([XYZxyz])-?(\d{7})-?([A-Za-z])(?![\w-])")

IBAN = re.compile(r"(?<!\w)([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)(?!\w)")
IBAN_LENGTHS = {"ES": 24, "AD": 24, "PT": 25, "FR": 27, "MC": 27, "IT": 27, "DE": 22, "GB": 22, "IE": 22,
                "NL": 18, "BE": 16, "LU": 20, "CH": 21, "AT": 20, "PL": 28, "SE": 24, "DK": 18, "NO": 15,
                "FI": 18}
EXAMPLE_IBANS = {  # the examples banks, ISO 13616 and Spanish documentation use
    "ES9121000418450200051332", "ES7921000813610123456789", "ES7620770024003102575766",
    "ES6000491500051234567892", "GB82WEST12345698765432", "GB29NWBK60161331926819",
    "DE89370400440532013000", "FR1420041010050500013M02606", "NL91ABNA0417164300", "BE68539007547034",
}
CCC = re.compile(r"(?<![\w-])(\d{4})[ -]?(\d{4})[ -]?(\d{2})[ -]?(\d{10})(?![\w-])")  # entity office DC account
CCC_WEIGHTS = (1, 2, 4, 8, 5, 10, 9, 7, 3, 6)

INVOICE = re.compile(r"(?<![\w-])[A-Z]{1,3}\d{1,3}-(\d{8,10})(?![\w-])")

STREET_TYPES = (r"Calle|Avenida|Avda\.?|Av\.|Plaza|Pza\.|Paseo|Ronda|Rda\.|Carretera|Ctra\.|Camino|Travesía|"
                r"Glorieta|Rambla|Vía|Pasaje|Alameda|Bulevar|Callejón|Cuesta|Urbanización|Urb\.")
_STREET_WORD = r"[A-ZÁÉÍÓÚÑÜ][\w'.-]*"
_STREET_LINK = r"(?:de\s+(?:la\s+|las\s+|los\s+)?|del\s+|el\s+|la\s+|y\s+)"
ADDRESS = re.compile(
    rf"(?<![\w/])(?:(?:{STREET_TYPES})\s+|C/\s*){_STREET_LINK}?({_STREET_WORD}(?:\s+{_STREET_LINK}?{_STREET_WORD}){{0,4}})"
    r",?\s+(?:n[º°o]\.?\s*|núm\.?\s*|número\s+)?\d{1,4}(?!\d|[.,]\d)"
)
ADDRESS_CAPS = re.compile(  # the abbreviated, upper-case form billing systems print: CL FALSA 12
    r"(?<![\w/])(?:CL|AV|AVDA|PZ|PS|CTRA|CM|RD|TR|GL|UR|PJ)\.?\s+((?:[A-ZÁÉÍÓÚÑÜ'.-]{2,}\s+){1,5})\d{1,4}(?!\w)"
)
PLACEHOLDER_STREETS = {"falsa", "ejemplo", "example", "fake", "test", "prueba", "inventada", "ficticia",
                       "imaginaria", "sample", "placeholder"}
POSTAL_TOWN = re.compile(  # a postal code (01000-52999) followed by a capitalised town name
    r"(?<![\w.,/-])((?:0[1-9]|[1-4]\d|5[0-2])\d{3}),?\s+\(?([A-ZÁÉÍÓÚÑÜ][a-záéíóúñü]{2,}|[A-ZÁÉÍÓÚÑÜ]{3,})\b"
)
PLACEHOLDER_POSTAL = {"01234", "11111", "12345", "22222", "33333", "44444"}

BLOCKED_EXTS = {"har", "pdf", "csv", "tsv", "xlsx", "xls", "ods", "jsonl", "ndjson", "parquet", "zip", "gz",
                "tgz", "tar", "7z", "cookies", "part", "eml", "msg", "sqlite", "db"}

HINT = ("Replace it with an obviously fake value (600000001, 00000000T, user@example.com, Ana Ejemplo, Calle "
        "Falsa 123) or a placeholder (<phone>, <NIF>); keep captures, PDFs, token files and CLI output out of "
        "git. A known-account-value hit is the real account's data even if it looks synthetic: change the "
        f"value, `{ALLOW_MARKER}` doesn't apply to it. For any other false alarm, show the user the flagged "
        f"lines and ask before adding `{ALLOW_MARKER}` (with a reason) to them.")

Lines = list[tuple[int, str]]
Blocks = list[tuple[str, Lines]]


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str


# --- pattern layer -------------------------------------------------------------------------------

def synthetic(digits: str) -> bool:
    """Obviously invented digits: five equal in a row (600000001) or six in sequence (612345678)."""
    if re.search(r"(\d)\1{4}", digits):
        return True
    return any(digits[i:i + 6] in "0123456789" or digits[i:i + 6] in "9876543210" for i in range(len(digits) - 5))


def _email_ok(local: str, domain: str) -> bool:
    local, domain = local.lower(), domain.lower()
    if any(domain == d or domain.endswith("." + d) for d in SAFE_DOMAINS):
        return True
    if domain.endswith(SAFE_TLDS) or local in ROLE_LOCAL_PARTS or local in PLACEHOLDER_LOCAL_PARTS:
        return True
    if local.split("+")[0] in ROLE_LOCAL_PARTS or "noreply" in local or "no-reply" in local:
        return True
    return len(local) == 1 or bool(re.fullmatch(r"x+|\.+|_+|<.*>|\{.*\}|\$.*", local))  # a@b.com, xxx@, {email}@


def _es_phone_personal(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    return not digits.startswith(ES_SERVICE_PREFIXES) and not synthetic(digits)


def _us_phone_personal(m: re.Match) -> bool:
    area, exch, num = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
    return area not in US_TOLL_FREE and exch != "555" and not synthetic(area + exch + num)


def _nif_valid(number: str, letter: str) -> bool:
    return NIF_LETTERS[int(number) % 23] == letter.upper() and not synthetic(number)


def _iban_personal(raw: str) -> bool:
    iban = raw.replace(" ", "")
    country = iban[:2]
    if len(iban) != IBAN_LENGTHS.get(country) or iban in EXAMPLE_IBANS:
        return False
    if country == "ES" and not iban[2:].isdigit():
        return False
    if int("".join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97 != 1:
        return False
    return not synthetic(re.sub(r"\D", "", iban[4:]))


def _ccc_digit(ten: str) -> int:
    d = 11 - sum(int(c) * w for c, w in zip(ten, CCC_WEIGHTS)) % 11
    return {10: 1, 11: 0}.get(d, d)


def _ccc_personal(entity: str, office: str, dc: str, account: str) -> bool:
    valid = dc == f"{_ccc_digit('00' + entity + office)}{_ccc_digit(account)}"
    return valid and not synthetic(entity + office + dc + account)


def _street_personal(name: str) -> bool:
    return not any(w.lower() in PLACEHOLDER_STREETS for w in re.findall(r"\w+", name))


def scan_line(line: str) -> list[str]:
    """Kinds of PII-looking patterns in one line (never the values)."""
    if ALLOW_MARKER in line:
        return []
    kinds = []
    if any(not _email_ok(m.group(1), m.group(2)) for m in EMAIL.finditer(line)):
        kinds.append("email")
    if (any(_es_phone_personal(m.group(1)) for m in ES_PHONE.finditer(line))
            or any(_us_phone_personal(m) for m in US_PHONE.finditer(line))):
        kinds.append("phone number")
    if (any(_nif_valid(m.group(1), m.group(2)) for m in DNI.finditer(line))
            or any(_nif_valid(str("XYZ".index(m.group(1).upper())) + m.group(2), m.group(3))
                   for m in NIE.finditer(line))):
        kinds.append("DNI/NIF/NIE")
    if any(_iban_personal(m.group(1)) for m in IBAN.finditer(line)):
        kinds.append("IBAN")
    if any(_ccc_personal(*m.groups()) for m in CCC.finditer(line)):
        kinds.append("bank account number")
    if any(not synthetic(m.group(1)) for m in INVOICE.finditer(line)):
        kinds.append("invoice number")
    if any(_street_personal(m.group(1)) for m in (*ADDRESS.finditer(line), *ADDRESS_CAPS.finditer(line))):
        kinds.append("street address")
    if any(m.group(1) not in PLACEHOLDER_POSTAL for m in POSTAL_TOWN.finditer(line)):
        kinds.append("postal code + town")
    return kinds


def numbered(text: str) -> Lines:
    return list(enumerate(text.splitlines(), 1))


def scan_lines(lines: Lines, label: str) -> list[Finding]:
    """Pattern and known-value findings for these lines."""
    return [Finding(f"{label}:{n}", k) for n, line in lines for k in scan_line(line)] + known_values(lines, label)


def scan_text(text: str, label: str) -> list[Finding]:
    return scan_lines(numbered(text), label)


def blocked_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    ext = name.rsplit(".", 1)[1].lower() if "." in name.lstrip(".") else ""
    return (".local-secrets" in parts[:-1] or name == ".local-secrets"
            or bool(re.fullmatch(r"(?i)tokens.*\.json|pii-needles.*\.json", name))
            or name == ".env" or (name.startswith(".env.") and name not in (".env.example", ".env.sample"))
            or ext in BLOCKED_EXTS)


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def scan_staged() -> tuple[list[Finding], Blocks]:
    """Pattern and known-value findings for the staged diff, and its added lines per file for the model."""
    findings, added = [], {}
    fields = git("diff", "--cached", "--name-status", "--no-renames", "-z").split("\0")
    blocked = set()
    for status, path in zip(fields[0::2], fields[1::2]):
        if status and status[0] in "AM" and blocked_path(path):
            findings.append(Finding(path, "data file that never belongs in git"))
            blocked.add(path)
    diff = git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--diff-filter=AM")
    path, lineno, lines = "", 0, {}
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            lines.setdefault(path, []).append((lineno, line[1:]))
            lineno += 1
    for path, file_lines in lines.items():
        findings += scan_lines(file_lines, path)
        if path not in blocked:
            added[path] = file_lines
    return findings, list(added.items())


def message_lines(path: str) -> Lines:
    lines = []
    for n, line in numbered(Path(path).read_text(errors="replace")):
        if re.match(r"# -+ >8 -+$", line):  # `git commit -v`: the diff below the scissors isn't the message
            break
        if not line.startswith("#"):
            lines.append((n, line))
    return lines


# --- known-values layer (needles) ----------------------------------------------------------------

NEEDLES_ENV = "VODAFONE_PII_NEEDLES"
KIND_LABELS = {"phone": "phone number", "masked": "masked phone number", "nif": "DNI/NIF/NIE",
               "id": "account, site or service id", "invoice": "invoice number", "email": "email",
               "name": "holder name", "address": "address"}
# Dropped before word pairs are formed, on both sides, so "Calle de la Prueba 5" pairs as "prueba 5".
STOPWORDS = {"de", "del", "la", "las", "los", "el", "y", "e", "i", "d", "l", "a", "calle", "c", "cl", "avenida",
             "avda", "av", "plaza", "pza", "pz", "paseo", "ps", "ronda", "rda", "rd", "carretera", "ctra", "camino",
             "cm", "travesia", "tr", "glorieta", "gl", "rambla", "via", "pasaje", "pj", "urbanizacion", "urb", "n",
             "no", "num", "numero", "piso", "planta", "puerta", "pta", "esc", "escalera", "bajo", "bis"}
WORD = re.compile(r"[^\W_]+")
DIGIT_GROUPS = re.compile(r"(?<![\d])(?:\+|00)?\d{1,4}(?:[ .()-]{1,2}\d{1,4}){1,6}(?!\d)")
ALNUM_TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
MASKED = re.compile(r"(?<![\d*•xX·])(\d{1,3})?[*•xX·]{2,}(\d{2,4})(?!\d)")

_needles_cache: dict[str, tuple[str, dict[str, str]] | None] = {}


def fold(text: str) -> str:
    """Lower case without accents: José -> jose."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def _national(digits: str) -> str:
    if digits.startswith("0034"):
        return digits[4:]
    if digits.startswith("34") and len(digits) == 11:
        return digits[2:]
    if digits.startswith("001") and len(digits) == 13:
        return digits[3:]
    if digits.startswith("1") and len(digits) == 11:
        return digits[1:]
    return digits


def _digit_tokens(digits: str) -> set[str]:
    return {"d:" + d for d in (digits, _national(digits)) if len(d) >= 5}


def candidates(line: str) -> set[str]:
    """Normalised tokens of one line that a known value could hash to."""
    out: set[str] = set()
    for m in re.finditer(r"\d+", line):
        out |= _digit_tokens(m.group())
    for m in DIGIT_GROUPS.finditer(line):  # 612 34 56 78, +34-612-345-678: every run of adjacent groups
        groups = re.findall(r"\d+", m.group())
        for i in range(len(groups)):
            for j in range(i + 2, len(groups) + 1):
                joined = "".join(groups[i:j])
                if 7 <= len(joined) <= 13:
                    out |= _digit_tokens(joined)
    for m in ALNUM_TOKEN.finditer(line):  # NIF, invoice numbers, alphanumeric service ids
        tok = m.group().upper()
        if len(tok) >= 6 and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            out |= {"a:" + tok, "a:" + tok.replace("-", "")}
    for m in MASKED.finditer(line):  # 6*****789, ***6789
        first, last = m.group(1), m.group(2)
        if first and len(last) >= 3:
            out.add(f"m:{first[0]}|{last[-3:]}")
            if len(last) == 4:
                out.add(f"m:{first[0]}|{last}")
        if len(last) == 4:
            out.add(f"l:{last}")
    for m in EMAIL.finditer(line):
        out.add("e:" + m.group(0).lower())
    words = WORD.findall(fold(line))
    out |= {"w:" + w for w in words if len(w) >= 3 and w.isalpha()}
    kept = [w for w in words if w not in STOPWORDS]
    out |= {f"p:{a} {b}" for a, b in zip(kept, kept[1:])}
    return out


def _hash(salt: str, token: str) -> str:
    return hashlib.sha256(f"{salt}\0{token}".encode()).hexdigest()


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".config") / "vodafone-cli"


def needles_path() -> Path:
    return Path(os.environ[NEEDLES_ENV]).expanduser() if os.environ.get(NEEDLES_ENV) else config_home() / "pii-needles.json"


def load_needles() -> tuple[str, dict[str, str]] | None:
    """(salt, hash -> kind) from the needles file, or None when there is none."""
    path = needles_path()
    key = str(path)
    if key not in _needles_cache:
        try:
            data = json.loads(path.read_text())
            table = {h: kind for kind, hashes in data["kinds"].items() for h in hashes}
            _needles_cache[key] = (str(data["salt"]), table)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            _needles_cache[key] = None
    return _needles_cache[key]


def needle_kinds(line: str, salt: str, table: dict[str, str]) -> set[str]:
    return {table[h] for h in (_hash(salt, c) for c in candidates(line)) if h in table}


_needles_note_shown = False


def known_values(lines: Lines, label: str) -> list[Finding]:
    """Findings for lines holding a known account value. `pii:allow` doesn't apply: a hit is real data."""
    global _needles_note_shown
    needles = load_needles()
    if needles is None:
        if not _needles_note_shown:
            print(f"pii check: known-values check skipped: no {needles_path()} (run `python3 "
                  ".githooks/pii_check.py needles build`)", file=sys.stderr)
            _needles_note_shown = True
        return []
    salt, table = needles
    return [Finding(f"{label}:{n}", f"known account value: {KIND_LABELS.get(k, k)}")
            for n, line in lines for k in sorted(needle_kinds(line, salt, table))]


def _phone_shaped(value: str) -> bool:
    digits = re.sub(r"[\s.()+-]", "", value)
    return digits.isdigit() and len(_national(digits)) == 9 and _national(digits)[0] in "6789"


def _nif_shaped(value: str) -> bool:
    return bool(re.fullmatch(r"\d{8}-?[A-Za-z]|[XYZxyz]-?\d{7}-?[A-Za-z]", value.strip()))


def _classify(value: str) -> str:
    if EMAIL.fullmatch(value.strip()):
        return "email"
    if _phone_shaped(value):
        return "phone"
    return "nif" if _nif_shaped(value) else "id"


def _string_leaves(obj) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _string_leaves(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _string_leaves(v)]
    return [obj] if isinstance(obj, str) else []


def collect_values(me: dict, lines: list[dict], invoices: list[dict], claims: dict) -> list[tuple[str, str]]:
    """(kind, value) for every account value in the CLI's outputs and the identity JWT's claims.

    Kinds: phone, nif, id, invoice, email, first (given name), family (surnames), address.
    """
    values: list[tuple[str, str]] = []

    def add(kind: str, value) -> None:
        if isinstance(value, str) and value.strip():
            values.append((kind, value.strip()))

    def line_kind(text) -> str:
        t = (text or "").lower()
        return "phone" if any(k in t for k in ("mobile", "móvil", "movil", "landline", "fijo")) else "id"

    for acct in me.get("accounts") or []:
        add("id", acct.get("id"))
        add("address", acct.get("address"))
        holder = acct.get("holder") or {}
        add("first", holder.get("first_name"))
        add("family", holder.get("family_name"))
        add("email", holder.get("email"))
        add("phone", holder.get("msisdn"))
    for out in lines:
        add("id", out.get("account_id"))
        for line in out.get("lines") or []:
            add(line_kind(line.get("kind")), line.get("id"))
    for out in invoices:
        add("id", out.get("account_id"))
        for inv in out.get("invoices") or []:
            add("invoice", inv.get("id"))
            add("invoice", inv.get("number"))
            add("id", inv.get("billing_account_id"))
    profile = claims.get("userProfile") if isinstance(claims.get("userProfile"), dict) else {}
    for value in (claims.get("sub"), claims.get("usr"), profile.get("id"), profile.get("userName")):
        if isinstance(value, str):
            add(_classify(value), value)
    add("first", profile.get("firstName"))
    add("family", profile.get("lastName"))
    add("email", profile.get("email"))
    add("phone", profile.get("msisdn"))
    add("nif", (profile.get("document") or {}).get("id"))
    for site in profile.get("sites") or []:
        add("id", site.get("id"))
    for svc in profile.get("services") or []:
        add(line_kind(svc.get("type")), svc.get("id"))
        add("id", svc.get("customerAccountId"))
    for value in _string_leaves(profile):  # anything else shaped like contact data or an identity document
        kind = _classify(value)
        if kind != "id":
            add(kind, value)
    return values


def needle_tokens(values: list[tuple[str, str]]) -> dict[str, str]:
    """Normalised token -> needle kind for these (kind, value) pairs."""
    tokens: dict[str, str] = {}
    first: list[str] = []
    family: list[str] = []
    for kind, value in values:
        if kind == "phone":
            digits = re.sub(r"\D", "", value)
            nat = _national(digits)
            tokens.update({t: "phone" for t in _digit_tokens(digits)})
            if len(nat) >= 7:
                tokens.update({f"m:{nat[0]}|{nat[-3:]}": "masked", f"m:{nat[0]}|{nat[-4:]}": "masked",
                               f"l:{nat[-4:]}": "masked"})
        elif kind == "nif":
            nif = re.sub(r"[\s-]", "", value).upper()
            tokens["a:" + nif] = "nif"
            digits = re.sub(r"\D", "", nif)
            if len(digits) >= 7:
                tokens["d:" + digits] = "nif"
        elif kind in ("id", "invoice"):
            if value.isdigit():
                if len(value) >= 5:
                    tokens["d:" + value] = kind
                continue
            up = value.upper()
            if len(up) >= 6 and any(c.isdigit() for c in up):
                tokens.update({"a:" + up: kind, "a:" + up.replace("-", ""): kind})
            tokens.update({"d:" + run: kind for run in re.findall(r"\d{6,}", value)})
        elif kind == "email":
            tokens["e:" + value.lower()] = "email"
            local = fold(value.split("@", 1)[0])
            if len(local) >= 6 and local.isalpha():
                tokens["w:" + local] = "email"
        elif kind in ("first", "family"):
            words = [w for w in WORD.findall(fold(value)) if w not in STOPWORDS and w.isalpha()]
            (first if kind == "first" else family).extend(w for w in words if w not in first + family)
            tokens.update({f"p:{a} {b}": "name" for a, b in zip(words, words[1:])})
        elif kind == "address":
            words = [w for w in WORD.findall(fold(value)) if w not in STOPWORDS]
            tokens.update({f"p:{a} {b}": "address" for a, b in zip(words, words[1:])
                           if any(len(w) >= 3 and w.isalpha() for w in (a, b))})
    # Name words alone, except those in the maintainer's public handle (a relative may share a surname
    # with them): those only count next to another name word.
    tokens.update({"w:" + w: "name" for w in first + family if len(w) >= 3 and w not in MAINTAINER})
    tokens.update({f"p:{a} {b}": "name" for f in first for s in family for a, b in ((f, s), (s, f))})
    tokens.update({f"p:{a} {b}": "name" for a, b in zip(first + family, (first + family)[1:])})
    return tokens


def _token_claims() -> dict:
    """The identity JWT's claims from the CLI's token file (never printed)."""
    env = os.environ.get("VODAFONE_TOKENS_FILE")
    path = Path(env).expanduser() if env else config_home() / "tokens.json"
    try:
        jwt = json.loads(path.read_text()).get("jwt")
        jwt = jwt[0] if isinstance(jwt, list) and jwt else jwt
        payload = jwt.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (OSError, ValueError, AttributeError, IndexError, TypeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _vodafone(exe: str, *args: str) -> dict | None:
    """One CLI command's JSON output, captured here and never shown. None when it failed."""
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        out = json.loads(r.stdout) if r.returncode == 0 else None
    except (subprocess.SubprocessError, OSError, ValueError):
        out = None
    return out if isinstance(out, dict) else None


def self_check(values: list[tuple[str, str]], texts: list[str], salt: str, table: dict[str, str]) -> dict[str, int]:
    """Hits per kind when scanning the very outputs the needles came from, plus masked renderings of the
    phone numbers (the outputs carry none). Only counts leave this function."""
    hits: dict[str, int] = {}
    masked = []
    for kind, value in values:
        nat = _national(re.sub(r"\D", "", value)) if kind == "phone" else ""
        if len(nat) >= 7:
            masked.append(f"{nat[0]}*****{nat[-3:]}")
    for text in texts + masked:
        for line in text.splitlines():
            for kind in needle_kinds(line, salt, table):
                hits[kind] = hits.get(kind, 0) + 1
    return hits


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def needles_build() -> int:
    exe = shutil.which("vodafone")
    if not exe:
        print("needles build: the `vodafone` CLI isn't on PATH", file=sys.stderr)
        return 1
    me = _vodafone(exe, "me")
    if me is None:
        print("needles build: `vodafone me` failed (output not shown); is the CLI signed in?", file=sys.stderr)
        return 1
    account_ids = [a["id"] for a in me.get("accounts") or [] if isinstance(a, dict) and isinstance(a.get("id"), str)]
    lines, invoices, failed = [], [], []
    for i, acct in enumerate(account_ids, 1):
        for name, args, sink in (("lines", ("lines",), lines), ("invoices list", ("invoices", "list"), invoices)):
            out = _vodafone(exe, "--account", acct, *args)
            if out is None:
                failed.append(f"`{name}` for account #{i}")
            else:
                sink.append(out)
    claims = _token_claims()
    values = collect_values(me, lines, invoices, claims)
    tokens = needle_tokens(values)
    salt = secrets.token_hex(16)
    kinds: dict[str, list[str]] = {}
    for token, kind in tokens.items():
        kinds.setdefault(kind, []).append(_hash(salt, token))
    path = needles_path()
    _write_private(path, json.dumps({"version": 1, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "salt": salt, "kinds": {k: sorted(v) for k, v in sorted(kinds.items())}}))
    table = {h: kind for kind, hashes in kinds.items() for h in hashes}
    texts = [json.dumps(o, ensure_ascii=False, indent=2) for o in (me, *lines, *invoices)]
    texts.append(json.dumps(claims.get("userProfile") or {}, ensure_ascii=False, indent=2))
    hits = self_check(values, texts, salt, table)
    value_counts: dict[str, int] = {}
    for kind, value in set(values):
        kind = "name" if kind in ("first", "family") else kind
        value_counts[kind] = value_counts.get(kind, 0) + 1
    print(f"needles build: wrote {path} (mode 600) from `vodafone me`, `lines` and `invoices list` for "
          f"{len(account_ids)} account(s) and the identity JWT")
    missing = []
    for kind in KIND_LABELS:
        n_hashes = len(kinds.get(kind, []))
        if not n_hashes:
            continue
        source = f"{value_counts.get(kind, 0)} value(s)" if kind != "masked" else "derived from the phone numbers"
        ok = hits.get(kind, 0) > 0
        missing += [] if ok else [kind]
        print(f"  {KIND_LABELS[kind]:<30} {n_hashes:>4} hashes  {source:<32} self-check: "
              f"{'ok, ' + str(hits[kind]) + ' hit(s)' if ok else 'NO HIT'}")
    for f in failed:
        print(f"needles build: {f} failed (output not shown); its values are missing", file=sys.stderr)
    if missing:
        print(f"needles build: self-check found no hit for {', '.join(missing)}", file=sys.stderr)
    return 1 if missing or failed else 0


def needles_status() -> int:
    path = needles_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        print(f"no needles file at {path}; run `python3 .githooks/pii_check.py needles build`")
        return 1
    mode = oct(path.stat().st_mode & 0o777)
    print(f"{path} (mode {mode[2:]}), built {data.get('built_at', '?')}")
    for kind, hashes in data.get("kinds", {}).items():
        print(f"  {KIND_LABELS.get(kind, kind):<30} {len(hashes):>4} hashes")
    return 0


# --- model layer (Claude Haiku) ------------------------------------------------------------------

MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
CHUNK_CHARS = 200_000  # about 50K tokens per request
MAX_CHUNKS = 5
MODEL_BUDGET_SECONDS = 75  # all chunks together; the agent hook's own timeout is 90 s
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "personal_data_found": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"location": {"type": "string"}, "kind": {"type": "string"}},
                "required": ["location", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["personal_data_found", "findings"],
    "additionalProperties": False,
}
SYSTEM_PROMPT = f"""You check text that someone is about to commit to the public GitHub repository \
{MAINTAINER}/vodafone-cli, or post there as a pull request, issue, comment or release notes. vodafone-cli is \
a command-line client for one real Mi Vodafone (Vodafone España) customer account, and most commits are \
written by AI coding agents that have seen that account's live data. Anything committed is public for good, \
so a real customer's personal data must never appear.

Decide whether the text contains personal data of a real person, above all the account holder:
- a name: a full name, a surname, or a first name that refers to the account holder or another customer
- a phone number: Spanish (nine digits starting with 6, 7, 8 or 9, with or without +34 or 0034, in any \
grouping) or foreign (for example a US +1 number), or a masked number copied from real data
- a DNI, NIF or NIE; an IBAN or other bank account digits
- a postal address, a street with its number, a postal code with its town, or a personal email address
- customer, billing-account, site, service or subscription ids, and invoice numbers (shaped like two \
letters, two digits, a dash and nine digits)
- amounts, dates, calls or usage tied to a specific person's bill or line
- pasted CLI or API output, HAR captures or token contents carrying any of the above

These are fine; don't flag them:
- obviously invented test data: repeated or sequential digits (600000000, 600000001, 612345678, 699999999, \
00000000T, 12345678Z, AB12-012345678), placeholders (<phone>, <NIF>, Ana Ejemplo, Calle Falsa 123, \
user@example.com), and the documentation's masked example 6*****789
- Vodafone's public app constants: the OAuth client id, scopes, API hosts and paths, header names, tariff \
and product codes, error codes
- the maintainer's public GitHub handle {MAINTAINER} and its noreply address, and AI agents (Claude, Codex) \
named as authors
- code, tests, configuration, schemas, field names, and descriptions of what a field contains
- text that only talks about personal data (that a value was removed or masked, or must never be committed) \
without including any

Each block starts with "### <source>" and each line with its line number and "|". For each finding, give \
the location as "<source>:<line>" and the kind as a short category label only (for example "phone number", \
"holder name", "NIF", "invoice number"). Never repeat any of the personal data, not even part of it."""


def _checkout_root(cwd: str | None = None) -> Path | None:
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
    except (subprocess.CalledProcessError, OSError):
        return None


def _main_checkout(cwd: str | None = None) -> Path | None:
    """The clone that owns .git (worktrees share it): where a gitignored .env would live."""
    try:
        return Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd).strip()).parent
    except (subprocess.CalledProcessError, OSError):
        return None


def _api_key() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    main = _main_checkout()
    env = main / ".env" if main else None
    if env and env.is_file():
        for line in env.read_text(errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*(.*?)\s*$", line)
            if m:
                value = m.group(1).strip("'\"")
                return value if value and not value.startswith("$") else None
    return None


def _claude_cli() -> str | None:
    """The `claude` CLI; GUI git clients run hooks with a short PATH, so try its default install too."""
    found = shutil.which("claude")
    fallback = Path.home() / ".local" / "bin" / "claude"
    return found or (str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None)


def _chunks(blocks: Blocks) -> list[str]:
    """Render blocks as numbered text and pack them into requests of at most CHUNK_CHARS."""
    parts = []
    for label, lines in blocks:
        piece = f"### {label}"
        for n, text in lines:
            row = f"\n{n}| {text}"
            if len(piece) + len(row) > CHUNK_CHARS:
                parts.append(piece)
                piece = f"### {label} (continued)"
            piece += row
        parts.append(piece)
    chunks, current = [], ""
    for part in parts:
        if current and len(current) + len(part) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + part
    return chunks + ([current] if current else [])


def _ask_api(key: str, chunk: str, timeout: float) -> tuple[dict | None, str | None]:
    """(verdict, None) from the Messages API, or (None, why not)."""
    body = {"model": MODEL, "max_tokens": 8192, "system": SYSTEM_PROMPT,
            "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            "messages": [{"role": "user", "content": chunk}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response = json.load(resp)
    except urllib.error.HTTPError as e:
        return None, f"model check failed (HTTP {e.code})"
    except (urllib.error.URLError, OSError, ValueError):
        return None, "model check failed (no connection)"
    text = next((b.get("text", "") for b in response.get("content", []) if b.get("type") == "text"), "")
    try:
        verdict = json.loads(text) if response.get("stop_reason") == "end_turn" else None
    except ValueError:
        verdict = None
    return (verdict, None) if isinstance(verdict, dict) else (None, f"model check gave no verdict ({response.get('stop_reason')})")


def _ask_cli(exe: str, chunk: str, timeout: float) -> tuple[dict | None, str | None]:
    """(verdict, None) from `claude -p` on the user's Claude Code login, or (None, why not).

    No tools, no settings (so no hooks: it can't recurse into this check), no MCP, no CLAUDE.md
    (--safe-mode, and it runs in an empty directory), and nothing saved.
    """
    cmd = [exe, "-p", "--model", MODEL, "--effort", "low", "--output-format", "json", "--safe-mode",
           "--setting-sources", "", "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--system-prompt", SYSTEM_PROMPT, "--json-schema", json.dumps(VERDICT_SCHEMA)]
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    try:
        with tempfile.TemporaryDirectory(prefix="pii-check-") as cwd:
            r = subprocess.run(cmd, input=chunk, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        return None, f"model check timed out after {timeout:.0f}s"
    except OSError:
        return None, "model check failed (the claude CLI didn't start)"
    try:
        result = json.loads(r.stdout)
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get("is_error") or result.get("subtype") != "success":
        detail = ("error" if result.get("is_error") else result.get("subtype")) if isinstance(result, dict) \
            else f"exit {r.returncode}"
        return None, f"model check failed (claude CLI: {detail}; signed in?)"
    verdict = result.get("structured_output")
    return (verdict, None) if isinstance(verdict, dict) else (None, "model check gave no verdict")


def model_check(blocks: Blocks) -> tuple[list[Finding], str | None]:
    """Claude Haiku's findings for these lines, plus a note when the check couldn't (fully) run."""
    blocks = [(label, [(n, t) for n, t in lines if ALLOW_MARKER not in t and t.strip()]) for label, lines in blocks]
    blocks = [(label, lines) for label, lines in blocks if lines]
    if not blocks:
        return [], None
    key = _api_key()
    exe = None if key else _claude_cli()
    if not key and not exe:
        return [], "model check skipped: no ANTHROPIC_API_KEY and no `claude` CLI"
    chunks = _chunks(blocks)
    deadline = time.monotonic() + MODEL_BUDGET_SECONDS
    findings: list[Finding] = []
    for chunk in chunks[:MAX_CHUNKS]:
        remaining = deadline - time.monotonic()
        if remaining < 5:
            return findings, "model check stopped: out of time; the other checks still ran"
        verdict, note = _ask_api(key, chunk, remaining) if key else _ask_cli(exe, chunk, remaining)
        if verdict is None:
            return findings, f"{note}; the other checks still ran"
        if verdict.get("personal_data_found"):
            found = [Finding(str(f.get("location", "?")), f"{f.get('kind', 'personal data')} (model)")
                     for f in verdict.get("findings", []) if isinstance(f, dict)]
            findings += found or [Finding("text", "personal data (model)")]
    note = (f"the model checked the first {MAX_CHUNKS} of {len(chunks)} parts; split very large commits"
            if len(chunks) > MAX_CHUNKS else None)
    return findings, note


# --- agent hook ----------------------------------------------------------------------------------

GH_TEXT_CMDS = re.compile(r"^(?:pr|issue|release)\s+(?:create|edit|comment|review)\b|^api\b")
VALUE_FLAGS = {"-m", "--message", "-F", "--file", "-c", "-C", "--author", "--date", "-t", "--template",
               "--reuse-message", "--reedit-message", "--fixup", "--squash", "--trailer", "--cleanup"}
FILE_FLAGS = {"--body-file", "--notes-file", "-F", "--input", "--file"}


def _segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return [command.split()]
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|"):
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    return [s for s in segs + [cur] if s]


def _git_commit_args(seg: list[str]) -> list[str] | None:
    if "git" not in seg:
        return None
    rest = seg[seg.index("git") + 1:]
    i = 0
    while i < len(rest) and rest[i].startswith("-"):  # git -C dir / -c k=v before the subcommand
        i += 2 if rest[i] in ("-C", "-c") else 1
    return rest[i + 1:] if i < len(rest) and rest[i] == "commit" else None


def _gh_args(seg: list[str]) -> list[str] | None:
    """What follows `gh` and its global flags (`gh -R owner/repo pr create ...` -> ["pr", "create", ...])."""
    if "gh" not in seg:
        return None
    rest = seg[seg.index("gh") + 1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in ("-R", "--repo") else rest[1:]
    return rest


def _skips_hooks(args: list[str]) -> bool:
    skip_next = False
    for t in args:
        if skip_next:
            skip_next = False
            continue
        if t in VALUE_FLAGS:
            skip_next = True
        elif t in ("-n", "--no-verify") or re.fullmatch(r"-[a-zA-Z]*n[a-zA-Z]*", t) and not t.startswith("--"):
            return True
    return False


def check_command(command: str, cwd: str) -> tuple[list[Finding], str | None, Blocks]:
    """(pattern and known-value findings, refusal, text for the model) for one shell command an agent is
    about to run.

    `git commit` gets only the --no-verify check here: the git hooks check its content.
    `gh pr|issue|release create|edit|comment|review` and `gh api` get their text checked, body files included.
    """
    findings, refusal, blocks, posts_text = [], None, [], False
    for seg in _segments(command):
        commit_args = _git_commit_args(seg)
        if commit_args is not None and _skips_hooks(commit_args):
            refusal = "`git commit --no-verify` skips the personal-data check. Commit without it."
        tail = _gh_args(seg)
        if tail is not None and GH_TEXT_CMDS.match(" ".join(tail)):
            posts_text = True
            for flag, val in zip(tail, tail[1:]):
                if flag in FILE_FLAGS or val.startswith("@") or re.match(r"^[\w.-]+=@", val):
                    target = (val[1:] if val.startswith("@") else val).split("=@", 1)[-1]
                    if target == "-":
                        refusal = "Text piped in on stdin can't be checked; write it to a file and pass the file."
                        continue
                    p = Path(cwd, target)
                    if p.is_file():
                        lines = numbered(p.read_text(errors="replace"))
                        findings += scan_lines(lines, target)
                        blocks.append((target, lines))
    if posts_text:
        lines = numbered(command)
        findings += scan_lines(lines, "command")
        blocks.insert(0, ("command", lines))
    return findings, refusal, blocks


def agent_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    command = tool_input.get("command") or data.get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    cursor_style = "tool_input" not in data and "command" in data
    try:
        enable(quiet=True, cwd=cwd)
    except (subprocess.CalledProcessError, OSError):
        pass
    findings, refusal, blocks = check_command(command, cwd) if command else ([], None, [])
    if not findings and not refusal and blocks:
        findings, note = model_check(blocks)
        if note:
            print(f"pii check: {note}", file=sys.stderr)
    if not findings and not refusal:
        if cursor_style:
            print(json.dumps({"permission": "allow"}))
        return 0
    reason = refusal or ("Possible personal data of the account holder: "
                         + "; ".join(f"{f.where} ({f.kind})" for f in findings) + ". " + HINT)
    if cursor_style:
        print(json.dumps({"permission": "deny", "user_message": reason, "agent_message": reason}))
    print(reason, file=sys.stderr)
    return 2


# --- git hook install ----------------------------------------------------------------------------

SHIM_MARKER = "installed by .githooks/pii_check.py"
SHIM = """#!/bin/sh
# {marker}: runs the repo's tracked hook, if this checkout has it.
hook="$(git rev-parse --show-toplevel)/.githooks/{name}"
[ -x "$hook" ] && exec "$hook" "$@"
exit 0
"""


def enable(quiet: bool = False, cwd: str | None = None) -> int:
    top = Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())
    if not (top / ".githooks" / "pii_check.py").is_file():
        return 0  # another repo: leave its hooks alone
    hooks = Path(git("rev-parse", "--path-format=absolute", "--git-path", "hooks", cwd=cwd).strip())
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("pre-commit", "commit-msg"):
        target, content = hooks / name, SHIM.format(marker=SHIM_MARKER, name=name)
        if target.exists():
            current = target.read_text(errors="replace")
            if current == content:
                continue
            if SHIM_MARKER not in current and "File generated by pre-commit" not in current:
                if not quiet:
                    print(f"WARN: {target} is someone else's hook; left it alone. Call .githooks/{name} from it.",
                          file=sys.stderr)
                continue
        target.write_text(content)
        target.chmod(0o755)
        if not quiet:
            print(f"installed {target}")
    return 0


# --- CLI -----------------------------------------------------------------------------------------

def report(findings: list[Finding], note: str | None = None) -> int:
    if note:
        print(f"pii check: {note}", file=sys.stderr)
    if not findings:
        return 0
    print("Blocked: possible personal data of the account holder (values not shown):", file=sys.stderr)
    for f in findings:
        print(f"  {f.where}  {f.kind}", file=sys.stderr)
    print(HINT, file=sys.stderr)
    return 2


def check(findings: list[Finding], blocks: Blocks, use_model: bool = True) -> int:
    """Patterns and known values first; the model only runs when they found nothing (a hit blocks either way)."""
    if findings or not use_model:
        return report(findings)
    return report(*model_check(blocks))


def self_test() -> int:
    # Fabricated samples only, built at runtime so this file never holds a realistic value.
    mobile, nif_digits = "6" + "71" + "829304", "31" + "415926"
    nif = nif_digits + NIF_LETTERS[int(nif_digits) % 23]
    nie = "Y" + "2718281" + NIF_LETTERS[int("1" + "2718281") % 23]
    bban = "2100" + "0418" + "4502" + "0005" + "1333"
    iban = next(f"ES{c:02d}{bban}" for c in range(100) if _iban_personal(f"ES{c:02d}{bban}"))
    gmail = "gm" + "ail.com"
    bad = [f"reach me at jo.smith@{gmail}", f"tel {mobile}", f"+34 {mobile[:3]} {mobile[3:6]} {mobile[6:]}",
           f"+1 (415) 867-{'53' + '09'}", f"NIF {nif}", f"NIE {nie}", f"IBAN {iban}",
           "factura AB" + "12-31" + "4159265", "vive en Calle de " + "Alcalá 42", "28001 " + "Madrid"]
    good = ["user@example.com", f"support@{gmail}", "600000001", "612345678", "699999999", "00000000T",
            "12345678Z", "ES9121000418450200051332", "AB12-012345678", "Calle Falsa 123", f"tel {mobile}  # {ALLOW_MARKER}",
            "noreply@anthropic.com", "git@github.com", "sha256:a612345679b", "timeout 1700000000", "900123456"]
    fails = [s for s in bad if not scan_line(s)] + [s for s in good if scan_line(s)]
    cmd_cases = {
        "git commit --no-verify -m 'x'": True, "git commit -nm 'x'": True, "git commit -am 'fix'": False,
        "git -C repo commit -m 'msg -n here'": False, f"gh pr create --body 'cc jo@{gmail}'": True,
        "gh pr create --title t --body 'all good'": False, f"echo jo@{gmail}": False,
        f"gh -R o/r issue comment 1 --body 'tel {mobile}'": True, "gh pr create --body-file -": True,
    }
    for cmd, should_block in cmd_cases.items():
        found, refusal, _ = check_command(cmd, ".")
        if bool([f for f in found if "known account value" not in f.kind] or refusal) != should_block:
            fails.append(cmd)
    if not all(blocked_path(p) for p in ("a.har", ".local-secrets/x", "tokens.json", "x/tokens.bak.json",
                                         "bill.PDF", "d.csv", ".env")):
        fails.append("blocked_path positive")
    if any(blocked_path(p) for p in ("src/vodafone/tokens.py", "docs/x.md", ".env.example", "uv.lock")):
        fails.append("blocked_path negative")
    salt = "self-test"
    fake = [("phone", mobile), ("first", "Ana"), ("family", "Ejemplo Prueba"), ("nif", nif)]
    table = {_hash(salt, t): k for t, k in needle_tokens(fake).items()}
    for line, kind in ((f"{mobile[0]}*****{mobile[-3:]}", "masked"), ("ANA EJEMPLO", "name"), (nif, "nif"),
                       (f"{mobile[:3]} {mobile[3:5]} {mobile[5:7]} {mobile[7:]}", "phone")):
        if kind not in needle_kinds(line, salt, table):
            fails.append(f"needles: {kind}")
    if needle_kinds("600000001 6*****001 Calle Falsa", salt, table):
        fails.append("needles negative")
    if len(_chunks([("f", [(i, "x" * 1000) for i in range(1, 450)])])) != 3:
        fails.append("chunking")
    for f in fails:
        print(f"self-test FAILED: {f!r}", file=sys.stderr)
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    mode, args = (argv[1] if len(argv) > 1 else ""), argv[2:]
    if mode == "staged":
        return check(*scan_staged())
    if mode == "msg" and len(args) == 1:
        lines = message_lines(args[0])
        return check(scan_lines(lines, "commit message"), [("commit message", lines)])
    if mode == "text":
        lines = numbered(sys.stdin.read())
        return check(scan_lines(lines, "text"), [("text", lines)])
    if mode == "files":
        use_model = "--patterns-only" not in args
        paths = [p for p in args if p != "--patterns-only" and Path(p).is_file()]
        blocks = [(p, numbered(Path(p).read_text(errors="replace"))) for p in paths]
        findings = [Finding(p, "data file that never belongs in git") for p in paths if blocked_path(p)]
        return check(findings + [f for p, lines in blocks for f in scan_lines(lines, p)], blocks, use_model)
    if mode == "agent-hook":
        return agent_hook()
    if mode == "enable":
        return enable()
    if mode == "needles" and args == ["build"]:
        return needles_build()
    if mode == "needles" and args in ([], ["status"]):
        return needles_status()
    if mode == "--self-test":
        return self_test()
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))

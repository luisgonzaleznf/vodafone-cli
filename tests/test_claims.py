"""`invoices claim` and `invoices claims` against a mock DXL. All values are synthetic."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from typer.testing import CliRunner

from vodafone import api, cli
from vodafone.client import DxlClient

ACCESS = "A" * 28
CSRF = "c" * 36
ACCOUNT = "100000001"
OTHER_ACCOUNT = "100000002"
PARTY = "200000002"  # the lookup placeholder's id: the party a first review is filed for
CLOSED_PARTY = "200000005"  # relatedParty[0] of an earlier, closed review
TICKET = "300000003"
LATEST, OLDER = "XX00-000000010", "XX00-000000009"
NOW = datetime(2026, 10, 24, 6, 9, 52, 880123, tzinfo=UTC)
SENT_AT = "2026-10-24T06:09:52.880Z"
REVIEW_PATH = "/es/billreview/v1/troubleTicket"
QUESTION = "¿Enviar esta reclamación a Vodafone? [s/N]"


def _bill(bill_no: str, month: int, *, account: str | None = ACCOUNT, amount: float = 45.1) -> dict:
    return {
        "billNo": bill_no,
        "creationDate": f"2026-{month:02d}-11T09:30",
        "status": "ready",
        "billingCycle": {"billingYear": "2026", "billingMonth": f"{month:02d}"},
        "billingAccount": {"id": account} if account else None,
        "billOverview": {"numberOfAlarms": 0, "amounts": [{"currency": "EUR", "grossAmountDue": amount}]},
        "documents": [],
    }


def _no_review(account: str = ACCOUNT, party: str = PARTY) -> list[dict]:
    """The lookup's reply for an account that never filed a review."""
    return [{"id": party, "relatedParty": [{"id": account, "role": "Customer"}], "hasTicket": False}]


def _review(status: str | None, *, ticket: str = TICKET, bill_no: str = OLDER, party: str = CLOSED_PARTY) -> dict:
    review = {
        "id": ticket,
        "hasTicket": True,
        "relatedParty": [{"id": party, "role": "Customer"}, {"id": ACCOUNT}],
        "description": f"Factura {bill_no}\nComentario del cliente:  no reconozco el cargo",
        # Spanish local time labelled +0000, as Vodafone sends it.
        "creationDate": "2026-09-12T12:00:00.000+0000",
        "lastUpdate": "2026-09-20T12:00:00.000+0000",
        "expectedResolutionDate": "2026-10-12T12:00:00.000+0000",
        "channel": {"name": "MVA"},
    }
    if status is not None:
        review["status"] = status
    return review


@pytest.fixture
def tokens_file(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({
        "accessToken": ACCESS,
        "userCsrfToken": CSRF,
        "accessTokenExpirationTime": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }))
    return path


@pytest.fixture
def dxl(monkeypatch):
    """Mock DXL. Tweak state["review"] (the lookup reply) and state["post"] (a Response, or a
    callable taking the request); inspect state["requests"] and posts()."""
    state = {
        "accounts": [ACCOUNT],
        "bills": [_bill(OLDER, 9, amount=39.9), _bill(LATEST, 10)],
        "review": _no_review(),
        "post": httpx.Response(201, json={"id": TICKET, "status": "Abierto"}),
        "lookup_headers": {},
        "requests": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        path = request.url.path
        if path == "/v2/customer/customerAccounts":
            return httpx.Response(200, json={"items": [{"id": a} for a in state["accounts"]]})
        if path == "/v3/payment/customerBills":
            return httpx.Response(200, json={"items": state["bills"]})
        if path == REVIEW_PATH and request.method == "GET":
            return httpx.Response(200, json=state["review"], headers=state["lookup_headers"])
        if path == REVIEW_PATH and request.method == "POST":
            reply = state["post"]
            return reply(request) if callable(reply) else reply
        raise AssertionError(f"unexpected request {request.method} {path}")

    monkeypatch.setattr(api, "DxlClient", lambda tokens: DxlClient(tokens, spacing=0, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(api, "_utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "_interactive", lambda: False)  # as under CliRunner; `tty` turns it on
    state["posts"] = lambda: [r for r in state["requests"] if r.method == "POST"]
    return state


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: True)


def _claim(tokens_file, *args, input=None):
    return CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "invoices", "claim", *args], input=input)


def _claims(tokens_file):
    return CliRunner().invoke(cli.app, ["--tokens", str(tokens_file), "invoices", "claims"])


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def _note(bill_no: str, text: str) -> str:
    return f"Factura {bill_no}\nComentario del cliente:  {text}"


def _error(result) -> dict:
    last = result.stderr.strip().splitlines()[-1]
    # After a prompt the error shares its line: unlike a terminal, CliRunner doesn't echo the answer.
    return json.loads(last[last.index("{"):])["error"]


# --- the request ---------------------------------------------------------------------------------


def test_yes_sends_the_web_apps_body_once(tokens_file, dxl):
    result = _claim(tokens_file, "latest", "--message", "  El cargo de roaming no es mío (12,10 €).\nGracias  ", "--yes")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "ticket_id": TICKET, "invoice_id": LATEST, "billing_account_id": ACCOUNT, "sent_at": SENT_AT,
    }
    assert [(r.method, r.url.path) for r in dxl["requests"]] == [
        ("GET", "/v2/customer/customerAccounts"), ("GET", "/v3/payment/customerBills"),
        ("GET", REVIEW_PATH), ("POST", REVIEW_PATH),
    ]
    [post] = dxl["posts"]()
    body = _body(post)
    assert list(body) == ["description", "ticketType", "requestedResolutionDate", "channel", "note", "@type", "relatedParty"]
    assert body == {
        "description": "Peticion revision factura",
        "ticketType": "Peticion",
        "requestedResolutionDate": SENT_AT,
        "channel": {"id": "ECARE"},
        "note": [{"id": "Comment", "text": _note(LATEST, "El cargo de roaming no es mío (12,10 EUR).\nGracias")}],
        "@type": "TroubleTicketExtended",
        "relatedParty": [{"id": PARTY}],
    }
    # Serialized as JSON.stringify does: compact, non-ASCII as is.
    assert post.content == json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    assert "no es mío".encode() in post.content and b"\\u00ed" not in post.content
    body["note"][0]["text"].encode("iso-8859-1")  # all of it storable as Vodafone stores it


def test_the_note_has_two_spaces_after_the_colon_and_a_real_line_feed():
    assert api.claim_note("XX00-000000001", "hola") == "Factura XX00-000000001\nComentario del cliente:  hola"


def test_the_lookup_queries_the_invoices_billing_account(tokens_file, dxl):
    _claim(tokens_file, "latest", "--message", "x", "--dry-run")

    [lookup] = [r for r in dxl["requests"] if r.url.path == REVIEW_PATH]
    assert dict(lookup.url.params) == {
        "relatedParty[?(@.role=='Customer')].id": ACCOUNT,
        "status": "Abierto,Reabierto,Cerrado",
    }


def test_the_post_is_sent_like_the_web_apps_interceptor_sends_it(tokens_file, dxl):
    # A csrf-token cookie the server set (as its settings endpoint does) must not ride along as a
    # second, mismatched csrf-token.
    dxl["lookup_headers"] = [("set-cookie", "JSESSIONID=sess-1; Path=/"), ("set-cookie", "csrf-token=server-set; Path=/")]

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 0, result.stderr
    [post] = dxl["posts"]()
    h = post.headers
    assert h["authorization"] == f"Bearer {ACCESS}"
    assert h["vf-csrf-token"] == CSRF
    assert (h["vf-country-code"], h["vf-target-environment"]) == ("ES", "aws-prod-es")
    assert re.fullmatch(r"[0-9a-f-]{36}", h["vf-trace-transaction-id"])
    assert h["content-type"] == "application/json"
    assert (h["origin"], h["referer"]) == ("https://m.vodafone.es", "https://m.vodafone.es/")
    assert h["cookie"] == f"JSESSIONID=sess-1; csrf-token={CSRF}"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC), "2026-01-02T03:04:05.678Z"),
        (datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC), "2026-01-02T03:04:05.999Z"),  # truncated, as JS does
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05.000Z"),
        (datetime(2026, 1, 1, 0, 30, 0, 5000, tzinfo=timezone(timedelta(hours=2))), "2025-12-31T22:30:00.005Z"),
    ],
)
def test_requested_resolution_date_is_utc_with_milliseconds(now, expected):
    draft = api.ClaimDraft(LATEST, ACCOUNT, "2026-10", 45.1, "EUR", PARTY, "x")
    assert api.claim_body(draft, now)["requestedResolutionDate"] == expected


def test_the_send_time_is_taken_when_sending(tokens_file, dxl, monkeypatch):
    monkeypatch.setattr(api, "_utc_now", lambda: datetime.now(UTC))

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    sent = _body(dxl["posts"]()[0])["requestedResolutionDate"]
    assert re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z", sent)
    assert json.loads(result.stdout)["sent_at"] == sent
    assert abs(datetime.fromisoformat(sent) - datetime.now(UTC)) < timedelta(minutes=1)


# --- eligibility ---------------------------------------------------------------------------------


def test_without_a_review_it_is_filed_for_the_placeholders_id(tokens_file, dxl):
    _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert _body(dxl["posts"]()[0])["relatedParty"] == [{"id": PARTY}]


def test_with_only_closed_reviews_it_is_filed_for_their_first_party(tokens_file, dxl):
    dxl["review"] = [_review("Cerrado"), _review("CERRADO", ticket="300000004", party="200000009")]

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 0, result.stderr
    assert _body(dxl["posts"]()[0])["relatedParty"] == [{"id": CLOSED_PARTY}]


@pytest.mark.parametrize("status", ["Abierto", "Reabierto", "ABIERTO", "reabierto", " Abierto "])
@pytest.mark.parametrize("mode", ["--yes", "--dry-run"])
def test_an_open_or_reopened_review_refuses_without_sending(tokens_file, dxl, status, mode):
    dxl["review"] = [_review("Cerrado", ticket="300000004"), _review(status)]

    result = _claim(tokens_file, "latest", "--message", "x", mode)

    assert result.exit_code == 5
    assert result.stdout == ""
    error = _error(result)
    assert error["type"] == "claim_already_open" and "vodafone invoices claims" in error["message"]
    assert dxl["posts"]() == []


@pytest.mark.parametrize(
    "review",
    [
        [],  # nothing for the account
        _no_review(account=OTHER_ACCOUNT),  # a placeholder for another billing account
        [{**_review("Cerrado"), "relatedParty": [{"id": CLOSED_PARTY}]}],  # no billing account on the review
        [_review(None)],  # a review without a status: can't tell whether it is open
        _no_review(party="200000002; x"),  # a party id that is not a plain id
        _no_review(party=""),
        {"items": _no_review()},  # not the list the web app reads
        [{"id": PARTY, "relatedParty": [{"id": ACCOUNT}]}],  # no hasTicket
    ],
)
def test_a_lookup_that_gives_no_party_for_the_account_refuses_without_sending(tokens_file, dxl, review):
    dxl["review"] = review

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 7, result.stderr
    assert _error(result)["type"] == "api_changed"
    assert dxl["posts"]() == []


# --- confirmation --------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["s", "S", "si", "sí", "Sí"])
def test_on_a_terminal_a_yes_sends_once_after_showing_what_will_be_sent(tokens_file, dxl, tty, answer):
    result = _claim(tokens_file, "latest", "--message", "Línea uno\nLínea dos", input=f"{answer}\n")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["ticket_id"] == TICKET  # stdout stays pure JSON
    assert len(dxl["posts"]()) == 1
    assert _note(LATEST, "Línea uno\nLínea dos") in result.stderr
    assert "2026-10" in result.stderr and "45,10 €" in result.stderr and ACCOUNT in result.stderr
    assert QUESTION in result.stderr


@pytest.mark.parametrize("answer", ["n\n", "\n", "no\n", "y\n", ""])  # "" is end of input
def test_on_a_terminal_anything_but_yes_sends_nothing(tokens_file, dxl, tty, answer):
    result = _claim(tokens_file, "latest", "--message", "x", input=answer)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert _error(result) == {"type": "cancelled", "message": "not sent: the claim was not confirmed", "exit_code": 2}
    assert QUESTION in result.stderr
    assert dxl["posts"]() == []


def test_without_a_terminal_or_yes_it_refuses_before_any_request(tokens_file, dxl):
    result = _claim(tokens_file, "latest", "--message", "x", input="s\n")

    assert result.exit_code == 2
    error = _error(result)
    assert error["type"] == "usage" and "--yes" in error["message"] and "not sent" in error["message"]
    assert dxl["requests"] == []
    assert QUESTION not in result.stderr


def test_yes_skips_the_question(tokens_file, dxl, tty):
    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 0, result.stderr
    assert QUESTION not in result.stderr
    assert len(dxl["posts"]()) == 1


# --- dry run -------------------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [[], ["--yes"]])
def test_dry_run_prints_the_body_and_sends_nothing(tokens_file, dxl, extra):
    result = _claim(tokens_file, "latest", "--message", "  revisad la factura  ", "--dry-run", *extra)

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "description": "Peticion revision factura",
        "ticketType": "Peticion",
        "requestedResolutionDate": SENT_AT,
        "channel": {"id": "ECARE"},
        "note": [{"id": "Comment", "text": _note(LATEST, "revisad la factura")}],
        "@type": "TroubleTicketExtended",
        "relatedParty": [{"id": PARTY}],
    }
    assert dxl["posts"]() == []
    assert QUESTION not in result.stderr


def test_dry_run_body_is_what_a_real_send_posts(tokens_file, dxl):
    dry = json.loads(_claim(tokens_file, "latest", "--message", "x", "--dry-run").stdout)
    _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert _body(dxl["posts"]()[0]) == dry


# --- the message ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "",
        "   \n  ",
        "tab\tseparado",
        "fin de línea\r\nwindows",
        "\x1b[31mcoloreado",
        "nul\x00byte",
        "x" * 451,
        "x" * 448 + "€",  # 452 once written as "x… EUR": the limit counts the text as sent
    ],
)
@pytest.mark.parametrize("mode", ["--yes", "--dry-run"])
def test_an_invalid_message_is_refused_before_any_request(tokens_file, dxl, message, mode):
    result = _claim(tokens_file, "latest", "--message", message, mode)

    assert result.exit_code == 2
    error = _error(result)
    assert error["type"] == "usage" and "--message" in error["message"]
    if len(message.strip()) > 8:
        assert message.strip() not in result.stderr  # the text itself is never echoed
    assert dxl["requests"] == []


@pytest.mark.parametrize(
    ("message", "sent"),
    [
        ("x" * 450, "x" * 450),
        ("ñ" * 450, "ñ" * 450),  # Latin-1 letters count one each, as the textarea counts them
        ("  " + "y" * 450 + "\n", "y" * 450),
        ("x" * 446 + " €", "x" * 446 + " EUR"),  # 450 once written out
    ],
)
def test_a_message_up_to_the_forms_limit_is_sent_stripped(tokens_file, dxl, message, sent):
    result = _claim(tokens_file, "latest", "--message", message, "--yes")

    assert result.exit_code == 0, result.stderr
    assert _body(dxl["posts"]()[0])["note"][0]["text"] == _note(LATEST, sent)


@pytest.mark.parametrize(
    ("message", "sent"),
    [
        ("Me cobran 39,43 € de más", "Me cobran 39,43 EUR de más"),
        ("Me cobran 39,43€ de más", "Me cobran 39,43 EUR de más"),
        ("Me cobran €39,43 de más", "Me cobran EUR 39,43 de más"),
        ("Cuota de 5€/mes", "Cuota de 5 EUR/mes"),
        ("10 € y 5€.", "10 EUR y 5 EUR."),
        ("importe (€)", "importe (EUR)"),
        ("€", "EUR"),
        ("39,43 €\n¿Por qué?", "39,43 EUR\n¿Por qué?"),
    ],
)
def test_the_euro_sign_is_written_as_eur(message, sent):
    assert api.claim_text(message) == sent


def test_latin_1_text_is_sent_unchanged(tokens_file, dxl):
    text = "¿Por qué? Año, pingüino, ¡ya! Ç ß ø «x» 1º 2ª ½ ©"

    result = _claim(tokens_file, "latest", "--message", text, "--yes")

    assert result.exit_code == 0, result.stderr
    assert _body(dxl["posts"]()[0])["note"][0]["text"] == _note(LATEST, text)


@pytest.mark.parametrize(
    ("message", "code_point"),
    [
        ("Gracias 😀 por todo", "U+1F600"),
        ("Dicen “gratis” en la oferta", "U+201C"),
        ("El cargo — que no reconozco", "U+2014"),
        ("Cargo del día 1‒5", "U+2012"),
        ("línea\u2028separada", "U+2028"),
        ("Cargo de 3 ₤ extra", "U+20A4"),
    ],
)
@pytest.mark.parametrize("mode", ["--yes", "--dry-run"])
def test_a_character_outside_latin_1_is_refused_naming_its_code_point(tokens_file, dxl, message, code_point, mode):
    result = _claim(tokens_file, "latest", "--message", message, mode)

    assert result.exit_code == 2
    error = _error(result)
    assert error["type"] == "usage" and code_point in error["message"] and "ISO-8859-1" in error["message"]
    assert message not in result.stderr and message.split()[0] not in error["message"]
    assert dxl["requests"] == []


def test_the_preview_and_dry_run_show_the_text_as_sent(tokens_file, dxl, tty):
    dry = _claim(tokens_file, "latest", "--message", "Cobro de 39,43€", "--dry-run")
    asked = _claim(tokens_file, "latest", "--message", "Cobro de 39,43€", input="n\n")

    assert json.loads(dry.stdout)["note"][0]["text"] == _note(LATEST, "Cobro de 39,43 EUR")
    assert _note(LATEST, "Cobro de 39,43 EUR") in asked.stderr and "39,43€" not in asked.stderr
    assert dxl["posts"]() == []


def test_the_message_is_required(tokens_file, dxl):
    result = _claim(tokens_file, "latest", "--yes")

    assert result.exit_code == 2
    assert dxl["requests"] == []


# --- the invoice ---------------------------------------------------------------------------------


def test_a_claim_by_id_is_about_that_invoice(tokens_file, dxl):
    result = _claim(tokens_file, OLDER, "--message", "x", "--yes")

    assert json.loads(result.stdout)["invoice_id"] == OLDER
    assert _body(dxl["posts"]()[0])["note"][0]["text"].startswith(f"Factura {OLDER}\n")


@pytest.mark.parametrize("target", ["XX00-000000099", "all"])
def test_an_unknown_invoice_or_all_is_a_usage_error_and_sends_nothing(tokens_file, dxl, target):
    result = _claim(tokens_file, target, "--message", "x", "--yes")

    assert result.exit_code == 2
    assert _error(result)["type"] == "usage"
    assert [r for r in dxl["requests"] if r.url.path == REVIEW_PATH] == []


def test_another_accounts_invoice_is_not_found(tokens_file, dxl):
    dxl["bills"] = [_bill(LATEST, 10), _bill("XX00-000000011", 11, account=OTHER_ACCOUNT)]

    result = _claim(tokens_file, "XX00-000000011", "--message", "x", "--yes")

    assert result.exit_code == 2
    assert dxl["posts"]() == []


@pytest.mark.parametrize("bill", [_bill(LATEST, 10, account=None), _bill("XX00 000000010\n", 10)])
def test_an_invoice_without_a_billing_account_or_a_plain_number_sends_nothing(tokens_file, dxl, bill):
    dxl["bills"] = [bill]

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 7
    assert dxl["posts"]() == []


# --- POST errors: one attempt, and whether the claim was filed -----------------------------------


@pytest.mark.parametrize(
    ("reply", "exit_code", "kind"),
    [
        (httpx.Response(400, json={"message": f"relatedParty {PARTY} not valid"}), 7, "api_changed"),
        (httpx.Response(404, json={}), 7, "api_changed"),
        (httpx.Response(401, json={"message": "UNAUTHORIZED"}), 3, "access_denied"),
        (httpx.Response(403, json={"message": "FORBIDDEN"}), 5, "blocked"),
        (httpx.Response(429, json={}), 5, "blocked"),
        (httpx.Response(200, text="<html>_Incapsula_Resource</html>", headers={"content-type": "text/html"}), 5, "blocked"),
    ],
)
def test_a_refused_claim_says_it_was_not_filed(tokens_file, dxl, reply, exit_code, kind):
    dxl["post"] = reply

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == exit_code
    error = _error(result)
    assert error["type"] == kind and error["message"].endswith("the claim was not filed")
    assert PARTY not in result.stderr
    assert len(dxl["posts"]()) == 1


def _raises(exc_type):
    def reply(request):
        raise exc_type("synthetic transport failure", request=request)

    return reply


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500, json={"message": f"error for {PARTY}"}),
        httpx.Response(502, json={}),
        httpx.Response(504, text="<html>Gateway Timeout</html>", headers={"content-type": "text/html"}),
        httpx.Response(302, headers={"location": "https://m.vodafone.es/"}),
        _raises(httpx.ReadTimeout),
        _raises(httpx.WriteTimeout),
        _raises(httpx.RemoteProtocolError),
        _raises(httpx.ReadError),
    ],
)
def test_a_claim_that_may_have_arrived_is_unknown_and_never_retried(tokens_file, dxl, reply):
    dxl["post"] = reply

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 5
    error = _error(result)
    assert error["type"] == "outcome_unknown"
    assert "unknown whether the claim was filed" in error["message"]
    assert "vodafone invoices claims" in error["message"]
    assert "retry" not in error["message"].lower()
    assert PARTY not in result.stderr
    assert len(dxl["posts"]()) == 1


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_a_claim_that_never_connected_says_nothing_was_sent(tokens_file, dxl, exc_type):
    dxl["post"] = _raises(exc_type)

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 5
    error = _error(result)
    assert error["type"] == "upstream_unavailable"
    assert "nothing was sent" in error["message"] and error["message"].endswith("the claim was not filed")
    assert len(dxl["posts"]()) == 1


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(201, json={}),
        httpx.Response(201, json={"id": "bad id\n"}),
        httpx.Response(201, json=[TICKET]),
        httpx.Response(201, text="created", headers={"content-type": "text/plain"}),
        httpx.Response(204),
    ],
)
def test_an_accepted_claim_without_a_readable_ticket_id_is_probably_filed(tokens_file, dxl, reply):
    dxl["post"] = reply

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert result.exit_code == 5
    error = _error(result)
    assert error["type"] == "outcome_unknown" and "probably" in error["message"]
    assert "vodafone invoices claims" in error["message"]
    assert result.stdout == ""
    assert len(dxl["posts"]()) == 1


def test_a_200_with_a_ticket_id_is_a_success_too(tokens_file, dxl):
    dxl["post"] = httpx.Response(200, json={"id": "INC-0000001"})

    result = _claim(tokens_file, "latest", "--message", "x", "--yes")

    assert json.loads(result.stdout)["ticket_id"] == "INC-0000001"


# --- invoices claims -----------------------------------------------------------------------------


def test_claims_lists_the_reviews_with_their_note(tokens_file, dxl):
    dxl["review"] = [_review("Abierto", ticket="300000004", bill_no=LATEST), _review("Cerrado")]

    result = _claims(tokens_file)

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {
        "account_id": ACCOUNT,
        "claims": [
            {
                "id": "300000004", "status": "Abierto", "invoice_id": LATEST, "billing_account_id": ACCOUNT,
                "created_at": "2026-09-12T12:00:00.000+0000", "last_updated_at": "2026-09-20T12:00:00.000+0000",
                "expected_resolution_at": "2026-10-12T12:00:00.000+0000", "resolved_at": None, "channel": "MVA",
                "note": _note(LATEST, "no reconozco el cargo"), "details": {},
            },
            {
                "id": TICKET, "status": "Cerrado", "invoice_id": OLDER, "billing_account_id": ACCOUNT,
                "created_at": "2026-09-12T12:00:00.000+0000", "last_updated_at": "2026-09-20T12:00:00.000+0000",
                "expected_resolution_at": "2026-10-12T12:00:00.000+0000",
                "resolved_at": "2026-10-12T12:00:00.000+0000", "channel": "MVA",
                "note": _note(OLDER, "no reconozco el cargo"), "details": {},
            },
        ],
    }
    [lookup] = dxl["requests"][1:]
    assert (lookup.method, lookup.url.path) == ("GET", REVIEW_PATH)
    assert dict(lookup.url.params)["status"] == "Abierto,Reabierto,Cerrado"


def test_claims_is_empty_without_reviews(tokens_file, dxl):
    result = _claims(tokens_file)

    assert json.loads(result.stdout) == {"account_id": ACCOUNT, "claims": []}


def test_claims_reads_the_note_list_when_there_is_no_description(tokens_file, dxl):
    sparse = {"id": TICKET, "hasTicket": True, "status": "Cerrado", "note": [{"id": "Comment", "text": "otra cosa"}]}
    dxl["review"] = [sparse]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert claim["note"] == "otra cosa"
    assert claim["invoice_id"] is None and claim["billing_account_id"] is None and claim["channel"] is None
    assert claim["resolved_at"] is None  # Cerrado, but no date to show


# The shape of a closed review's notes in a live lookup: back-office fields, never the claim text.
BACK_OFFICE_NOTES = [
    {"id": "CATEG_OPERACIONAL_1", "text": "Email"},
    {"id": "Submotivo", "text": "Revisión factura"},
    {"id": "NumAveriasCliente", "text": "0"},
    {"id": "ESTADO", "text": "CLOSED"},
]


def test_claims_shows_the_back_office_notes_as_details(tokens_file, dxl):
    dxl["review"] = [{**_review("Cerrado"), "note": BACK_OFFICE_NOTES}]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert claim["details"] == {
        "CATEG_OPERACIONAL_1": "Email", "Submotivo": "Revisión factura", "NumAveriasCliente": "0", "ESTADO": "CLOSED",
    }
    assert claim["note"] == _note(OLDER, "no reconozco el cargo")


def test_claims_never_takes_a_back_office_note_for_the_claim_text(tokens_file, dxl):
    sparse = {"id": TICKET, "hasTicket": True, "status": "Cerrado", "note": BACK_OFFICE_NOTES}
    dxl["review"] = [sparse]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert claim["note"] is None
    assert claim["details"]["ESTADO"] == "CLOSED"


def test_the_claim_text_note_is_not_repeated_in_details(tokens_file, dxl):
    sparse = {"id": TICKET, "hasTicket": True, "status": "Abierto",
              "note": [{"id": "Comment", "text": "otra cosa"}, {"id": "ESTADO", "text": "OPEN"}, {"id": None, "text": "x"}]}
    dxl["review"] = [sparse]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert (claim["note"], claim["details"]) == ("otra cosa", {"ESTADO": "OPEN"})


@pytest.mark.parametrize("status", ["Cerrado", "CERRADO", " cerrado "])
def test_a_closed_claim_is_resolved_on_its_expected_date(tokens_file, dxl, status):
    dxl["review"] = [_review(status)]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert claim["resolved_at"] == claim["expected_resolution_at"] == "2026-10-12T12:00:00.000+0000"


@pytest.mark.parametrize("status", ["Abierto", "Reabierto", None])
def test_an_open_claim_has_no_resolved_date(tokens_file, dxl, status):
    dxl["review"] = [_review(status)]

    [claim] = json.loads(_claims(tokens_file).stdout)["claims"]

    assert claim["resolved_at"] is None
    assert claim["expected_resolution_at"] == "2026-10-12T12:00:00.000+0000"


def test_claims_with_an_unexpected_reply_is_api_changed(tokens_file, dxl):
    dxl["review"] = {"items": []}

    result = _claims(tokens_file)

    assert result.exit_code == 7
    assert _error(result)["type"] == "api_changed"

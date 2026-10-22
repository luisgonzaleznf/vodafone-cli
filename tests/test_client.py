"""DxlClient against httpx.MockTransport. All values are synthetic."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vodafone import api
from vodafone.client import DxlClient, describe_media_type
from vodafone.output import render
from vodafone.errors import (
    AccessDeniedError,
    ApiShapeError,
    AuthError,
    BlockedError,
    UpstreamError,
    UsageError,
    VodafoneError,
)
from vodafone.tokens import TokenSet

ACCESS = "A" * 28
CSRF = "c" * 36
ACCOUNT = "100000001"
MOBILE = "600000001"
FIBRE = "J00000ANF000"
DOCUMENT = "00000000T"


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def _tokens(*, expired: bool = False) -> TokenSet:
    now = datetime.now(UTC)
    services = [
        {"id": MOBILE, "code": "XXXX1", "type": "Mobile postpaid", "customerAccountId": ACCOUNT},
        {"id": FIBRE, "code": "XX001", "type": "Fibre", "customerAccountId": ACCOUNT},
        {"id": "600000999", "code": "XXXX1", "type": "Mobile postpaid", "customerAccountId": "999999999"},
    ]
    return TokenSet(
        access_token=ACCESS,
        csrf_token=CSRF,
        refresh_token="r" * 32,
        jwt=_jwt({"userProfile": {"services": services, "document": {"id": DOCUMENT}}}),
        access_expires_at=now + (timedelta(hours=-1) if expired else timedelta(hours=1)),
        refresh_expires_at=now + timedelta(days=80),
    )


def _client(handler, *, expired: bool = False) -> DxlClient:
    return DxlClient(_tokens(expired=expired), spacing=0, transport=httpx.MockTransport(handler))


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body, headers={"content-type": "application/vnd.vodafone.a42+json"})


def test_injects_headers_with_a_fresh_trace_id_per_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json(200, {"ok": True})

    with _client(handler) as client:
        client.get_json("/x")
        client.get_json("/x")

    h = seen[0].headers
    assert h["authorization"] == f"Bearer {ACCESS}"
    assert h["vf-csrf-token"] == CSRF
    assert h["vf-country-code"] == "ES"
    assert h["vf-target-environment"] == "aws-prod-es"
    assert h["user-agent"].startswith("Mozilla/5.0")
    assert "cookie" not in h
    assert seen[0].headers["vf-trace-transaction-id"] != seen[1].headers["vf-trace-transaction-id"]


def test_auth_posts_pair_the_csrf_cookie_with_the_header_and_keep_the_jar():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={}, headers={"set-cookie": "JSESSIONID=sess-1; Path=/"})

    csrf = "0123abcd-" + "e" * 27
    with _client(handler) as client:
        client.post("/token", data={"grant_type": "password"}, headers={"vf-csrf-token": csrf}, bearer=False)
        client.post("/otp", json={"code": "123456"}, headers={"vf-csrf-token": csrf})
        client.get_json("/identity")

    assert seen[0].headers["cookie"] == f"csrf-token={csrf}"
    assert seen[1].headers["cookie"] == f"JSESSIONID=sess-1; csrf-token={csrf}"  # the jar's cookie is kept
    assert seen[2].headers["cookie"] == "JSESSIONID=sess-1"  # the CSRF cookie never lands in the jar


@pytest.mark.parametrize("csrf", ['synthetic; injected=value; quote="', "", "a b"])
def test_an_unsafe_csrf_token_is_refused_before_anything_is_sent(csrf):
    seen = []
    with _client(lambda r: seen.append(r) or _json(200, {})) as client:
        with pytest.raises(AuthError) as exc:
            client.post("/token", data={"grant_type": "password"}, headers={"vf-csrf-token": csrf}, bearer=False)

    assert seen == []
    assert "injected" not in str(exc.value)


def test_auth_post_errors_name_the_dxl_ecode_but_never_free_text():
    body = {"code": "1007", "reason": "UNAUTHORIZED", "message": f"bad login for {MOBILE}", "status": "401", "ecode": "1007"}
    with _client(lambda r: _json(401, body)) as client:
        with pytest.raises(AuthError) as exc:
            client.post("/otp", json={"login_hint": MOBILE})

    assert "(HTTP 401: ecode 1007)" in str(exc.value)
    assert MOBILE not in str(exc.value)


def test_401_with_expired_token_means_reexport():
    with _client(lambda r: _json(401, {"message": "UNAUTHORIZED"}), expired=True) as client:
        with pytest.raises(AuthError) as exc:
            client.get_json("/x")
    assert type(exc.value) is AuthError
    assert exc.value.exit_code == 3


def test_401_with_valid_token_is_a_resource_denial_not_a_relogin():
    body = {"message": "UNAUTHORIZED", "description": "Access is denied", "transactionId": "t-1"}
    seen = []
    with _client(lambda r: seen.append(r) or _json(401, body)) as client:
        with pytest.raises(AccessDeniedError) as exc:
            client.get_json("/x")
    assert exc.value.exit_code == 3
    assert exc.value.transaction_id == seen[0].headers["vf-trace-transaction-id"]  # ours, not "t-1"
    assert "Access is denied" in exc.value.message


def test_imperva_page_is_blocked():
    html = httpx.Response(200, text="<html><iframe src='/_Incapsula_Resource?x'></iframe></html>",
                          headers={"content-type": "text/html"})
    with _client(lambda r: html) as client:
        with pytest.raises(BlockedError) as exc:
            client.get_json("/x")
    assert exc.value.exit_code == 5


def test_429_is_blocked_with_retry_hint():
    resp = httpx.Response(429, json={}, headers={"retry-after": "30"})
    with _client(lambda r: resp) as client:
        with pytest.raises(BlockedError, match="retry after 30s"):
            client.get_json("/x")


def test_5xx_reads_csm_api_error_shape_and_masks_ids():
    body = {"code": 205, "reason": "INTERNAL_SERVER_ERROR", "message": f"sin datos para el cliente {ACCOUNT}", "status": 500}
    with _client(lambda r: _json(500, body)) as client:
        with pytest.raises(UpstreamError) as exc:
            client.get_json("/x", {"nif": "00000000T"})
    assert exc.value.exit_code == 5
    assert ACCOUNT not in exc.value.message
    assert "00000000T" not in exc.value.message


def test_non_json_success_is_api_changed():
    resp = httpx.Response(200, text="not json", headers={"content-type": "text/plain"})
    with _client(lambda r: resp) as client:
        with pytest.raises(ApiShapeError) as exc:
            client.get_json("/x")
    assert exc.value.exit_code == 7


def test_secrets_stay_out_of_repr_and_errors():
    tokens = _tokens()
    assert ACCESS not in repr(tokens) and CSRF not in repr(tokens)
    with _client(lambda r: _json(403, {"message": "FORBIDDEN"})) as client:
        with pytest.raises(BlockedError) as exc:
            client.get_json("/x")
    assert ACCESS not in str(exc.value.to_dict()) and CSRF not in str(exc.value.to_dict())


def _router(routes: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json(200, routes[request.url.path])

    return handler


ACCOUNTS = {"items": [{"id": ACCOUNT, "status": "Activo", "debtAmount": 0}]}


def test_usage_uses_jwt_services_for_the_account_and_parses_allowances():
    balances = {
        "items": [
            {
                "type": "data",
                "name": "Datos móviles",
                "group": "card_data",
                "allowance": {
                    "availableAllowance": "1234", "availableAllowanceUnitCode": "GB",
                    "usedAllowance": "5.67", "usedAllowanceUnitCode": "GB",
                    "remainingAllowance": "1228.33", "remainingAllowanceUnitCode": "GB",
                    "unlimited": True,
                },
            },
            {"type": "fibre", "name": "Fibra", "group": "card_fibre"},
        ]
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(dict(request.url.params))
        return _json(200, {"/v2/customer/customerAccounts": ACCOUNTS, "/v2/service/serviceBalances": balances}[request.url.path])

    with _client(handler) as client:
        out = api.get_usage(client)

    assert [line.id for line in out.lines] == [MOBILE, FIBRE]  # the other account's line is excluded
    data = out.lines[0].balances[0]
    assert (data.used.value, data.used.unit, data.remaining.value, data.unlimited) == (5.67, "GB", 1228.33, True)
    assert out.lines[0].balances[1].total.value is None
    assert requested[1]["serviceIdentifier"] == MOBILE
    assert requested[1]["categories"] == "inPlan"


def test_usage_line_matches_unique_suffix_only():
    with _client(_router({"/v2/customer/customerAccounts": ACCOUNTS, "/v2/service/serviceBalances": {"items": []}})) as client:
        assert [line.id for line in api.get_usage(client, line=MOBILE[-4:]).lines] == [MOBILE]
        with pytest.raises(UsageError):
            api.get_usage(client, line="4242")


def test_missing_balances_items_is_api_changed():
    with _client(_router({"/v2/customer/customerAccounts": ACCOUNTS, "/v2/service/serviceBalances": {"links": {}}})) as client:
        with pytest.raises(ApiShapeError):
            api.get_usage(client, line=MOBILE)


def test_invoices_query_the_jwt_document_with_the_process_header():
    bills = [
        {
            "id": {"value": "F-1"},
            "extraId": [{"value": "123", "schemeName": "invoiceNumber"}],
            "status": "settled",
            "type": "regular",
            "details": {"dueDateTime": {"value": "2001-02-20T00:00:00"}, "chargeDateTime": {"value": "2001-02-03T00:00:00"}},
            "amountsDue": {"amount": {"value": "37.27", "currencyID": "EUR"}, "netAmount": {"value": "45.10"}},
        }
    ]
    requested = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requested[request.url.path] = request
        return _json(200, {"/v2/customer/customerAccounts": ACCOUNTS, "/es/csm-api/customerBillAPI/v3/customerBill": bills}[request.url.path])

    with _client(handler) as client:
        out = api.get_invoices(client)

    assert set(requested) == {"/v2/customer/customerAccounts", "/es/csm-api/customerBillAPI/v3/customerBill"}
    bill_request = requested["/es/csm-api/customerBillAPI/v3/customerBill"]
    assert dict(bill_request.url.params) == {
        "billingAccountId": ACCOUNT,
        "relatedParty[?(@.role=='Individual')].id": DOCUMENT,
        "relatedParty.role": "Individual",
    }
    assert bill_request.headers["x-vf-api-process"] == "ConsultarEstadoFacturas"
    inv = out.invoices[0]
    assert (inv.id, inv.number, inv.amount, inv.net_amount, inv.currency, inv.issued_at) == (
        "F-1", "123", 45.10, 37.27, "EUR", "2001-02-03T00:00:00"
    )


@pytest.mark.parametrize("raw", ["NaN", "nan", "Infinity", "-inf", "1e400", "-1e400"])
def test_non_finite_numeric_strings_are_api_changed(raw):
    with pytest.raises(ApiShapeError, match="finite"):
        api._number(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_render_refuses_non_standard_json(value):
    with pytest.raises(ValueError):
        render({"value": value})


@pytest.mark.parametrize("body", [b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}', b'{"x": 1e400}'])
def test_a_json_body_with_a_non_finite_number_is_api_changed(body):
    resp = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    with _client(lambda r: resp) as client:
        with pytest.raises(ApiShapeError, match="non-finite"):
            client.get_json("/x")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
@pytest.mark.parametrize("tid", [ACCESS, CSRF, DOCUMENT, ACCOUNT, "a1b2c3d4-0000-4000-8000-00000000000a", {"id": "t-1"}])
def test_the_transaction_id_is_always_the_requests_own_trace_id(status, tid):
    seen = []
    with _client(lambda r: seen.append(r) or _json(status, {"transactionId": tid})) as client:
        with pytest.raises(VodafoneError) as exc:
            client.get_json("/x", {"nif": DOCUMENT, "customerAccountId": ACCOUNT})
    assert exc.value.transaction_id == seen[0].headers["vf-trace-transaction-id"]
    if isinstance(tid, str):
        assert tid not in str(exc.value.to_dict())


def test_a_get_error_never_echoes_the_query_values_it_sent():
    document = "Pab12cD"  # few digits, so digit-run masking alone would not catch it
    body = {"description": f"sin datos para {document} en {ACCOUNT}", "transactionId": document}
    with _client(lambda r: _json(500, body)) as client:
        with pytest.raises(UpstreamError) as exc:
            client.get_json("/x", {"relatedParty.id": document, "billingAccountId": ACCOUNT})
    rendered = str(exc.value.to_dict())
    assert document not in rendered and ACCOUNT not in rendered
    assert "sin datos para" in exc.value.message


def test_a_download_error_never_echoes_the_download_token():
    token = "f" * 64
    body = {"message": f"no file for /v2/download/{token}"}
    with _client(lambda r: _json(500, body)) as client:
        with pytest.raises(UpstreamError) as exc:
            client.get_bytes(f"/v2/download/{token}", label="/v2/download/{hash}")
    assert token not in exc.value.message and "/v2/download/{hash}" in exc.value.message


def test_a_non_json_body_names_a_fixed_media_type_label_never_the_header():
    resp = httpx.Response(200, text="oops", headers={"content-type": f"text/plain; detail={ACCESS}; x={DOCUMENT}"})
    with _client(lambda r: resp) as client:
        with pytest.raises(ApiShapeError) as exc:
            client.get_json("/x")
    assert "(plain text)" in exc.value.message
    assert ACCESS not in exc.value.message and DOCUMENT not in exc.value.message and "detail" not in exc.value.message


@pytest.mark.parametrize(
    ("content_type", "label"),
    [
        ("application/json; charset=utf-8", "JSON"),
        ("application/vnd.vodafone.a42+json", "JSON"),
        ("text/html", "HTML"),
        ("application/pdf", "PDF"),
        ("application/xml", "XML"),
        ("text/plain; detail=s3cr3t", "plain text"),
        ("", "no content type"),
        ("s3cr3t-value", "another content type"),
        ("application/octet-stream; name=s3cr3t.json", "another content type"),  # parameters are ignored
    ],
)
def test_media_type_labels_are_fixed(content_type, label):
    assert describe_media_type(content_type) == label


def test_a_get_error_description_never_echoes_the_session_tokens():
    body = {"description": f"denied for Bearer {ACCESS} csrf {CSRF}"}
    with _client(lambda r: _json(401, body)) as client:
        with pytest.raises(AccessDeniedError) as exc:
            client.get_json("/x")
    assert "denied for Bearer" in exc.value.message
    assert ACCESS not in exc.value.message and CSRF not in exc.value.message


@pytest.mark.parametrize(
    ("retry_after", "params", "shown"),
    [
        ("30", None, True),
        ("3600", None, True),
        ("3601", None, False),  # not a plausible back-off: could be anything
        ("123456", None, False),  # e.g. an echoed OTP code
        ("1234", {"line": "1234"}, False),  # equal to a sent value
        ("12", {"customerAccountId": ACCOUNT[:4] + "12"}, False),  # part of a sent value
        ("-1", None, False),
    ],
)
def test_retry_after_is_shown_only_when_it_cannot_echo_a_sent_value(retry_after, params, shown):
    resp = httpx.Response(429, json={}, headers={"retry-after": retry_after})
    with _client(lambda r: resp) as client:
        with pytest.raises(BlockedError) as exc:
            client.get_json("/x", params)
    assert (f"retry after {retry_after}s" in exc.value.message) is shown
    assert shown or "back off" in exc.value.message


def test_an_unparseable_number_is_not_echoed():
    with pytest.raises(ApiShapeError) as exc:
        api._number("Pab12cD")
    assert "Pab12cD" not in exc.value.message


def test_a_non_numeric_retry_after_is_not_echoed():
    resp = httpx.Response(429, json={}, headers={"retry-after": f"later {ACCESS}"})
    with _client(lambda r: resp) as client:
        with pytest.raises(BlockedError) as exc:
            client.get_json("/x")
    assert ACCESS not in exc.value.message and "back off" in exc.value.message

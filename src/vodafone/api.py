"""Read operations over DxlClient.

Each public function returns an output model from models.py; the CLI and the MCP server both
call these through run_with_client.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, TypeAdapter, ValidationError

from . import config
from .client import DxlClient
from .errors import ApiShapeError, AuthError, UsageError, VodafoneError
from .models import (
    Account,
    Balance,
    DownloadOutput,
    Holder,
    Invoice,
    InvoiceFile,
    InvoicesOutput,
    Line,
    LinesOutput,
    LineUsage,
    MeOutput,
    Quantity,
    Tariff,
    UsageOutput,
    WireAccountDetail,
    WireBalances,
    WireBill,
    WireBillDocuments,
    WireBillDocumentsItem,
    WireCustomerAccount,
    WireCustomerAccounts,
    WireService,
    WireTariffs,
    WireUserProfile,
)
from .tokens import FileTokenStore

T = TypeVar("T")

# TMF678 bill query as the web app's Facturas screen sends it: the related party is the logged-in
# user's identity document, and the backend answers 500 unless the process header is present.
_RELATED_PARTY_ID = "relatedParty[?(@.role=='Individual')].id"
_BILL_PROCESS_HEADER = {"x-vf-api-process": "ConsultarEstadoFacturas"}


def run_with_client(tokens_path: Path, fn: Callable[[DxlClient], T]) -> T:
    """Load the token set, open a DxlClient and call `fn` with it.

    The token file is re-read on every call, so a re-exported file is picked up without a restart.
    Anything other than a VodafoneError is re-raised as one carrying only the exception type, since
    messages can embed request URLs or headers.
    """
    try:
        tokens = FileTokenStore(tokens_path).load()
        with DxlClient(tokens) as client:
            return fn(client)
    except VodafoneError:
        raise
    except Exception as exc:
        raise VodafoneError(f"unexpected internal error: {type(exc).__name__}") from None


def _parse(model: type[T], data: Any, what: str) -> T:
    try:
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(data)
        return TypeAdapter(model).validate_python(data)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})[:5]
        raise ApiShapeError(f"unexpected {what} response shape (fields: {', '.join(fields)})")


def _number(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        raise ApiShapeError(f"expected a numeric string, got {raw!r}")


def _kind(service_type: str | None) -> str:
    t = (service_type or "").lower()
    if "mobile" in t or "móvil" in t:
        return "mobile"
    if "landline" in t or "fijo" in t:
        return "landline"
    if "fibre" in t or "fiber" in t or "fibra" in t:
        return "fibre"
    return "other"


# --- accounts ------------------------------------------------------------------------------------


def list_accounts(client: DxlClient) -> list[WireCustomerAccount]:
    data = client.get_json("/v2/customer/customerAccounts", {"fields": "debtAmount"})
    return _parse(WireCustomerAccounts, data, "customerAccounts").items


def resolve_account_id(client: DxlClient, account_id: str | None) -> str:
    accounts = list_accounts(client)
    ids = [a.id for a in accounts]
    if account_id:
        if account_id not in ids:
            raise UsageError(f"account {account_id!r} is not one of this login's {len(ids)} customer account(s)")
        return account_id
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ApiShapeError("customerAccounts returned no accounts")
    raise UsageError(f"this login has {len(ids)} customer accounts; pass --account (see `vodafone me`)")


def get_me(client: DxlClient) -> MeOutput:
    out = []
    for acct in list_accounts(client):
        detail = _parse(WireAccountDetail, client.get_json(f"/v2/customer/customerAccounts/{acct.id}"), "customerAccount")
        out.append(
            Account(
                id=acct.id,
                status=acct.status,
                type=acct.type,
                client_type=acct.clientType,
                market_type=acct.marketType,
                market_segment=acct.marketSegment,
                overdue=acct.overdue,
                debt_amount=acct.debtAmount,
                digital=acct.digital,
                smart_pay=acct.smartPay,
                one_plus=acct.onePlus,
                one_plus_micro=acct.onePlusMicro,
                address=acct.address.formattedAddress if acct.address else None,
                holder=Holder(
                    first_name=detail.firstName,
                    family_name=detail.familyName,
                    email=detail.email,
                    msisdn=detail.msisdn,
                ),
            )
        )
    return MeOutput(accounts=out)


# --- lines ---------------------------------------------------------------------------------------


def _user_profile(client: DxlClient) -> WireUserProfile:
    claims = client.tokens.claims()
    if not claims:
        raise AuthError("the token set has no readable identity JWT (mcare-LocalStore.key.jwt); re-export tokens.json")
    return _parse(WireUserProfile, claims.get("userProfile"), "JWT userProfile")


def account_services(client: DxlClient, account_id: str) -> list[WireService]:
    """The account's lines, from the identity JWT's userProfile.services.

    The JWT is the only source with every line's real service id: billingAccounts lists fibre
    under a display label ("Internet Fibra") that tariffs/serviceBalances reject with 401.
    """
    return [s for s in _user_profile(client).services if s.customerAccountId == account_id]


def get_tariff(client: DxlClient, account_id: str, service_id: str) -> Tariff | None:
    data = client.get_json("/v2/product/tariffs", {"customerAccountId": account_id, "subscriptionId": service_id})
    items = _parse(WireTariffs, data, "tariffs").items
    if not items:
        return None
    t = items[0]
    return Tariff(code=t.code, name=t.name, status=t.status)


def get_lines(client: DxlClient, account_id: str | None = None) -> LinesOutput:
    acct = resolve_account_id(client, account_id)
    lines = [
        Line(
            id=svc.id,
            kind=_kind(svc.type),
            type=svc.type,
            service_code=svc.code,
            tariff=get_tariff(client, acct, svc.id),
        )
        for svc in account_services(client, acct)
    ]
    return LinesOutput(account_id=acct, lines=lines)


def match_line(services: list[WireService], query: str) -> WireService:
    """Exact service id, else a unique suffix (e.g. the last 4 digits of the number)."""
    q = query.strip().replace(" ", "")
    for svc in services:
        if svc.id == q:
            return svc
    matches = [svc for svc in services if q and svc.id.endswith(q)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise UsageError(f"--line {query!r} matches {len(matches)} lines; give more digits (see `vodafone lines`)")
    raise UsageError(f"--line {query!r} matches none of the account's {len(services)} lines (see `vodafone lines`)")


# --- usage ---------------------------------------------------------------------------------------


def get_balances(client: DxlClient, account_id: str, service_id: str) -> list[Balance]:
    data = client.get_json(
        "/v2/service/serviceBalances",
        {"customerAccountId": account_id, "serviceIdentifier": service_id, "categories": "inPlan", "group": "true"},
    )
    out = []
    for item in _parse(WireBalances, data, "serviceBalances").items:
        a = item.allowance
        out.append(
            Balance(
                type=item.type,
                name=item.name,
                group=item.group,
                unlimited=a.unlimited if a else None,
                total=Quantity(value=_number(a and a.availableAllowance), unit=a and a.availableAllowanceUnitCode),
                used=Quantity(value=_number(a and a.usedAllowance), unit=a and a.usedAllowanceUnitCode),
                remaining=Quantity(value=_number(a and a.remainingAllowance), unit=a and a.remainingAllowanceUnitCode),
            )
        )
    return out


def get_usage(client: DxlClient, account_id: str | None = None, line: str | None = None) -> UsageOutput:
    acct = resolve_account_id(client, account_id)
    services = account_services(client, acct)
    if line is not None:
        services = [match_line(services, line)]
    lines = [
        LineUsage(id=svc.id, kind=_kind(svc.type), type=svc.type, balances=get_balances(client, acct, svc.id))
        for svc in services
    ]
    return UsageOutput(account_id=acct, lines=lines)


# --- invoices ------------------------------------------------------------------------------------


def get_invoices(client: DxlClient, account_id: str | None = None) -> InvoicesOutput:
    acct = resolve_account_id(client, account_id)
    document = _user_profile(client).document
    if document is None:
        raise ApiShapeError("the identity JWT has no userProfile.document, which the invoice query needs")

    data = client.get_json(
        "/es/csm-api/customerBillAPI/v3/customerBill",
        {"billingAccountId": acct, _RELATED_PARTY_ID: document.id, "relatedParty.role": "Individual"},
        headers=_BILL_PROCESS_HEADER,
    )
    invoices = []
    for bill in _parse(list[WireBill], data, "customerBill"):
        amounts = bill.amountsDue
        details = bill.details
        invoices.append(
            Invoice(
                id=bill.id.value if bill.id else None,
                number=bill.extraId[0].value if bill.extraId else None,
                billing_account_id=acct,
                status=bill.status,
                type=bill.type,
                issued_at=details.chargeDateTime.value if details and details.chargeDateTime else None,
                due_at=details.dueDateTime.value if details and details.dueDateTime else None,
                amount=_number(amounts.amount.value) if amounts and amounts.amount else None,
                net_amount=_number(amounts.netAmount.value) if amounts and amounts.netAmount else None,
                currency=amounts.amount.currencyID if amounts and amounts.amount else None,
            )
        )
    return InvoicesOutput(account_id=acct, invoices=invoices)


# --- invoice PDFs ----------------------------------------------------------------------------------


def _bill_month(bill: WireBillDocumentsItem) -> str | None:
    cycle = bill.billingCycle
    if cycle and cycle.billingYear and cycle.billingMonth and cycle.billingMonth.isdigit():
        return f"{cycle.billingYear}-{int(cycle.billingMonth):02d}"
    if bill.creationDate and len(bill.creationDate) >= 7:
        return bill.creationDate[:7]
    return None


def _file_names(bills: list[WireBillDocumentsItem]) -> dict[str, str]:
    """factura_YYYY-MM.pdf per bill number; bills sharing a month get a _<billNo> suffix."""
    months = [_bill_month(b) for b in bills]
    names = {}
    for bill, month in zip(bills, months):
        if month is None:
            names[bill.billNo] = f"factura_{bill.billNo}.pdf"
        elif months.count(month) > 1:
            names[bill.billNo] = f"factura_{month}_{bill.billNo}.pdf"
        else:
            names[bill.billNo] = f"factura_{month}.pdf"
    return names


def _select_bills(bills: list[WireBillDocumentsItem], target: str) -> list[WireBillDocumentsItem]:
    newest_first = sorted(bills, key=lambda b: (b.creationDate or "", _bill_month(b) or ""), reverse=True)
    if target == "all":
        return newest_first
    if not bills:
        raise UsageError("this account has no invoices to download")
    if target == "latest":
        return newest_first[:1]
    for bill in bills:
        if bill.billNo == target:
            return [bill]
    raise UsageError(f"no invoice with id {target!r}; ids are listed by `vodafone invoices list`")


def _download_path(bill: WireBillDocumentsItem) -> str:
    """The /v2/download/{hash} path from the bill's document link, checked to stay on the API host."""
    href = next((d.links.download.href for d in bill.documents if d.links and d.links.download), None)
    if not href:
        raise ApiShapeError("a bill in customerBills has no document download link")
    url = urlsplit(href)
    if url.netloc != urlsplit(config.API_BASE).netloc or not url.path.startswith("/v2/download/"):
        raise ApiShapeError("a bill's download link points outside the DXL download endpoint")
    return url.path


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def download_invoices(
    client: DxlClient, target: str, directory: Path, account_id: str | None = None
) -> DownloadOutput:
    """Save invoice PDFs: `target` is a bill id, "latest" or "all".

    Bills come from /v3/payment/customerBills?type=bill, whose documents carry the per-bill
    /v2/download/{hash} links. Downloads run one at a time, DOWNLOAD_SPACING apart (a burst stalls
    the backend), and stop at the first failure. Existing files are skipped, never overwritten.
    """
    acct = resolve_account_id(client, account_id)
    data = client.get_json("/v3/payment/customerBills", {"type": "bill"})
    bills = [
        b for b in _parse(WireBillDocuments, data, "customerBills").items
        if b.billingAccount is None or b.billingAccount.id in (None, acct)
    ]
    names = _file_names(bills)
    selected = _select_bills(bills, target)

    directory.mkdir(parents=True, exist_ok=True)
    results: list[InvoiceFile] = []
    fetched = 0
    for bill in selected:
        path = directory / names[bill.billNo]
        month = _bill_month(bill)
        if path.exists():
            results.append(InvoiceFile(id=bill.billNo, month=month, file=str(path), bytes=path.stat().st_size, status="skipped"))
            continue
        if fetched:
            time.sleep(config.DOWNLOAD_SPACING)
        try:
            body, content_type = client.get_bytes(_download_path(bill), label="/v2/download/{hash}")
            if "pdf" not in content_type or not body.startswith(b"%PDF-"):
                raise ApiShapeError(f"invoice {bill.billNo} download did not return a PDF ({content_type or 'no content type'})")
            _write_atomic(path, body)
        except VodafoneError as err:
            done = [r.file for r in results if r.status == "downloaded"]
            err.message = f"{err.message} (stopped at invoice {bill.billNo}; downloaded before stopping: {done or 'none'})"
            raise
        fetched += 1
        results.append(InvoiceFile(id=bill.billNo, month=month, file=str(path), bytes=len(body), status="downloaded"))
    return DownloadOutput(account_id=acct, directory=str(directory), invoices=results)

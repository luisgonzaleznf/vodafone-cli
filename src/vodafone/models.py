"""Pydantic models.

`Wire*` models mirror DXL responses (camelCase, unknown fields ignored, only what we read).
The snake_case output models define the stable JSON the CLI prints.
"""

from pydantic import BaseModel, ConfigDict


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


# --- /v2/customer/customerAccounts[?fields=debtAmount] ---------------------------------------


class WireFormattedAddress(Wire):
    formattedAddress: str | None = None


class WireCustomerAccount(Wire):
    id: str
    clientType: str | None = None
    type: str | None = None
    status: str | None = None
    address: WireFormattedAddress | None = None
    overdue: bool | None = None
    debtAmount: float | None = None
    marketType: str | None = None
    marketSegment: str | None = None
    digital: bool | None = None
    smartPay: bool | None = None
    onePlus: bool | None = None
    onePlusMicro: bool | None = None


class WireCustomerAccounts(Wire):
    items: list[WireCustomerAccount]


# --- /v2/customer/customerAccounts/{id} --------------------------------------------------------


class WireAccountDetail(Wire):
    id: str
    firstName: str | None = None
    familyName: str | None = None
    email: str | None = None
    msisdn: str | None = None


# --- identity JWT claims: userProfile (line inventory, identity document) ----------------------


class WireService(Wire):
    id: str  # MSISDN for mobile/landline; an opaque service id for fibre
    code: str | None = None
    type: str | None = None  # "Mobile postpaid" | "landline" | "Fibre" ...
    customerAccountId: str | None = None


class WireIdentityDocument(Wire):
    id: str  # the logged-in user's document; the TMF678 relatedParty id. Never printed.


class WireUserProfile(Wire):
    services: list[WireService]
    document: WireIdentityDocument | None = None


# --- /v2/product/tariffs?customerAccountId&subscriptionId --------------------------------------


class WireTariff(Wire):
    code: str | None = None
    name: str | None = None
    serviceType: str | None = None
    status: str | None = None


class WireTariffs(Wire):
    items: list[WireTariff]


# --- /v2/service/serviceBalances?customerAccountId&serviceIdentifier&categories=inPlan&group=true


class WireAllowance(Wire):
    availableAllowance: str | None = None
    usedAllowance: str | None = None
    remainingAllowance: str | None = None
    availableAllowanceUnitCode: str | None = None
    usedAllowanceUnitCode: str | None = None
    remainingAllowanceUnitCode: str | None = None
    unlimited: bool | None = None


class WireBalance(Wire):
    type: str | None = None
    name: str | None = None
    group: str | None = None
    allowance: WireAllowance | None = None


class WireBalances(Wire):
    items: list[WireBalance]


# --- /es/csm-api/customerBillAPI/v3/customerBill (TMF678 customer bills) -----------------------


class WireValue(Wire):
    value: str | None = None


class WireExtraId(Wire):
    value: str | None = None
    schemeName: str | None = None


class WireAmount(Wire):
    value: str | None = None
    currencyID: str | None = None


class WireAmountsDue(Wire):
    amount: WireAmount | None = None
    netAmount: WireAmount | None = None


class WireBillDetails(Wire):
    dueDateTime: WireValue | None = None
    chargeDateTime: WireValue | None = None


class WireBill(Wire):
    id: WireValue | None = None
    extraId: list[WireExtraId] = []
    type: str | None = None
    status: str | None = None
    details: WireBillDetails | None = None
    amountsDue: WireAmountsDue | None = None


# --- /v3/payment/customerBills?type=bill (bill documents with their PDF download links) --------


class WireLink(Wire):
    href: str | None = None


class WireDocumentLinks(Wire):
    download: WireLink | None = None


class WireBillDocument(Wire):
    links: WireDocumentLinks | None = None


class WireBillingCycle(Wire):
    billingYear: str | None = None
    billingMonth: str | None = None


class WireBillingAccountRef(Wire):
    id: str | None = None


class WireBillDocumentsItem(Wire):
    billNo: str  # same value as the TMF678 bill id.value
    creationDate: str | None = None
    status: str | None = None
    billingCycle: WireBillingCycle | None = None
    billingAccount: WireBillingAccountRef | None = None
    documents: list[WireBillDocument] = []


class WireBillDocuments(Wire):
    items: list[WireBillDocumentsItem]


# --- CLI output ----------------------------------------------------------------------------------


class Holder(BaseModel):
    first_name: str | None
    family_name: str | None
    email: str | None
    msisdn: str | None


class Account(BaseModel):
    id: str
    status: str | None
    type: str | None
    client_type: str | None
    market_type: str | None
    market_segment: str | None
    overdue: bool | None
    debt_amount: float | None
    digital: bool | None
    smart_pay: bool | None
    one_plus: bool | None
    one_plus_micro: bool | None
    address: str | None
    holder: Holder | None


class MeOutput(BaseModel):
    accounts: list[Account]


class Tariff(BaseModel):
    code: str | None
    name: str | None
    status: str | None


class Line(BaseModel):
    id: str
    kind: str  # mobile | landline | fibre | other
    type: str | None
    service_code: str | None
    tariff: Tariff | None


class LinesOutput(BaseModel):
    account_id: str
    lines: list[Line]


class Quantity(BaseModel):
    value: float | None
    unit: str | None


class Balance(BaseModel):
    type: str | None  # data | voice | SMS | fixed
    name: str | None
    group: str | None
    unlimited: bool | None
    total: Quantity
    used: Quantity
    remaining: Quantity


class LineUsage(BaseModel):
    id: str
    kind: str
    type: str | None
    balances: list[Balance]


class UsageOutput(BaseModel):
    account_id: str
    lines: list[LineUsage]


class Invoice(BaseModel):
    id: str | None
    number: str | None
    billing_account_id: str
    status: str | None
    type: str | None
    issued_at: str | None
    due_at: str | None
    amount: float | None
    net_amount: float | None
    currency: str | None


class InvoicesOutput(BaseModel):
    account_id: str
    invoices: list[Invoice]


class InvoiceFile(BaseModel):
    id: str  # bill number, the same id `invoices list` prints
    month: str | None  # YYYY-MM billing month
    file: str
    bytes: int
    status: str  # downloaded | skipped (file already existed)


class DownloadOutput(BaseModel):
    account_id: str
    directory: str
    invoices: list[InvoiceFile]

"""Pydantic models.

`Wire*` models mirror DXL responses (camelCase, unknown fields ignored, only what we read).
The snake_case output models define the stable JSON the CLI prints.
"""

from pydantic import BaseModel, ConfigDict, Field


class Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


# --- OAuth2 token endpoint (password / authorization_code / refresh_token grants) --------------


class WireTokenResponse(Wire):
    access_token: str = Field(pattern=r"^[!-~]+$")  # sent as a bearer header: non-blank, visible ASCII
    expires_at: str | None = None  # epoch seconds, as a string
    expires_in: str | int | None = None
    refresh_token: str | None = None
    refresh_token_expires: str | None = None  # epoch seconds, as a string
    jws: str | None = None  # password grant: signed claims incl. the masked numbers for the SMS step


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
    amount: WireAmount | None = None  # before tax
    netAmount: WireAmount | None = None  # the total to pay, IVA included, despite the name


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
    amount: float | None  # the total to pay, IVA included
    net_amount: float | None  # before tax
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


class LoginOutput(BaseModel):
    tokens_file: str
    access_token_expires_at: str
    refresh_token_expires_at: str | None
    jwt_fetched: bool


class RefreshOutput(BaseModel):
    tokens_file: str
    access_token_expires_at: str
    refresh_token_rotated: bool
    refresh_token_expires_at: str | None
    jwt_refreshed: bool


class AccessTokenStatus(BaseModel):
    expires_at: str | None
    expired: bool
    seconds_remaining: int | None


class RefreshTokenStatus(BaseModel):
    present: bool
    expires_at: str | None
    expired: bool
    days_remaining: float | None


class JwtStatus(BaseModel):
    present: bool
    expires_at: str | None


class TokenStatusOutput(BaseModel):
    """`vodafone auth status`."""

    store: str
    tokens_file: str
    exported_at: str | None
    access_token: AccessTokenStatus
    refresh_token: RefreshTokenStatus
    jwt: JwtStatus


class AuthStatusOutput(BaseModel):
    """The MCP server's auth_status: `vodafone auth status` plus whether the read tools can run.
    Without a readable token file, the token fields are null."""

    signed_in: bool = Field(
        description=(
            "True if the read tools can run: the access token is valid or a refresh token can renew it, the "
            "identity JWT is readable, and no browser sign-in is in progress."
        )
    )
    reason: str | None = Field(description="Why the user is not signed in; null when signed in.")
    store: str
    tokens_file: str
    exported_at: str | None
    access_token: AccessTokenStatus | None
    refresh_token: RefreshTokenStatus | None
    jwt: JwtStatus | None

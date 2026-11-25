from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, EmailStr, StringConstraints, field_validator
from zoneinfo import ZoneInfo


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

Password = Annotated[str, StringConstraints(strip_whitespace=False)]

class Register(Input):
    email: EmailStr
    password: Password = Field(min_length=10, max_length=128)
    name: str = Field(min_length=2, max_length=120)
    organization: str = Field(min_length=2, max_length=200)
    inn: str | None = Field(default=None, pattern=r"^(\d{10}|\d{12})$")
    organization_type: Literal["buyer", "supplier", "both"] = "both"
    industries: list[str] = Field(default_factory=list, max_length=20)


class Login(Input):
    email: EmailStr
    password: Password = Field(min_length=1, max_length=128)


class OrganizationUpdate(Input):
    name: str = Field(min_length=2, max_length=200)
    type: Literal["buyer", "supplier", "both"]
    description: str = Field(default="", max_length=10000)
    industries: list[str] = Field(default_factory=list, max_length=20)
    regions: list[str] = Field(default_factory=list, max_length=30)
    timezone: str = "Europe/Moscow"
    auto_search: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("Unknown timezone")
        return value


class Invite(Input):
    email: EmailStr
    role: Literal["manager", "employee", "viewer"] = "employee"


class AcceptInvite(Input):
    token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=2, max_length=120)
    password: Password = Field(min_length=10, max_length=128)


class MemberUpdate(Input):
    role: Literal["manager", "employee", "viewer"]
    permissions: dict[Literal["view_team", "view_counterparties", "approve", "send", "export"], bool] = Field(default_factory=dict)


class TaskCreate(Input):
    direction: Literal["purchase", "sales"]
    title: str = Field(min_length=2, max_length=200)
    quantity: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=200)
    budget: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    deadline: date | None = None
    owner_id: str | None = None
    requirements: str = Field(default="", max_length=20000)


class RequirementsInput(Input):
    text: str = Field(min_length=10, max_length=20000)
    expected_version: int = Field(ge=1)
    approve: bool = True


class TaskStateInput(Input):
    status: Literal["closed", "cancelled"]


class Characteristic(Input):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=500)
    unit: str = Field(default="", max_length=50)
    required: bool = True


class ItemInput(Input):
    name: str = Field(min_length=1, max_length=200)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=4)
    unit: str = Field(default="", max_length=60)
    characteristics: list[Characteristic] = Field(default_factory=list, max_length=50)


class CounterpartyInput(Input):
    name: str = Field(min_length=2, max_length=200)
    inn: str | None = Field(default=None, pattern=r"^(\d{10}|\d{12})$")
    region: str = Field(default="", max_length=200)
    category: str = Field(default="", max_length=160)
    email: EmailStr | None = None
    website: str | None = Field(default=None, max_length=500, pattern=r"^https?://")
    source: str = Field(min_length=3, max_length=3000)


class CounterpartyLink(Input):
    counterparty_id: str
    item_ids: list[str] = Field(default_factory=list, max_length=50)


class SalesOutcome(Input):
    positive_response: bool = False
    deal_confirmed: bool = False
    deal_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    basis: str = Field(min_length=3, max_length=2000)


class CatalogInput(Input):
    name: str = Field(min_length=2, max_length=200)
    category: str = Field(default="", max_length=160)
    unit: str = Field(default="", max_length=50)
    characteristics: list[Characteristic] = Field(default_factory=list, max_length=50)
    price: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    currency: Literal["RUB"] = "RUB"
    valid_until: date | None = None
    source: str = Field(default="", max_length=3000)


class Evidence(Input):
    requirement: str = Field(min_length=1, max_length=300)
    confirmed: bool
    source: str = Field(min_length=3, max_length=3000)
    locator: str = Field(min_length=1, max_length=200)
    excerpt: str = Field(min_length=1, max_length=3000)


class QuoteInput(Input):
    counterparty_id: str
    document_id: str | None = None
    previous_id: str | None = None
    requirements_version: int = Field(ge=1)
    kind: Literal["received", "sent"] = "received"
    amount: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    currency: Literal["RUB"] = "RUB"
    delivery_included: bool | None = None
    delivery_cost: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    delivery_days: int | None = Field(default=None, ge=0, le=3650)
    terms: str = Field(default="", max_length=3000)
    tax: str = Field(default="", max_length=1000)
    valid_until: date | None = None
    assessment: Literal["matches", "partial", "does_not_match", "needs_clarification"] = "needs_clarification"
    evidence: list[Evidence] = Field(default_factory=list, max_length=100)
    matching_items: list[str] = Field(default_factory=list, max_length=50)


class SelectionInput(Input):
    comment: str = Field(default="", max_length=2000)


class OutreachInput(Input):
    counterparty_id: str
    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=3, max_length=30000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    item_ids: list[str] = Field(default_factory=list, max_length=50)
    kind: Literal["initial", "reminder", "clarification"] = "initial"
    purpose: Literal["procurement_request", "requested_quote", "business_reply", "advertising"] = "procurement_request"
    basis: str = Field(min_length=3, max_length=3000)
    expected_version: int | None = Field(default=None, ge=1)


class ApprovalDecision(Input):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=2000)


class PeriodInput(Input):
    period: Literal["month", "quarter", "year"]


class MessageInput(Input):
    external_id: str = Field(min_length=1, max_length=300)
    campaign_id: str | None = None
    counterparty_id: str | None = None
    sender: EmailStr
    recipient: EmailStr
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(max_length=100000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    is_auto_reply: bool = False


class JobInput(Input):
    kind: Literal["search", "analyze", "draft"]
    query: str = Field(default="", max_length=300)
    sources: list[Literal["web", "onlinecontract", "online_tender", "tender_online"]] = Field(default_factory=lambda: ["web", "onlinecontract", "online_tender", "tender_online"], min_length=1, max_length=4)
    active_only: bool = True
    limit: int = Field(default=10, ge=1, le=50)

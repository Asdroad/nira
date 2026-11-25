from datetime import datetime, timezone, date
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import String, Text, ForeignKey, UniqueConstraint, CheckConstraint, Numeric, JSON
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id():
    return str(uuid4())


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(default=now)


class TenantRecord(Record):
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)


class Organization(Record, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(200))
    inn: Mapped[str | None] = mapped_column(String(12), unique=True)
    type: Mapped[str] = mapped_column(String(20), default="both")
    description: Mapped[str] = mapped_column(Text, default="")
    industries: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    timezone: Mapped[str] = mapped_column(String(80), default="Europe/Moscow")
    settings: Mapped[dict] = mapped_column(JSON, default=lambda: {"require_approval": True, "auto_search": True})
    lock_version: Mapped[int] = mapped_column(default=0)


class User(Record, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(default=True)


class Membership(TenantRecord, Base):
    __tablename__ = "memberships"
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="active")
    slot: Mapped[int | None] = mapped_column()
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_member_email"),
        UniqueConstraint("organization_id", "slot", name="uq_member_slot"),
        CheckConstraint("slot IS NULL OR (slot = 0 AND role = 'owner') OR (slot BETWEEN 1 AND 5 AND role != 'owner')", name="ck_employee_slots"),
    )


class Invitation(TenantRecord, Base):
    __tablename__ = "invitations"
    membership_id: Mapped[str] = mapped_column(ForeignKey("memberships.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]


class Subscription(TenantRecord, Base):
    __tablename__ = "subscriptions"
    status: Mapped[str] = mapped_column(String(24), default="awaiting_payment")
    period: Mapped[str] = mapped_column(String(12), default="month")
    starts_at: Mapped[datetime | None]
    ends_at: Mapped[datetime | None]
    quota_anchor: Mapped[datetime] = mapped_column(default=now)
    auto_renew: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("organization_id", name="uq_subscription_org"),)


class Usage(TenantRecord, Base):
    __tablename__ = "usage"
    period_start: Mapped[date]
    resource: Mapped[str] = mapped_column(String(32))
    used: Mapped[int] = mapped_column(default=0)
    reserved: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("organization_id", "period_start", "resource", name="uq_usage_period"),)


class Campaign(TenantRecord, Base):
    __tablename__ = "campaigns"
    code: Mapped[int]
    direction: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(200))
    budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    deadline: Mapped[date | None]
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="requirements")
    requirements_version: Mapped[int] = mapped_column(default=1)
    requirements: Mapped[str] = mapped_column(Text, default="")
    requirements_approved: Mapped[bool] = mapped_column(default=False)
    selected_quote_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("organization_id", "code", name="uq_campaign_code"),)


class RequirementsVersion(TenantRecord, Base):
    __tablename__ = "requirements_versions"
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    version: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("campaign_id", "version", name="uq_requirements_version"),)


class RequestItem(TenantRecord, Base):
    __tablename__ = "request_items"
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit: Mapped[str] = mapped_column(String(60), default="")
    characteristics: Mapped[list] = mapped_column(JSON, default=list)


class Counterparty(TenantRecord, Base):
    __tablename__ = "counterparties"
    name: Mapped[str] = mapped_column(String(200))
    inn: Mapped[str | None] = mapped_column(String(12))
    region: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str | None] = mapped_column(String(254))
    website: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(Text)
    blocked: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("organization_id", "inn", name="uq_counterparty_inn"),)


class CampaignCounterparty(TenantRecord, Base):
    __tablename__ = "campaign_counterparties"
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    counterparty_id: Mapped[str] = mapped_column(ForeignKey("counterparties.id"))
    item_ids: Mapped[list] = mapped_column(JSON, default=list)
    positive_response: Mapped[bool] = mapped_column(default=False)
    deal_confirmed: Mapped[bool] = mapped_column(default=False)
    deal_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    __table_args__ = (UniqueConstraint("campaign_id", "counterparty_id", name="uq_campaign_counterparty"),)


class CatalogItem(TenantRecord, Base):
    __tablename__ = "catalog_items"
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(160), default="")
    unit: Mapped[str] = mapped_column(String(50), default="")
    characteristics: Mapped[list] = mapped_column(JSON, default=list)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    valid_until: Mapped[date | None]
    source: Mapped[str] = mapped_column(Text, default="")


class Document(TenantRecord, Base):
    __tablename__ = "documents"
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(240))
    extension: Mapped[str] = mapped_column(String(12))
    size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    blob: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("organization_id", "campaign_id", "sha256", name="uq_document_scope_hash"),)


class Quote(TenantRecord, Base):
    __tablename__ = "quotes"
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    counterparty_id: Mapped[str] = mapped_column(ForeignKey("counterparties.id"))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"))
    version: Mapped[int] = mapped_column(default=1)
    current: Mapped[bool] = mapped_column(default=True)
    requirements_version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(12), default="received")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    delivery_included: Mapped[bool | None]
    delivery_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    delivery_days: Mapped[int | None]
    terms: Mapped[str] = mapped_column(Text, default="")
    tax: Mapped[str] = mapped_column(Text, default="")
    valid_until: Mapped[date | None]
    assessment: Mapped[str] = mapped_column(String(24), default="needs_clarification")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    matching_items: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class Outreach(TenantRecord, Base):
    __tablename__ = "outreach"
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    counterparty_id: Mapped[str] = mapped_column(ForeignKey("counterparties.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16), default="initial")
    channel: Mapped[str] = mapped_column(String(20), default="email")
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attachment_ids: Mapped[list] = mapped_column(JSON, default=list)
    item_ids: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(default=1)
    requirements_version: Mapped[int]
    purpose: Mapped[str] = mapped_column(String(30), default="procurement_request")
    basis: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(24), default="draft")
    external_id: Mapped[str | None] = mapped_column(String(300))
    accepted_at: Mapped[datetime | None]


class Approval(TenantRecord, Base):
    __tablename__ = "approvals"
    outreach_id: Mapped[str] = mapped_column(ForeignKey("outreach.id"), index=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None]
    comment: Mapped[str] = mapped_column(Text, default="")


class Message(TenantRecord, Base):
    __tablename__ = "messages"
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    counterparty_id: Mapped[str | None] = mapped_column(ForeignKey("counterparties.id"))
    external_id: Mapped[str] = mapped_column(String(300))
    direction: Mapped[str] = mapped_column(String(12), default="incoming")
    sender: Mapped[str] = mapped_column(String(254))
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    attachment_ids: Mapped[list] = mapped_column(JSON, default=list)
    is_auto_reply: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("organization_id", "external_id", name="uq_message_external"),)


class Job(TenantRecord, Base):
    __tablename__ = "jobs"
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)


class AuditEvent(TenantRecord, Base):
    __tablename__ = "audit_events"
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str | None] = mapped_column(String(36))
    description: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

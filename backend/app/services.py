import calendar
import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select, update, func
from .models import Organization, Subscription, Usage, AuditEvent, Campaign, CampaignCounterparty, Outreach, Quote, User, Counterparty, now

QUOTAS = {"tasks": 100, "search_queries": 300, "web_pages": 2000, "documents": 200, "document_pages": 2000, "outgoing": 500, "ai_runs": 300}
TERMINAL_TASKS = {"closed", "cancelled"}

def require_open_task(task):
    if task.status in TERMINAL_TASKS:
        raise HTTPException(409, "Задача завершена или отменена. Создайте новую задачу")


def lock_organization(db, organization_id):
    # SQLite obtains the write lock; PostgreSQL locks this row until commit.
    db.execute(update(Organization).where(Organization.id == organization_id).values(lock_version=Organization.lock_version + 1))


def audit(db, actor, action, obj=None, description="", campaign_id=None, details=None):
    event = AuditEvent(organization_id=actor.organization.id, user_id=actor.user.id, action=action, entity_id=getattr(obj, "id", None), campaign_id=campaign_id, description=description, details=details or {})
    db.add(event)


def add_months(value, count, anchor_day=None):
    index = value.year * 12 + value.month - 1 + count
    year, month0 = divmod(index, 12)
    month = month0 + 1
    return value.replace(year=year, month=month, day=min(anchor_day or value.day, calendar.monthrange(year, month)[1]))


def subscription_for(db, org_id):
    return db.scalar(select(Subscription).where(Subscription.organization_id == org_id))


def quota_period(subscription, at=None):
    at = at or now()
    anchor = subscription.quota_anchor
    count = max(0, (at.year - anchor.year) * 12 + at.month - anchor.month)
    start = add_months(anchor, count, anchor.day)
    if start > at:
        count = max(0, count - 1)
        start = add_months(anchor, count, anchor.day)
    end = add_months(anchor, count + 1, anchor.day)
    return start, end


def require_access(db, org_id):
    sub = subscription_for(db, org_id)
    if not sub or not (sub.status == "development" or (sub.status == "active" and sub.ends_at and sub.ends_at > now())):
        raise HTTPException(402, "Для нового действия требуется активная подписка")
    return sub


def consume(db, org_id, resource, amount=1, reserve=False):
    if amount < 0 or resource not in QUOTAS:
        raise ValueError("Invalid usage resource")
    lock_organization(db, org_id)
    sub = require_access(db, org_id)
    start, _ = quota_period(sub)
    usage = db.scalar(select(Usage).where(Usage.organization_id == org_id, Usage.period_start == start.date(), Usage.resource == resource))
    if not usage:
        usage = Usage(organization_id=org_id, period_start=start.date(), resource=resource, used=0, reserved=0)
        db.add(usage)
        db.flush()
    if usage.used + usage.reserved + amount > QUOTAS[resource]:
        raise HTTPException(429, f"Исчерпан лимит ресурса: {resource}")
    if reserve:
        usage.reserved += amount
    else:
        usage.used += amount
    return usage


def snapshot(outreach):
    fields = ["recipient", "subject", "body", "attachment_ids", "item_ids", "requirements_version", "version", "purpose", "basis", "counterparty_id", "kind", "channel"]
    payload = {key: getattr(outreach, key) for key in fields}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def full_cost(quote):
    if quote.delivery_included is True:
        return quote.amount
    if quote.delivery_included is False and quote.delivery_cost is not None:
        return quote.amount + quote.delivery_cost
    return None


def quote_suitable(quote, campaign):
    return bool(quote.current and quote.kind == "received" and quote.assessment == "matches" and quote.requirements_version == campaign.requirements_version and campaign.requirements_approved and (not quote.valid_until or quote.valid_until >= date.today()) and full_cost(quote) is not None and quote.tax and quote.terms and quote.delivery_days is not None)


def task_counts(db, campaign):
    links = db.scalars(select(CampaignCounterparty).where(CampaignCounterparty.campaign_id == campaign.id)).all()
    sent = db.scalars(select(Outreach).where(Outreach.campaign_id == campaign.id, Outreach.kind == "initial", Outreach.accepted_at.is_not(None))).all()
    quotes = db.scalars(select(Quote).where(Quote.campaign_id == campaign.id, Quote.current.is_(True))).all()
    received = {db.get(Counterparty, q.counterparty_id).name.casefold() for q in quotes if q.kind == "received"}
    suitable = {db.get(Counterparty, q.counterparty_id).name.casefold() for q in quotes if quote_suitable(q, campaign)}
    if campaign.direction == "purchase":
        from .ai_service import latest_analysis, context_for
        analysis = latest_analysis(db, campaign)
        try:
            current = analysis and analysis.result.get("input_fingerprint") == context_for(db, campaign)[2]
        except HTTPException:
            current = False
        if current:
            for offer in analysis.result.get("offers", []):
                if offer["role"] == "offer" and offer["supplier"]["value"]:
                    received.add(offer["supplier"]["value"].casefold())
                    if offer["eligible"]:
                        suitable.add(offer["supplier"]["value"].casefold())
    return {
        "found": len(links), "sent": len({(o.counterparty_id, o.channel) for o in sent}),
        "received": len(received) if campaign.direction == "purchase" else sum(x.positive_response for x in links),
        "suitable": len(suitable) if campaign.direction == "purchase" else 0,
        "deals": sum(x.deal_confirmed for x in links),
    }


def task_data(db, campaign):
    user = db.get(User, campaign.owner_id)
    return {"id": campaign.id, "code": campaign.code, "direction": campaign.direction, "title": campaign.title, "quantity": campaign.quantity, "region": campaign.region, "budget": str(campaign.budget) if campaign.budget is not None else None, "deadline": campaign.deadline, "owner_id": campaign.owner_id, "owner_name": user.name if user else "", "status": campaign.status, "requirements": campaign.requirements, "requirements_version": campaign.requirements_version, "requirements_approved": campaign.requirements_approved, "selected_quote_id": campaign.selected_quote_id, "created_at": campaign.created_at, **task_counts(db, campaign)}


def columns(obj, exclude=()):
    result = {}
    for column in obj.__table__.columns:
        if column.name in exclude:
            continue
        value = getattr(obj, column.name)
        result[column.name] = str(value) if isinstance(value, Decimal) else value
    return result

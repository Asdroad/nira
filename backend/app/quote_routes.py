from datetime import date
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Quote, Counterparty, Document, RequestItem, CampaignCounterparty
from .schemas import QuoteInput, SelectionInput
from .security import Actor, get_actor, tenant_object, campaign_object
from .services import require_open_task, audit, columns, full_cost, quote_suitable, lock_organization
from .task_routes import validate_items
from .file_routes import visible_document

router = APIRouter(prefix="/api/v1/tasks", tags=["Коммерческие предложения"])


def quote_data(db, quote, task):
    company = db.get(Counterparty, quote.counterparty_id)
    cost = full_cost(quote)
    return {**columns(quote), "company": company.name, "full_cost": str(cost) if cost is not None else None, "suitable": quote_suitable(quote, task), "selected": task.selected_quote_id == quote.id, "stale": quote.requirements_version != task.requirements_version, "expired": bool(quote.valid_until and quote.valid_until < date.today())}


@router.get("/{task_id}/quotes")
def list_quotes(task_id: str, sort: str = "price", history: bool = False, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    query = select(Quote).where(Quote.campaign_id == task.id)
    if not history:
        query = query.where(Quote.current.is_(True))
    results = [quote_data(db, q, task) for q in db.scalars(query).all()]
    if sort not in {"price", "days"}:
        raise HTTPException(422, "Неизвестный критерий сравнения")
    results.sort(key=lambda q: (not q["suitable"], (q["delivery_days"] if q["delivery_days"] is not None else Decimal("Infinity")) if sort == "days" else (Decimal(q["full_cost"]) if q["full_cost"] is not None else Decimal("Infinity"))))
    return {"items": results, "requirements_version": task.requirements_version, "recommendation_id": next((q["id"] for q in results if q["suitable"]), None)}


@router.post("/{task_id}/quotes", status_code=201)
def create_quote(task_id: str, data: QuoteInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    company = tenant_object(db, Counterparty, data.counterparty_id, actor)
    if data.requirements_version != task.requirements_version:
        raise HTTPException(409, "Предложение относится к другой версии требований")
    if data.kind == "received" and task.direction != "purchase" or data.kind == "sent" and task.direction != "sales":
        raise HTTPException(422, "Направление КП не соответствует задаче")
    validate_items(db, task, data.matching_items)
    if data.document_id:
        document = visible_document(db, data.document_id, actor)
        if document.campaign_id and document.campaign_id != task.id:
            raise HTTPException(422, "Документ относится к другой задаче")
    if not db.scalar(select(CampaignCounterparty.id).where(CampaignCounterparty.campaign_id == task.id, CampaignCounterparty.counterparty_id == company.id)):
        raise HTTPException(422, "Сначала добавьте контрагента к задаче")
    current = db.scalar(select(Quote).where(Quote.campaign_id == task.id, Quote.counterparty_id == company.id, Quote.kind == data.kind, Quote.current.is_(True)))
    if current and data.previous_id != current.id or not current and data.previous_id:
        raise HTTPException(409, "Укажите ID текущей версии КП для обновления")
    values = data.model_dump(exclude={"expected_version"})
    quote = Quote(organization_id=actor.organization.id, campaign_id=task.id, created_by=actor.user.id, version=current.version + 1 if current else 1, **values)
    if data.delivery_included is True and data.delivery_cost not in {None, 0}:
        raise HTTPException(422, "Для включённой доставки отдельная стоимость не добавляется")
    if data.assessment == "matches":
        items = db.scalars(select(RequestItem).where(RequestItem.campaign_id == task.id)).all()
        required = {c["name"] for item in items for c in item.characteristics if c.get("required", True)}
        confirmed = {e.requirement for e in data.evidence if e.confirmed}
        if not task.requirements_approved or not data.evidence or not all(e.confirmed for e in data.evidence) or not required.issubset(confirmed):
            raise HTTPException(422, "Для соответствия нужны подтверждённые источники по обязательным условиям")
        if full_cost(quote) is None or not data.tax or not data.terms or data.delivery_days is None:
            raise HTTPException(422, "Для соответствия уточните полную стоимость, налоги, оплату и срок")
    if current:
        current.current = False
        if task.selected_quote_id == current.id:
            task.selected_quote_id = None
    db.add(quote); db.flush()
    if task.direction == "purchase":
        task.status = "comparison"
    audit(db, actor, "quote.created", quote, "Сохранено КП от «" + company.name + "»", task.id)
    return quote_data(db, quote, task)


@router.post("/{task_id}/quotes/{quote_id}/select")
def select_quote(task_id: str, quote_id: str, data: SelectionInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    quote = tenant_object(db, Quote, quote_id, actor)
    if quote.campaign_id != task.id:
        raise HTTPException(404, "КП задачи не найдено")
    if not quote_suitable(quote, task):
        raise HTTPException(409, "Выбрать можно действующее КП, соответствующее текущим требованиям")
    task.selected_quote_id, task.status = quote.id, "selected"
    audit(db, actor, "quote.selected", quote, "Сотрудник выбрал предложение «" + db.get(Counterparty, quote.counterparty_id).name + "»", task.id, {"comment": data.comment, "deal_confirmed": False})
    return {"selected_quote_id": quote.id, "order_placed": False}

import csv
import io
from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .db import get_db
from .config import get_settings
from .models import Organization, Counterparty, CatalogItem, Message, Job, AuditEvent, Document, Outreach, Approval, Usage, Membership, Campaign
from .schemas import OrganizationUpdate, CounterpartyInput, CatalogInput, PeriodInput, MessageInput
from .security import Actor, get_actor, tenant_object, campaign_object, visible_campaigns
from .services import audit, columns, QUOTAS, quota_period, subscription_for, task_data, TERMINAL_TASKS
from .file_routes import list_documents, visible_document
from .auth_routes import team
from .outreach_routes import list_approvals
from .quote_routes import list_quotes

router = APIRouter(prefix="/api/v1", tags=["Рабочее пространство"])


@router.get("/organization")
def organization(actor: Actor = Depends(get_actor)):
    return columns(actor.organization)


@router.put("/organization")
def update_organization(data: OrganizationUpdate, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.owner()
    for key, value in data.model_dump(exclude={"auto_search"}).items():
        setattr(actor.organization, key, value)
    actor.organization.settings = {**actor.organization.settings, "auto_search": data.auto_search, "require_approval": True}
    audit(db, actor, "organization.updated", actor.organization, "Обновлён профиль организации")
    return columns(actor.organization)


@router.get("/counterparties")
def counterparties(q: str = "", actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.require("view_counterparties")
    query = select(Counterparty).where(Counterparty.organization_id == actor.organization.id)
    if q:
        query = query.where(Counterparty.name.ilike("%" + q[:200] + "%"))
    return {"items": [columns(x) for x in db.scalars(query.order_by(Counterparty.name)).all()]}


@router.post("/counterparties", status_code=201)
def create_counterparty(data: CounterpartyInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write(); actor.require("view_counterparties")
    if data.inn and db.scalar(select(Counterparty.id).where(Counterparty.organization_id == actor.organization.id, Counterparty.inn == data.inn)):
        raise HTTPException(409, "Контрагент с таким ИНН уже есть")
    obj = Counterparty(organization_id=actor.organization.id, **data.model_dump())
    db.add(obj); db.flush()
    audit(db, actor, "counterparty.created", obj, "Добавлен контрагент «" + obj.name + "»")
    return columns(obj)


class StopInput(BaseModel):
    blocked: bool


@router.put("/counterparties/{counterparty_id}/stop")
def stop_counterparty(counterparty_id: str, data: StopInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write(); actor.require("view_counterparties")
    company = tenant_object(db, Counterparty, counterparty_id, actor)
    if not data.blocked:
        actor.owner()
    company.blocked = data.blocked
    audit(db, actor, "counterparty.stop", company, "Контрагент внесён в стоп-лист" if data.blocked else "Контрагент исключён из стоп-листа")
    return columns(company)


@router.get("/catalog")
def catalog(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    return {"items": [columns(x) for x in db.scalars(select(CatalogItem).where(CatalogItem.organization_id == actor.organization.id).order_by(CatalogItem.name)).all()]}


@router.post("/catalog", status_code=201)
def create_catalog(data: CatalogInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    obj = CatalogItem(organization_id=actor.organization.id, **data.model_dump())
    db.add(obj); db.flush()
    audit(db, actor, "catalog.created", obj, "Добавлена позиция каталога")
    return columns(obj)


@router.put("/catalog/{item_id}")
def edit_catalog(item_id: str, data: CatalogInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    obj = tenant_object(db, CatalogItem, item_id, actor)
    for key, value in data.model_dump().items():
        setattr(obj, key, value)
    audit(db, actor, "catalog.updated", obj, "Обновлены условия позиции каталога")
    return columns(obj)


@router.get("/messages")
def messages(campaign_id: str | None = None, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    query = select(Message).where(Message.organization_id == actor.organization.id)
    if campaign_id:
        campaign_object(db, campaign_id, actor)
        query = query.where(Message.campaign_id == campaign_id)
    elif not actor.can("view_team"):
        ids = [t.id for t in db.scalars(visible_campaigns(actor)).all()]
        query = query.where(Message.campaign_id.in_(ids))
    return {"items": [columns(x) for x in db.scalars(query.order_by(Message.created_at.desc())).all()]}


@router.post("/messages/import", status_code=201)
def import_message(data: MessageInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    if data.campaign_id:
        campaign_object(db, data.campaign_id, actor)
    elif not actor.can("view_team"):
        raise HTTPException(403, "Непривязанное письмо доступно руководителю")
    if data.counterparty_id:
        tenant_object(db, Counterparty, data.counterparty_id, actor)
    for doc_id in data.attachment_ids:
        visible_document(db, doc_id, actor)
    previous = db.scalar(select(Message).where(Message.organization_id == actor.organization.id, Message.external_id == data.external_id))
    if previous:
        if previous.campaign_id:
            campaign_object(db, previous.campaign_id, actor)
        elif not actor.can("view_team"):
            raise HTTPException(404, "Письмо не найдено")
        return {**columns(previous), "duplicate": True}
    message = Message(organization_id=actor.organization.id, **data.model_dump())
    db.add(message); db.flush()
    audit(db, actor, "message.imported", message, "Письмо добавлено вручную", data.campaign_id)
    return columns(message)


@router.get("/subscription")
def subscription(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    sub = subscription_for(db, actor.organization.id)
    start, end = quota_period(sub)
    rows = db.scalars(select(Usage).where(Usage.organization_id == actor.organization.id, Usage.period_start == start.date())).all()
    usage = {x.resource: x for x in rows}
    return {**columns(sub), "quota_start": start, "quota_end": end, "resources": [{"resource": resource, "limit": limit, "used": usage[resource].used if resource in usage else 0, "reserved": usage[resource].reserved if resource in usage else 0} for resource, limit in QUOTAS.items()], "price": None, "currency": "RUB", "payment_connected": False, "billing_periods": ["month", "quarter", "year"]}


@router.put("/subscription/period")
def subscription_period(data: PeriodInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.owner()
    sub = subscription_for(db, actor.organization.id)
    sub.period = data.period
    audit(db, actor, "subscription.preference", sub, "Выбран период следующей оплаты")
    return {"period": sub.period, "payment_created": False, "access_extended": False}


@router.post("/subscription/checkout")
def checkout(actor: Actor = Depends(get_actor)):
    actor.owner()
    raise HTTPException(503, "Цена и платёжный сервис пока не настроены. Оплата не создана")


@router.get("/integrations")
def integrations(actor: Actor = Depends(get_actor)):
    from .local_ocr import ocr_status
    from .search_adapters import SOURCES
    ocr = ocr_status()
    from .ai_provider import ai_status
    ai = ai_status()
    return {"items": [
        {"id": "documents", "name": "Документы", "status": "local", "capabilities": ["PDF с текстом", "DOCX", "XLSX", "CSV"] + (["OCR русских и английских сканов"] if ocr["available"] else []), "unavailable": ([] if ai["available"] else ["ИИ-анализ"]) + ([] if ocr["available"] else ["OCR"]), "message": ocr["message"]},
        {"id": "mail", "name": "Корпоративная почта", "status": "not_configured", "capabilities": [], "planned": ["Чтение", "Черновики", "Отправка после согласования"]},
        {"id": "web", "name": "Поиск в интернете", "status": "local", "capabilities": ["DuckDuckGo без ключа", "Ссылки и фрагменты выдачи"], "message": "Пилотный поиск. Поисковик может временно ограничивать запросы. Запускается из карточки задачи."},
        {"id": "ai", "name": "Основной ИИ · OpenRouter", "status": "local" if ai["available"] else "not_configured", "capabilities": ["Разбор КП и прайсов", "Проверка требований", "Сравнение с источниками", "Объявления и письма", "Выгрузка анализа"], "message": ai["message"]},
        {"id": "onlinecontract", "name": "ONLINECONTRACT", "status": "local", "capabilities": ["Поиск закупок без регистрации", "Действующие и архивные процедуры"], "message": "Публичный JSON-интерфейс сайта; стабильность внешнего API не гарантирована. Участие и часть данных требуют аккаунта."},
        *[{"id": identifier, "name": SOURCES[identifier]["name"], "status": "unavailable", "capabilities": [], "planned": [], "message": SOURCES[identifier]["message"]} for identifier in ("online_tender", "tender_online")],
        {"id": "payments", "name": "Оплата подписки", "status": "not_configured", "capabilities": [], "planned": ["Месяц", "Три месяца", "Год"]},
    ]}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    job = tenant_object(db, Job, job_id, actor)
    if job.campaign_id:
        campaign_object(db, job.campaign_id, actor)
    elif not actor.can("view_team") and job.created_by != actor.user.id:
        raise HTTPException(404, "Задание не найдено")
    return columns(job)


@router.get("/reports")
def reports(direction: str | None = None, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.require("export")
    query = visible_campaigns(actor)
    if direction:
        query = query.where(Campaign.direction == direction)
    return {"items": [task_data(db, x) for x in db.scalars(query.order_by(Campaign.code)).all()], "counting": {"sent": "Unique accepted initial outreach per company and channel", "suitable": "Unique companies with current matching unexpired quotes"}}


def safe_csv(value):
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


@router.get("/reports/export.csv")
def export(direction: str | None = None, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    data = reports(direction, actor, db)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Задача", "Направление", "Количество", "Регион", "Бюджет, ₽", "Найдено компаний", "Направлено обращений", "Получено ответов", "Подходящих КП", "Подтверждено сделок", "Статус", "Версия требований"])
    for task in data["items"]:
        writer.writerow([safe_csv(task[key]) for key in ["title", "direction", "quantity", "region", "budget", "found", "sent", "received", "suitable", "deals", "status", "requirements_version"]])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="NIRA_report.csv"', "Cache-Control": "no-store"})


@router.get("/bootstrap")
def bootstrap(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    tasks = db.scalars(visible_campaigns(actor).order_by(Campaign.created_at.desc())).all()
    ids = [t.id for t in tasks]
    events = select(AuditEvent).where(AuditEvent.organization_id == actor.organization.id)
    if not actor.can("view_team"):
        events = events.where(AuditEvent.campaign_id.in_(ids) | (AuditEvent.user_id == actor.user.id))
    active = [t for t in tasks if t.status not in TERMINAL_TASKS]
    task_list = [task_data(db, t) for t in tasks]
    return {
        "mode": "local" if get_settings().env != "production" else "production",
        "user": columns(actor.user, {"password_hash"}), "membership": columns(actor.membership), "organization": columns(actor.organization), "csrf_token": actor.csrf,
        "tasks": task_list, "team": team(actor, db),
        "counterparties": counterparties("", actor, db)["items"] if actor.can("view_counterparties") else [],
        "catalog": catalog(actor, db)["items"], "documents": list_documents(None, actor, db)["items"],
        "approvals": list_approvals("pending", actor, db)["items"], "messages": messages(None, actor, db)["items"],
        "activity": [columns(e) for e in db.scalars(events.order_by(AuditEvent.created_at.desc()).limit(40)).all()],
        "subscription": subscription(actor, db), "integrations": integrations(actor)["items"],
        "quotes": [quote for t in tasks for quote in list_quotes(t.id, "price", False, actor, db)["items"]],
        "overview": {"active": len(active), "suitable": sum(t["suitable"] for t in task_list), "budget": str(sum(t.budget or 0 for t in active if t.direction == "purchase"))},
    }

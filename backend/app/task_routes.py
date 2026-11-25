from datetime import date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .db import get_db
from .models import Campaign, Membership, RequestItem, RequirementsVersion, Counterparty, CampaignCounterparty, Quote, Approval, Outreach, Job, AuditEvent
from .schemas import TaskCreate, RequirementsInput, TaskStateInput, ItemInput, CounterpartyLink, SalesOutcome, JobInput
from .security import Actor, get_actor, tenant_object, campaign_object, visible_campaigns
from .services import require_open_task, audit, consume, lock_organization, task_data, columns, TERMINAL_TASKS, require_access

router = APIRouter(prefix="/api/v1/tasks", tags=["Закупки и продажи"])


@router.get("")
def list_tasks(direction: str | None = None, status: str | None = None, q: str = "", actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    query = visible_campaigns(actor)
    if direction:
        query = query.where(Campaign.direction == direction)
    if status:
        query = query.where(Campaign.status == status)
    if q:
        query = query.where(Campaign.title.ilike("%" + q[:200] + "%"))
    return {"items": [task_data(db, c) for c in db.scalars(query.order_by(Campaign.created_at.desc())).all()]}


@router.post("", status_code=201)
def create_task(data: TaskCreate, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    if actor.organization.type == "buyer" and data.direction == "sales" or actor.organization.type == "supplier" and data.direction == "purchase":
        raise HTTPException(409, "Это направление не включено в профиле организации")
    owner_id = data.owner_id or actor.user.id
    if owner_id != actor.user.id:
        actor.require("view_team")
    if not db.scalar(select(Membership.id).where(Membership.organization_id == actor.organization.id, Membership.user_id == owner_id, Membership.status == "active", Membership.role != "viewer")):
        raise HTTPException(422, "Ответственный должен иметь активный доступ к организации")
    if data.deadline and data.deadline < date.today():
        raise HTTPException(422, "Срок задачи не может быть в прошлом")
    lock_organization(db, actor.organization.id)
    active = db.scalar(select(func.count()).select_from(Campaign).where(Campaign.organization_id == actor.organization.id, Campaign.status.not_in(TERMINAL_TASKS)))
    if active >= 10:
        raise HTTPException(409, "Лимит — десять активных задач")
    consume(db, actor.organization.id, "tasks")
    code = (db.scalar(select(func.max(Campaign.code)).where(Campaign.organization_id == actor.organization.id)) or 0) + 1
    obj = Campaign(organization_id=actor.organization.id, code=code, created_by=actor.user.id, owner_id=owner_id, **data.model_dump(exclude={"owner_id"}))
    db.add(obj); db.flush()
    db.add(RequirementsVersion(organization_id=actor.organization.id, campaign_id=obj.id, version=1, text=data.requirements))
    db.add(RequestItem(organization_id=actor.organization.id, campaign_id=obj.id, name=data.title))
    audit(db, actor, "task.created", obj, "Создана задача «" + obj.title + "»", obj.id)
    return task_data(db, obj)


@router.get("/{task_id}")
def get_task(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    return {**task_data(db, task), "items": [columns(i) for i in db.scalars(select(RequestItem).where(RequestItem.campaign_id == task.id)).all()]}


@router.put("/{task_id}/requirements")
def requirements(task_id: str, data: RequirementsInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    if task.status in TERMINAL_TASKS:
        raise HTTPException(409, "Задача завершена")
    if data.expected_version != task.requirements_version:
        raise HTTPException(409, "Требования уже изменились. Обновите карточку")
    current = db.scalar(select(RequirementsVersion).where(RequirementsVersion.campaign_id == task.id, RequirementsVersion.version == task.requirements_version))
    if task.requirements_approved or data.text != task.requirements:
        task.requirements_version += 1
        current = RequirementsVersion(organization_id=actor.organization.id, campaign_id=task.id, version=task.requirements_version, text=data.text)
        db.add(current)
    task.requirements, task.requirements_approved = data.text, data.approve
    current.text, current.approved_by = data.text, actor.user.id if data.approve else None
    task.status = "planned" if data.approve else "requirements"
    task.selected_quote_id = None
    if data.approve:
        from .ai_service import queue_analysis
        from .config import get_settings
        if get_settings().ai_auto_run:
            queue_analysis(db, task, actor.user.id, automatic=True)
    for outreach in db.scalars(select(Outreach).where(Outreach.campaign_id == task.id, Outreach.accepted_at.is_(None))).all():
        outreach.status = "revision"
        for approval in db.scalars(select(Approval).where(Approval.outreach_id == outreach.id, Approval.status.in_(["pending", "approved"]))).all():
            approval.status = "stale"
    audit(db, actor, "requirements.updated", task, "Сохранена версия требований " + str(task.requirements_version), task.id)
    return task_data(db, task)


@router.get("/{task_id}/requirements/history")
def requirements_history(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    return {"items": [columns(x) for x in db.scalars(select(RequirementsVersion).where(RequirementsVersion.campaign_id == task.id).order_by(RequirementsVersion.version)).all()]}


@router.patch("/{task_id}/state")
def task_state(task_id: str, data: TaskStateInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    task.status = data.status
    outreach_ids = select(Outreach.id).where(Outreach.campaign_id == task.id)
    for approval in db.scalars(select(Approval).where(Approval.outreach_id.in_(outreach_ids), Approval.status.in_(["pending", "approved"]))).all():
        approval.status = "stale"
    audit(db, actor, "task.state", task, "Задача завершена" if data.status == "closed" else "Задача отменена", task.id)
    return task_data(db, task)


@router.post("/{task_id}/items", status_code=201)
def add_item(task_id: str, data: ItemInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    if task.requirements_approved:
        raise HTTPException(409, "Сначала сохраните новую неутверждённую версию требований")
    if db.scalar(select(func.count()).select_from(RequestItem).where(RequestItem.campaign_id == task.id)) >= 50:
        raise HTTPException(409, "Не более 50 позиций в задаче")
    item = RequestItem(organization_id=actor.organization.id, campaign_id=task.id, **data.model_dump())
    db.add(item); db.flush()
    audit(db, actor, "item.created", item, "Добавлена позиция", task.id)
    return columns(item)


def validate_items(db, task, ids):
    valid = set(db.scalars(select(RequestItem.id).where(RequestItem.campaign_id == task.id)).all())
    if not set(ids).issubset(valid):
        raise HTTPException(422, "Позиция не принадлежит задаче")


@router.post("/{task_id}/counterparties")
def link_counterparty(task_id: str, data: CounterpartyLink, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write(); actor.require("view_counterparties")
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    company = tenant_object(db, Counterparty, data.counterparty_id, actor)
    validate_items(db, task, data.item_ids)
    existing = db.scalar(select(CampaignCounterparty).where(CampaignCounterparty.campaign_id == task.id, CampaignCounterparty.counterparty_id == company.id))
    if not existing:
        existing = CampaignCounterparty(organization_id=actor.organization.id, campaign_id=task.id, counterparty_id=company.id, item_ids=data.item_ids)
        db.add(existing); db.flush()
        audit(db, actor, "counterparty.linked", existing, "Добавлен контрагент «" + company.name + "»", task.id)
    return columns(existing)


@router.get("/{task_id}/counterparties")
def task_counterparties(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    links = db.scalars(select(CampaignCounterparty).where(CampaignCounterparty.campaign_id == task.id)).all()
    return {"items": [{**columns(db.get(Counterparty, x.counterparty_id)), "link": columns(x)} for x in links]}


@router.put("/{task_id}/counterparties/{counterparty_id}/outcome")
def sales_outcome(task_id: str, counterparty_id: str, data: SalesOutcome, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    if task.direction != "sales":
        raise HTTPException(409, "Результат продажи доступен для задачи продаж")
    link = db.scalar(select(CampaignCounterparty).where(CampaignCounterparty.campaign_id == task.id, CampaignCounterparty.counterparty_id == counterparty_id))
    if not link:
        raise HTTPException(404, "Контрагент задачи не найден")
    link.positive_response, link.deal_confirmed, link.deal_amount = data.positive_response, data.deal_confirmed, data.deal_amount
    audit(db, actor, "sales.outcome", link, "Сотрудник зафиксировал результат продажи", task.id, {"basis": data.basis})
    return columns(link)


@router.get("/{task_id}/history")
def task_history(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    return {"items": [columns(x) for x in db.scalars(select(AuditEvent).where(AuditEvent.campaign_id == task.id).order_by(AuditEvent.created_at.desc())).all()]}


@router.post("/{task_id}/jobs", status_code=201)
def create_job(task_id: str, data: JobInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    require_access(db, actor.organization.id)
    if not task.requirements_approved:
        raise HTTPException(409, "Утвердите требования перед запуском")
    if data.kind in {"analyze", "draft"}:
        from .ai_service import queue_analysis
        return columns(queue_analysis(db, task, actor.user.id))
    payload = {"requirements_version": task.requirements_version}
    if data.kind == "search":
        query = (data.query or task.title).strip()
        if len(query) < 2:
            raise HTTPException(422, "Введите минимум два символа для поиска")
        if db.scalar(select(Job.id).where(Job.campaign_id == task.id, Job.kind == "search", Job.status.in_(["queued", "running"]))):
            raise HTTPException(409, "Поиск по этой задаче уже выполняется")
        sources = list(dict.fromkeys(data.sources))
        requests = sum(s in {"web", "onlinecontract"} for s in sources)
        if not requests:
            raise HTTPException(422, "Выберите интернет или ONLINECONTRACT: у остальных сайтов нет открытого поиска")
        usage = consume(db, actor.organization.id, "search_queries", requests, reserve=True)
        from .ai_provider import ai_status
        contact_budget = 10 if "web" in sources and ai_status()["available"] else 0
        pages = consume(db, actor.organization.id, "web_pages", requests + contact_budget, reserve=True)
        payload.update(query=query, sources=sources, active_only=data.active_only, limit=data.limit,
                       usage_id=usage.id, pages_usage_id=pages.id, reserved_requests=requests,
                       reserved_pages=requests + contact_budget, contact_budget=contact_budget)
    job = Job(organization_id=actor.organization.id, campaign_id=task.id, created_by=actor.user.id, kind=data.kind,
              status="queued", payload=payload)
    db.add(job); db.flush()
    audit(db, actor, "search.queued", job, "Поиск добавлен в очередь локального исполнителя", task.id)
    return columns(job)


@router.get("/{task_id}/jobs")
def task_jobs(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    return {"items": [columns(job) for job in db.scalars(select(Job).where(Job.campaign_id == task.id).order_by(Job.created_at.desc()).limit(20))]}

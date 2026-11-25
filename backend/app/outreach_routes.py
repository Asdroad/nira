from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Outreach, Approval, Counterparty, Document, CampaignCounterparty, now
from .schemas import OutreachInput, ApprovalDecision
from .security import Actor, get_actor, tenant_object, campaign_object, visible_campaigns
from .services import require_open_task, audit, columns, snapshot, require_access, lock_organization
from .task_routes import validate_items
from .file_routes import visible_document

router = APIRouter(prefix="/api/v1", tags=["Обращения и согласования"])


def validate_outreach(db, task, data, actor):
    company = tenant_object(db, Counterparty, data.counterparty_id, actor)
    if company.blocked:
        raise HTTPException(409, "Контрагент запретил обращения")
    if not task.requirements_approved:
        raise HTTPException(409, "Сначала утвердите требования")
    if not db.scalar(select(CampaignCounterparty.id).where(CampaignCounterparty.campaign_id == task.id, CampaignCounterparty.counterparty_id == company.id)):
        raise HTTPException(422, "Контрагент не добавлен к задаче")
    validate_items(db, task, data.item_ids)
    for identifier in data.attachment_ids:
        document = visible_document(db, identifier, actor)
        if document.campaign_id and document.campaign_id != task.id:
            raise HTTPException(422, "Вложение относится к другой задаче")
    if "\r" in data.subject or "\n" in data.subject:
        raise HTTPException(422, "Тема письма не должна содержать переносы строки")


@router.post("/tasks/{task_id}/outreach", status_code=201)
def draft(task_id: str, data: OutreachInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    validate_outreach(db, task, data, actor)
    obj = Outreach(organization_id=actor.organization.id, campaign_id=task.id, created_by=actor.user.id, requirements_version=task.requirements_version, **data.model_dump(exclude={"expected_version"}))
    db.add(obj); db.flush()
    audit(db, actor, "outreach.drafted", obj, "Подготовлен черновик обращения", task.id)
    return columns(obj)


@router.get("/tasks/{task_id}/outreach")
def list_outreach(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    return {"items": [columns(x) for x in db.scalars(select(Outreach).where(Outreach.campaign_id == task.id)).all()]}


@router.put("/outreach/{outreach_id}")
def edit(outreach_id: str, data: OutreachInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    obj = tenant_object(db, Outreach, outreach_id, actor)
    task = campaign_object(db, obj.campaign_id, actor)
    require_open_task(task)
    if obj.accepted_at:
        raise HTTPException(409, "Отправленное обращение не редактируется")
    if data.expected_version != obj.version:
        raise HTTPException(409, "Черновик уже изменился. Обновите данные")
    validate_outreach(db, task, data, actor)
    for key, value in data.model_dump(exclude={"expected_version"}).items():
        setattr(obj, key, value)
    obj.version += 1
    obj.requirements_version, obj.status = task.requirements_version, "draft"
    for approval in db.scalars(select(Approval).where(Approval.outreach_id == obj.id, Approval.status.in_(["pending", "approved"]))).all():
        approval.status = "stale"
    audit(db, actor, "outreach.edited", obj, "Обращение изменено; прежнее согласование отменено", task.id)
    return columns(obj)


@router.post("/outreach/{outreach_id}/request-approval", status_code=201)
def request_approval(outreach_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    obj = tenant_object(db, Outreach, outreach_id, actor)
    task = campaign_object(db, obj.campaign_id, actor)
    require_open_task(task)
    if obj.accepted_at or obj.requirements_version != task.requirements_version or not task.requirements_approved:
        raise HTTPException(409, "Обращение уже отправлено или требования изменились")
    existing = db.scalar(select(Approval).where(Approval.outreach_id == obj.id, Approval.status == "pending"))
    if existing:
        return columns(existing)
    approval = Approval(organization_id=actor.organization.id, outreach_id=obj.id, snapshot_hash=snapshot(obj))
    db.add(approval); db.flush()
    obj.status, task.status = "approval", "approval"
    audit(db, actor, "approval.requested", approval, "Обращение передано на согласование", task.id)
    return columns(approval)


@router.get("/approvals")
def list_approvals(status: str = "pending", actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task_ids = [t.id for t in db.scalars(visible_campaigns(actor)).all()]
    query = select(Approval, Outreach).join(Outreach, Approval.outreach_id == Outreach.id).where(Approval.organization_id == actor.organization.id, Approval.status == status, Outreach.campaign_id.in_(task_ids))
    return {"items": [{**columns(a), "outreach": columns(o)} for a, o in db.execute(query).all()]}


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: str, data: ApprovalDecision, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write(); actor.require("approve")
    lock_organization(db, actor.organization.id)
    approval = tenant_object(db, Approval, approval_id, actor)
    obj = tenant_object(db, Outreach, approval.outreach_id, actor)
    task = campaign_object(db, obj.campaign_id, actor)
    require_open_task(task)
    if approval.status != "pending":
        raise HTTPException(409, "Согласование уже рассмотрено или устарело")
    if snapshot(obj) != approval.snapshot_hash or obj.requirements_version != task.requirements_version:
        raise HTTPException(409, "Содержимое обращения изменилось")
    approval.status, approval.decided_by, approval.decided_at, approval.comment = data.decision, actor.user.id, now(), data.comment
    obj.status = "ready" if data.decision == "approved" else "revision"
    task.status = "ready" if data.decision == "approved" else "revision"
    audit(db, actor, "approval." + data.decision, approval, "Черновик согласован" if data.decision == "approved" else "Черновик возвращён на доработку", task.id)
    return {**columns(approval), "email_sent": False}


@router.post("/outreach/{outreach_id}/send")
def send(outreach_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write(); actor.require("send")
    lock_organization(db, actor.organization.id)
    obj = tenant_object(db, Outreach, outreach_id, actor)
    task = campaign_object(db, obj.campaign_id, actor)
    require_open_task(task)
    require_access(db, actor.organization.id)
    company = tenant_object(db, Counterparty, obj.counterparty_id, actor)
    if company.blocked:
        raise HTTPException(409, "Контрагент запретил обращения")
    approval = db.scalar(select(Approval).where(Approval.outreach_id == obj.id, Approval.status == "approved", Approval.snapshot_hash == snapshot(obj)))
    if not approval or obj.requirements_version != task.requirements_version or not task.requirements_approved:
        raise HTTPException(409, "Нет действующего согласования этого обращения")
    if obj.accepted_at:
        return {"status": obj.status, "external_id": obj.external_id}
    raise HTTPException(503, "Почта не подключена. Черновик сохранён; письмо не отправлено")

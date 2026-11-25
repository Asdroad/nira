from datetime import timedelta
from sqlalchemy import select, update
from fastapi import HTTPException
from .models import Job, Campaign, Membership, User, Organization, Usage, AuditEvent, now
from .security import Actor, campaign_object
from .services import lock_organization, require_access, require_open_task
from .search_adapters import run_search


def validate_job(db, job, check_version=True):
    user = db.get(User, job.created_by)
    membership = db.scalar(select(Membership).where(Membership.organization_id == job.organization_id,
        Membership.user_id == job.created_by, Membership.status == "active"))
    if not user or not user.active or not membership or membership.role == "viewer":
        raise HTTPException(403, "Доступ автора задания отозван")
    actor = Actor(user, membership, db.get(Organization, job.organization_id), None, "")
    task = campaign_object(db, job.campaign_id, actor)
    require_open_task(task)
    require_access(db, job.organization_id)
    if not task.requirements_approved or (check_version and task.requirements_version != job.payload["requirements_version"]):
        raise HTTPException(409, "Требования изменились; запустите обработку заново")
    return actor


def settle(db, job, successful, contact_requests=0):
    for key in ("usage_id", "pages_usage_id"):
        reserved = job.payload.get("reserved_pages", job.payload.get("reserved_requests", 0)) if key == "pages_usage_id" else job.payload.get("reserved_requests", 0)
        usage = db.get(Usage, job.payload.get(key))
        if usage and usage.organization_id == job.organization_id:
            usage.reserved = max(0, usage.reserved - reserved)
            usage.used += min(successful + (contact_requests if key == "pages_usage_id" else 0), reserved)


def process_search_one(factory):
    with factory() as db:
        db.execute(update(Job).where(Job.kind == "search", Job.status == "running", Job.started_at < now() - timedelta(minutes=5)).values(status="queued"))
        job = db.scalar(select(Job).where(Job.kind == "search", Job.status == "queued").order_by(Job.created_at).limit(1))
        if not job:
            db.commit()
            return False
        claimed = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(status="running", started_at=now(), attempts=Job.attempts + 1))
        if claimed.rowcount != 1:
            db.rollback()
            return True
        db.refresh(job)
        identifier, attempt, payload = job.id, job.attempts, dict(job.payload)
        try:
            validate_job(db, job)
        except HTTPException as exc:
            settle(db, job, 0)
            job.status, job.error, job.finished_at = "cancelled", exc.detail, now()
            db.commit()
            return True
        db.commit()
    try:
        result = run_search(payload["query"], payload["sources"], payload["limit"], payload["active_only"])
        if payload.get("contact_budget"):
            from .web_contacts import discover_contacts
            result["contacts"], result["contact_requests"] = discover_contacts(result.get("items", []), payload["contact_budget"])
        error = None if result["successful_requests"] else "Источники не ответили. Откройте подробности поиска и повторите позже."
    except Exception:
        result, error = {}, "Поиск прерван из-за ошибки исполнителя. Повторите позже."
    with factory() as db:
        job = db.get(Job, identifier)
        if job.status != "running" or job.attempts != attempt:
            return True
        lock_organization(db, job.organization_id)
        successful = result.get("successful_requests", 0)
        settle(db, job, successful, result.get("contact_requests", 0))
        try:
            validate_job(db, job)
            job.status = "failed" if error else "completed"
        except HTTPException as exc:
            job.status, error = "cancelled", exc.detail
        job.result, job.error, job.finished_at = result, error, now()
        db.add(AuditEvent(organization_id=job.organization_id, user_id=job.created_by, campaign_id=job.campaign_id,
                         action="search." + job.status, entity_id=job.id,
                         description="Поиск завершён: " + str(len(result.get("items", []))) + " результатов" if not error else error))
        if job.status == "completed":
            from .config import get_settings
            from .ai_service import queue_analysis
            if get_settings().ai_auto_run:
                try:
                    queue_analysis(db, db.get(Campaign, job.campaign_id), job.created_by, automatic=True)
                except HTTPException:
                    pass
        db.commit()
    return True

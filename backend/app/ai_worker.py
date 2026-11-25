from datetime import timedelta
from sqlalchemy import select, update
from fastapi import HTTPException
from .models import Job, Campaign, Document, Usage, AuditEvent, now
from .search_worker import validate_job
from .services import lock_organization
from .ai_provider import analyze_context, AIError, ai_status, request_plan
from .ai_harness import prepare_stage, merge_stage, reserve_budget, digest, execution_key
from .ai_service import context_for, validate_report, queue_analysis, prepare_outreach
from .config import get_settings


def settle(db, job, charged):
    usage = db.get(Usage, job.payload.get("usage_id")) if job.payload.get("usage_id") else None
    if usage and usage.organization_id == job.organization_id:
        usage.reserved = max(0, usage.reserved - 1)
        usage.used += int(charged)


def process_ai_one(factory):
    with factory() as db:
        # A paid call with uncertain completion is never blindly retried after a crash.
        stale = db.scalars(select(Job).where(Job.kind == "analyze", Job.status == "running", Job.started_at < now() - timedelta(minutes=5))).all()
        for job in stale:
            expired = db.execute(update(Job).where(Job.id == job.id, Job.status == "running", Job.started_at < now() - timedelta(minutes=5)).values(status="failed"))
            if expired.rowcount != 1:
                continue
            settle(db, job, True)
            job.status, job.error, job.finished_at = "failed", "Исполнитель был прерван. Проверьте историю OpenRouter перед повторным запуском", now()
        job = db.scalar(select(Job).where(Job.kind == "analyze", Job.status == "queued").order_by(Job.created_at).limit(1))
        if not job:
            db.commit(); return False
        claimed = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(status="running", started_at=now(), attempts=Job.attempts + 1))
        if claimed.rowcount != 1:
            db.rollback(); return True
        db.refresh(job)
        identifier, attempt = job.id, job.attempts
        try:
            actor = validate_job(db, job)
            task = db.get(Campaign, job.campaign_id)
            context, fragments, fingerprint = context_for(db, task)
            job.payload = {**job.payload, "input_fingerprint": fingerprint, "execution_key": execution_key()}
            if any(d.status in {"queued", "processing"} for d in db.scalars(select(Document).where(Document.campaign_id == task.id)).all()):
                job.status = "queued"
                db.commit(); return False
            lock_organization(db, job.organization_id)
            stage, previous, reused, base_id = prepare_stage(db, task, context)
            needs_call = previous is None or bool(stage["leads"])
            planned = request_plan(stage)[1] if needs_call else {"provider": "local_cache", "model": get_settings().ai_model,
                "stage": "leads", "cost_usd": 0, "reserved_cost_usd": 0}
            reserve_budget(db, task, planned)
            job.result = {"metadata": {**planned, "base_report_id": base_id, "reused_leads": len(reused)}}
        except AIError as exc:
            settle(db, job, False)
            job.status, job.error, job.finished_at = "blocked", str(exc), now()
            job.result = {"metadata": {**exc.metadata, "reserved_cost_usd": 0, "cost_usd": 0}}
            db.commit(); return True
        except (HTTPException, ValueError) as exc:
            settle(db, job, False)
            job.status, job.error, job.finished_at = "cancelled", getattr(exc, "detail", "Не удалось подготовить контекст ИИ"), now()
            if get_settings().ai_auto_run:
                try:
                    validate_job(db, job, check_version=False)
                    queue_analysis(db, db.get(Campaign, job.campaign_id), job.created_by, automatic=True)
                except HTTPException:
                    pass
            db.commit(); return True
        db.commit()
    called, metadata = False, job.result.get("metadata", {})
    try:
        if needs_call:
            called = ai_status()["available"]
            data, received = analyze_context(stage)
            metadata = {**metadata, **received}
        else:
            data = {"leads": []}
        data = merge_stage(data, previous, reused, stage)
        report = validate_report(data, context, fragments)
        result = {**report, "metadata": metadata, "input_fingerprint": fingerprint,
                  "base_fingerprint": context["base_fingerprint"], "lead_fingerprints": {l["source_id"]: digest(l) for l in context["leads"]},
                  "documents": [{"id": d["document_id"], "filename": d["filename"], "complete": d["complete"]} for d in context["documents"]]}
        error = None
    except AIError as exc:
        called = exc.charged
        metadata = {**metadata, **exc.metadata}
        if not called and metadata.get("cost_usd") is None:
            metadata["cost_usd"] = 0
        result, error = {"metadata": metadata}, str(exc)
    except Exception:
        result, error = {"metadata": metadata}, "Не удалось проверить ответ ИИ по источникам. Результат не применён"
    with factory() as db:
        job = db.get(Job, identifier)
        if job.status != "running" or job.attempts != attempt:
            return True
        lock_organization(db, job.organization_id)
        settle(db, job, called)
        task = db.get(Campaign, job.campaign_id)
        try:
            actor = validate_job(db, job)
            if context_for(db, task)[2] != fingerprint:
                raise HTTPException(409, "Документы или результаты поиска изменились во время анализа. Требуется новый анализ")
            job.status = "failed" if error else "completed"
            if not error and task.direction == "purchase" and result["offers"]:
                task.status = "comparison"
            if not error:
                result["outreach_ids"] = prepare_outreach(db, task, job, result)
            if not error and get_settings().ai_auto_run and result.get("search_queries") and not db.scalar(select(Job.id).where(Job.campaign_id == task.id, Job.kind == "search", Job.payload["requirements_version"].as_integer() == task.requirements_version)):
                from .task_routes import create_job
                from .schemas import JobInput
                try:
                    create_job(task.id, JobInput(kind="search", query=result["search_queries"][0][:300], sources=["web"] if task.direction == "purchase" else ["web", "onlinecontract"]), actor, db)
                except HTTPException:
                    result["warnings"].append("Автоматический поиск не запущен; используйте подготовленную фразу во вкладке ИИ")
        except HTTPException as exc:
            job.status, error = "cancelled", exc.detail
        job.result, job.error, job.finished_at = result, error, now()
        db.add(AuditEvent(organization_id=job.organization_id, user_id=job.created_by, campaign_id=job.campaign_id,
            action="ai." + job.status, entity_id=job.id, description="ИИ разобрал документы и подготовил рекомендации" if not error else error))
        if job.status == "cancelled" and get_settings().ai_auto_run:
            try:
                validate_job(db, job, check_version=False)
                queue_analysis(db, task, job.created_by, automatic=True)
            except HTTPException:
                pass
        db.commit()
    return True

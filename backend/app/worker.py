"""Durable local worker. Start independently from the HTTP process."""
import json
import subprocess
import sys
import time
from datetime import timedelta
from sqlalchemy import select, update
from fastapi import HTTPException
from .db import SessionLocal
from .models import Job, Document, Usage, AuditEvent, now
from .services import consume, lock_organization
from .file_routes import stored_path
from .config import ROOT, get_settings


def process_document_one(factory=SessionLocal):
    with factory() as db:
        # A crashed process's local extraction lease can be retried safely.
        db.execute(update(Job).where(Job.kind == "extract_document", Job.status == "running", Job.started_at < now() - timedelta(minutes=5)).values(status="queued"))
        job = db.scalar(select(Job).where(Job.status == "queued", Job.kind == "extract_document").order_by(Job.created_at).limit(1))
        if not job:
            db.commit(); return False
        result = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued").values(status="running", started_at=now(), attempts=Job.attempts + 1))
        if result.rowcount != 1:
            db.rollback(); return True
        db.refresh(job)
        identifier, attempt, payload = job.id, job.attempts, job.payload
        document = db.get(Document, payload["document_id"])
        path, extension = stored_path(document), document.extension
        document.status = "processing"
        db.commit()
    try:
        completed = subprocess.run([sys.executable, "-m", "backend.app.document_parser", str(path), extension], cwd=ROOT, capture_output=True, timeout=get_settings().document_timeout)
        parsed = json.loads(completed.stdout.decode("utf-8"))
        if not parsed.get("ok"):
            raise ValueError(parsed.get("error", "Не удалось разобрать документ"))
        extracted = parsed["data"]
        error = None
    except Exception as exc:
        extracted, error = {}, str(exc)[:1000]
    with factory() as db:
        job = db.get(Job, identifier)
        if job.status != "running" or job.attempts != attempt:
            return True
        lock_organization(db, job.organization_id)
        document = db.get(Document, payload["document_id"])
        usage = db.get(Usage, payload["usage_id"]) if payload.get("usage_id") else None
        charged_pages = payload.get("charged_pages", 0)
        if not error:
            try:
                consume(db, job.organization_id, "document_pages", max(0, extracted.get("pages", 0) - charged_pages))
                charged_pages = max(charged_pages, extracted.get("pages", 0))
            except HTTPException as exc:
                error = exc.detail
        if usage and usage.reserved > 0:
            usage.reserved -= 1
            if not error:
                usage.used += 1
        extracted["quota_pages_charged"] = charged_pages
        document.extracted = extracted
        document.error = error
        document.status = "error" if error else "needs_ocr" if extracted.get("ocr_required") else "extracted"
        job.status = "failed" if error else "completed"
        job.error, job.result, job.finished_at = error, {"document_id": document.id, "status": document.status}, now()
        db.add(AuditEvent(organization_id=job.organization_id, user_id=job.created_by, campaign_id=job.campaign_id, action="document.extracted" if not error else "document.failed", entity_id=document.id, description="Документ обработан локальным исполнителем" if not error else "Документ требует проверки: " + error))
        if document.campaign_id and not error and get_settings().ai_auto_run:
            from .ai_service import queue_analysis
            from .models import Campaign
            try:
                queue_analysis(db, db.get(Campaign, document.campaign_id), job.created_by, automatic=True)
            except HTTPException as exc:
                document.extracted = {**document.extracted, "warnings": document.extracted.get("warnings", []) + ["ИИ-анализ не запущен: " + str(exc.detail)]}
        db.commit()
    return True


def process_one(factory=SessionLocal):
    from .search_worker import process_search_one
    from .ai_worker import process_ai_one
    return process_document_one(factory) or process_search_one(factory) or process_ai_one(factory)


if __name__ == "__main__":
    print("NIRA search and document worker started", flush=True)
    while True:
        try:
            if not process_one():
                time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception as error:
            print("Worker error:", type(error).__name__, flush=True)
            time.sleep(5)

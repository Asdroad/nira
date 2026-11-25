import hashlib
import os
import re
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .config import get_settings
from .models import Document, Job
from .security import Actor, get_actor, tenant_object, campaign_object, visible_campaigns
from .services import audit, consume, columns, require_access, lock_organization

router = APIRouter(prefix="/api/v1/documents", tags=["Документы"])
SUPPORTED = {"pdf", "docx", "xlsx", "csv"}


def visible_document(db, identifier, actor):
    document = tenant_object(db, Document, identifier, actor)
    if document.campaign_id:
        campaign_object(db, document.campaign_id, actor)
    elif not actor.can("view_team") and document.uploaded_by != actor.user.id:
        raise HTTPException(404, "Документ не найден")
    return document


def stored_path(document):
    storage = get_settings().storage_path.resolve()
    path = (storage / document.blob).resolve()
    if not path.is_relative_to(storage):
        raise RuntimeError("Invalid internal file path")
    return path


@router.get("")
def list_documents(campaign_id: str | None = None, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    query = select(Document).where(Document.organization_id == actor.organization.id)
    if campaign_id:
        campaign_object(db, campaign_id, actor)
        query = query.where(Document.campaign_id == campaign_id)
    elif not actor.can("view_team"):
        task_ids = [t.id for t in db.scalars(visible_campaigns(actor)).all()]
        query = query.where(Document.campaign_id.in_(task_ids) | ((Document.campaign_id.is_(None)) & (Document.uploaded_by == actor.user.id)))
    return {"items": [columns(d, {"blob", "extracted"}) for d in db.scalars(query.order_by(Document.created_at.desc())).all()]}


@router.post("", status_code=201)
def upload(campaign_id: str | None = Form(None), file: UploadFile = File(...), actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    require_access(db, actor.organization.id)
    if campaign_id:
        campaign_object(db, campaign_id, actor)
    filename = (file.filename or "document").replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:240]
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in SUPPORTED:
        raise HTTPException(415, "Поддерживаются PDF, DOCX, XLSX и CSV")
    directory = get_settings().storage_path / actor.organization.id
    directory.mkdir(parents=True, exist_ok=True)
    relative = actor.organization.id + "/" + str(uuid4())
    path = get_settings().storage_path / relative
    size, checksum = 0, hashlib.sha256()
    try:
        with path.open("xb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > get_settings().max_file_mb * 1024 * 1024:
                    raise HTTPException(413, "Файл превышает лимит 25 МБ")
                checksum.update(chunk); target.write(chunk)
        if size == 0:
            raise HTTPException(422, "Файл пуст")
        lock_organization(db, actor.organization.id)
        previous = db.scalar(select(Document).where(Document.organization_id == actor.organization.id,
            Document.campaign_id == campaign_id, Document.sha256 == checksum.hexdigest()))
        if previous:
            visible_document(db, previous.id, actor)
            path.unlink()
            return {**columns(previous, {"blob", "extracted"}), "duplicate": True}
        usage = consume(db, actor.organization.id, "documents", reserve=True)
        document = Document(organization_id=actor.organization.id, campaign_id=campaign_id, uploaded_by=actor.user.id, filename=filename, extension=extension, size=size, sha256=checksum.hexdigest(), blob=relative)
        db.add(document); db.flush()
        job = Job(organization_id=actor.organization.id, campaign_id=campaign_id, created_by=actor.user.id, kind="extract_document", payload={"document_id": document.id, "usage_id": usage.id})
        db.add(job); db.flush()
        audit(db, actor, "document.uploaded", document, "Загружен документ «" + filename + "»", campaign_id)
        return {**columns(document, {"blob", "extracted"}), "job_id": job.id}
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        file.file.close()


@router.get("/{document_id}")
def document_detail(document_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    return columns(visible_document(db, document_id, actor), {"blob"})


@router.post("/{document_id}/reprocess", status_code=201)
def reprocess(document_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    require_access(db, actor.organization.id)
    document = visible_document(db, document_id, actor)
    if document.status not in {"needs_ocr", "error"}:
        raise HTTPException(409, "Документ уже обработан или находится в очереди")
    if not stored_path(document).is_file():
        raise HTTPException(404, "Оригинал документа отсутствует")
    charged_pages = document.extracted.get("quota_pages_charged", document.extracted.get("pages", 0) if document.status == "needs_ocr" else 0)
    # Successful text extraction already counted the document, even if OCR was missing.
    usage = consume(db, actor.organization.id, "documents", reserve=True) if document.status == "error" and not charged_pages else None
    job = Job(organization_id=actor.organization.id, campaign_id=document.campaign_id, created_by=actor.user.id,
              kind="extract_document", payload={"document_id": document.id, "usage_id": usage.id if usage else None, "charged_pages": charged_pages})
    document.status, document.error = "queued", None
    db.add(job); db.flush()
    audit(db, actor, "document.reprocess", document, "Повторная локальная обработка документа", document.campaign_id)
    return {**columns(document, {"blob", "extracted"}), "job_id": job.id}


@router.get("/{document_id}/download")
def download(document_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    document = visible_document(db, document_id, actor)
    path = stored_path(document)
    if not path.exists():
        raise HTTPException(404, "Файл отсутствует в хранилище")
    return FileResponse(path, filename=document.filename, media_type="application/octet-stream", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

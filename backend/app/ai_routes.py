import csv
import io
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Job, Document, Outreach
from .security import Actor, get_actor, campaign_object
from .services import columns, require_open_task, require_access, lock_organization
from .ai_provider import ai_status
from .ai_service import context_for, latest_analysis, queue_analysis
from .ai_harness import task_costs
from .workspace_routes import safe_csv

router = APIRouter(prefix="/api/v1", tags=["ИИ: документы, сравнение и тексты"])


@router.get("/ai/status")
def status(actor: Actor = Depends(get_actor)):
    return ai_status()


@router.post("/tasks/{task_id}/ai", status_code=201)
def run(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    lock_organization(db, actor.organization.id)
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    require_access(db, task.organization_id)
    if not task.requirements_approved:
        raise HTTPException(409, "Утвердите требования перед запуском ИИ")
    return columns(queue_analysis(db, task, actor.user.id))


@router.get("/tasks/{task_id}/ai")
def analysis(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    task = campaign_object(db, task_id, actor)
    jobs = db.scalars(select(Job).where(Job.campaign_id == task.id, Job.kind == "analyze").order_by(Job.created_at.desc()).limit(10)).all()
    completed = latest_analysis(db, task)
    stale = False
    if completed:
        try:
            stale = completed.result.get("input_fingerprint") != context_for(db, task)[2]
        except HTTPException:
            stale = True
    return {"task_id": task.id, "status": ai_status(), "job": columns(jobs[0]) if jobs else None,
            "report": completed.result if completed else None, "report_id": completed.id if completed else None,
            "stale": stale, "requirements_version": task.requirements_version,
            "costs": task_costs(db, task),
            "outreach": [columns(o) for o in db.scalars(select(Outreach).where(Outreach.campaign_id == task.id).order_by(Outreach.created_at.desc())).all()],
            "documents": [columns(d, {"blob", "extracted"}) for d in db.scalars(select(Document).where(Document.campaign_id == task.id).order_by(Document.created_at)).all()],
            "dispatch": {"email": {"status": "not_configured", "message": "Почта пока не подключена. ИИ готовит текст, отправка не выполнена"},
                         "platforms": {"status": "not_configured", "message": "Для публикации нужны аккаунт и доступный интерфейс конкретной площадки"}}}


def current_report(task_id, actor, db):
    actor.require("export")
    data = analysis(task_id, actor, db)
    if not data["report"] or data["stale"]:
        raise HTTPException(409, "Сначала выполните ИИ-анализ текущих документов и требований")
    return data["report"]


@router.post("/tasks/{task_id}/ai/search", status_code=201)
def ai_search(task_id: str, query_index: int = 0, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    actor.write()
    task = campaign_object(db, task_id, actor)
    require_open_task(task)
    data = analysis(task_id, actor, db)
    queries = (data["report"] or {}).get("search_queries", [])
    if data["stale"] or not 0 <= query_index < len(queries):
        raise HTTPException(409, "Сначала выполните актуальный ИИ-анализ")
    from .task_routes import create_job
    from .schemas import JobInput
    return create_job(task.id, JobInput(kind="search", query=queries[query_index][:300], sources=["web"] if task.direction == "purchase" else ["web", "onlinecontract"]), actor, db)


@router.get("/tasks/{task_id}/ai/export.csv")
def export_csv(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    report = current_report(task_id, actor, db)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    writer.writerow(["Компания", "Документ", "Товар", "Количество", "Цена из КП", "Полная стоимость", "Валюта", "Налоги", "Доставка", "Оплата", "Соответствие", "Почему выбрать / отклонить", "Вопросы", "Источники"])
    reasons = {r["document_id"]: r["reason"] for r in report["ranking"]}
    for offer in report["offers"]:
        evidence = [f"{ref['filename']} {json.dumps(ref['locator'], ensure_ascii=False)}: {ref['excerpt']}" for field in ("price", "delivery", "tax", "payment") for ref in offer[field]["evidence"]]
        row = [offer["supplier"]["value"], offer["filename"], offer["product"]["value"], offer["quantity"]["value"], offer["price"]["value"], offer["full_cost"], offer["currency"], offer["tax"]["value"], offer["delivery"]["value"], offer["payment"]["value"], offer["assessment"], reasons.get(offer["document_id"], "; ".join(offer["excluded_reasons"])), "; ".join(offer["clarification_questions"]), "\n".join(evidence)]
        writer.writerow([safe_csv(value) for value in row])
    writer.writerow([])
    writer.writerow(["Рекомендация", safe_csv(report["recommendation_reason"])])
    writer.writerow(["Ограничения", safe_csv("; ".join(report["warnings"]))])
    return Response("\ufeff" + stream.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="NIRA_AI_comparison.csv"', "Cache-Control": "no-store"})


@router.get("/tasks/{task_id}/ai/export.md")
def export_report(task_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    report = current_report(task_id, actor, db)
    # Downloaded plain text, not inserted as HTML into the app.
    lines = ["# Анализ NIRA", report["summary"], "## Рекомендация", report["recommendation_reason"], "## Критерии"]
    for c in report["criteria"]:
        lines.append(f"- {c['requirement']} ({'обязательно' if c['required'] else 'желательно'})")
    for offer in report["offers"]:
        lines += ["## " + (offer["supplier"]["value"] or offer["filename"]), "Документ: " + offer["filename"], "Оценка: " + offer["assessment"], "Полная стоимость: " + (offer["full_cost"] or "неизвестна") + " " + (offer["currency"] or "")]
        for field, label in [("product", "Товар"), ("quantity", "Количество"), ("price", "Цена"), ("tax", "Налоги"), ("delivery", "Доставка"), ("payment", "Оплата"), ("validity", "Срок действия")]:
            lines.append(label + ": " + (offer[field]["value"] or "не указано"))
            for ref in offer[field]["evidence"]:
                lines.append(f"  Источник: {ref['filename']}, {json.dumps(ref['locator'], ensure_ascii=False)} — {ref['excerpt']}")
        for check in offer["checks"]:
            lines.append(f"- {check['criterion_id']}: {check['status']} — {check['explanation']}")
            for ref in check["evidence"]:
                lines.append(f"  Источник: {ref['filename']}, {json.dumps(ref['locator'], ensure_ascii=False)} — {ref['excerpt']}")
        lines += ["Риски: " + "; ".join(offer["risks"]), "Уточнить: " + "; ".join(offer["clarification_questions"])]
    lines += ["## Почему один вариант лучше другого"] + [r["document_id"] + ": " + r["reason"] for r in report["ranking"]]
    lines += ["## Вопросы"] + report["questions"] + ["## Ограничения"] + report["warnings"]
    lines += ["## Объявление", report["announcement"]["title"], report["announcement"]["body"], "## Шаблон письма", report["email"]["title"], report["email"]["body"]]
    return Response("\n\n".join(lines), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="NIRA_AI_analysis.md"', "Cache-Control": "no-store"})

import hashlib
import json
import re
from datetime import date
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select, func
from pydantic import TypeAdapter, EmailStr, ValidationError
from .config import get_settings
from .models import Document, Job, RequestItem, Organization, AuditEvent, Counterparty, CampaignCounterparty, Outreach, now
from .services import consume
from .ai_provider import ai_status
from .ai_harness import digest, execution_key


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def contains_number(number, evidence):
    if number is None:
        return False
    for ref in evidence:
        # Spaces are accepted as thousands separators, comma as decimal separator.
        for raw in re.findall(r"(?<!\d)\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?", ref["excerpt"]):
            try:
                parsed = Decimal(re.sub(r"[ \u00a0\u202f]", "", raw).replace(",", "."))
                if parsed == Decimal(str(number)):
                    return True
            except Exception:
                continue
    return False


def citation_pieces(text, limit=1600):
    """Keep source substrings intact and small enough to quote without model rewriting."""
    while len(text) > limit:
        end = text.rfind(" ", 0, limit + 1)
        if end < limit // 2:
            end = limit
        yield text[:end]
        # Overlap preserves prices and conditions that cross a chunk boundary.
        text = text[max(1, end - 160):]
    if text:
        yield text


def context_for(db, task):
    org = db.get(Organization, task.organization_id)
    documents = db.scalars(select(Document).where(Document.organization_id == task.organization_id,
        Document.campaign_id == task.id).order_by(Document.created_at, Document.id)).all()
    if len(documents) > 30:
        raise HTTPException(422, "В одном анализе поддерживается до 30 документов. Разделите закупку на задачи")
    search = db.scalar(select(Job).where(Job.campaign_id == task.id, Job.kind == "search", Job.status == "completed",
        Job.organization_id == task.organization_id, Job.payload["requirements_version"].as_integer() == task.requirements_version).order_by(Job.created_at.desc()).limit(1))
    items = db.scalars(select(RequestItem).where(RequestItem.campaign_id == task.id).order_by(RequestItem.id)).all()
    trusted = {"direction": task.direction, "title": task.title, "quantity": task.quantity, "region": task.region,
        "budget": str(task.budget) if task.budget is not None else None, "deadline": str(task.deadline) if task.deadline else None,
        "requirements": task.requirements, "requirements_version": task.requirements_version,
        "items": [{"name": i.name, "quantity": str(i.quantity) if i.quantity is not None else None, "unit": i.unit, "characteristics": i.characteristics} for i in items]}
    signature = {"task": trusted, "organization": {"name": org.name, "description": org.description, "industries": org.industries},
        "documents": [{"id": d.id, "sha256": d.sha256, "status": d.status, "extracted": d.extracted} for d in documents],
        "search": search.result if search else None}
    signature["as_of"] = date.today().isoformat()
    fingerprint = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    base_fingerprint = digest({k: v for k, v in signature.items() if k != "search"})
    required_fields = [{"id": "task_" + field, "requirement": label + ": " + str(trusted[field]), "task_excerpt": str(trusted[field])}
        for field, label in [("quantity", "Количество"), ("region", "Регион доставки"), ("budget", "Бюджет"), ("deadline", "Срок")]
        if trusted[field] is not None and normalize(trusted[field])]
    workflow = ({"role": "ПОСТАВЩИК", "objective": "Продать собственный товар заказчику. Искать покупателей и закупки. Объявление — предложение товара, письмо — предложение заказчику; НЕ запрос КП поставщикам."}
                if task.direction == "sales" else {"role": "ЗАКАЗЧИК", "objective": "Закупить товар. Искать поставщиков, запросить их КП, сравнить полученные предложения."})
    context = {"today": date.today().isoformat(), "trusted_task": trusted, "required_task_fields": required_fields, "workflow": workflow,
               "organization": signature["organization"], "documents": [], "leads": [], "warnings": [], "base_fingerprint": base_fingerprint}
    remaining = get_settings().ai_max_context_chars - len(json.dumps(trusted, ensure_ascii=False))
    fragments = {}
    for doc in documents:
        entry = {"document_id": doc.id, "filename": doc.filename, "complete": doc.status == "extracted", "fragments": [], "warnings": list(doc.extracted.get("warnings", []))}
        source_fragments = [(index, part, text, fragment) for index, fragment in enumerate(doc.extracted.get("fragments", []))
                            for part, text in enumerate(citation_pieces(fragment.get("text", "")))]
        for index, part, text, fragment in source_fragments:
            identifier = f"{doc.id}:{index}" + (f":{part}" if part else "")
            if remaining < len(text) + 160:
                entry["complete"] = False
                entry["warnings"].append("Лимит контекста: часть документа не передана модели")
                break
            ref = {"fragment_id": identifier, "document_id": doc.id, "filename": doc.filename, "locator": fragment.get("locator", {}), "text": text}
            fragments[identifier] = ref
            entry["fragments"].append(ref)
            remaining -= len(text) + 160
        if not entry["fragments"] or entry["warnings"]:
            entry["complete"] = False
        context["documents"].append(entry)
    if search:
        contacts = {entry["source_id"]: entry for entry in search.result.get("contacts", [])}
        for index, item in enumerate(search.result.get("items", [])[:50]):
            lead = {"source_id": str(index), "title": item.get("title"), "url": item.get("url"), "description": item.get("description", "")[:2000], "kind": item.get("kind"), "site_pages": contacts.get(str(index), {}).get("pages", [])}
            if remaining < len(json.dumps(lead, ensure_ascii=False)):
                context["warnings"].append("Часть поисковой выдачи не вошла в контекст")
                break
            context["leads"].append(lead)
            remaining -= len(json.dumps(lead, ensure_ascii=False))
    # Account for JSON metadata as well as text; don't silently transmit an unbounded prompt.
    while len(json.dumps(context, ensure_ascii=False)) > get_settings().ai_max_context_chars:
        if context["leads"]:
            context["leads"].pop()
            continue
        last = next((d for d in reversed(context["documents"]) if d["fragments"]), None)
        if last is None:
            raise HTTPException(422, "Описание задачи слишком большое для настроенного контекста ИИ")
        removed = last["fragments"].pop()
        fragments.pop(removed["fragment_id"], None)
        last["complete"] = False
    if any(not d["complete"] for d in context["documents"]):
        context["warnings"].append("Не все документы полностью прочитаны. Неполные документы не участвуют в окончательной рекомендации")
    return context, fragments, fingerprint


def queue_analysis(db, task, author_id, automatic=False):
    if not task.requirements_approved or task.status in {"closed", "cancelled"}:
        return None
    existing = db.scalar(select(Job).where(Job.campaign_id == task.id, Job.kind == "analyze", Job.status.in_(["queued", "running"])))
    if existing:
        return existing
    _, _, fingerprint = context_for(db, task)
    matched = db.scalar(select(Job).where(Job.campaign_id == task.id, Job.organization_id == task.organization_id,
        Job.kind == "analyze", Job.payload["input_fingerprint"].as_string() == fingerprint,
        Job.payload["execution_key"].as_string() == execution_key(),
        Job.status.in_(["completed", "blocked", "failed"])).order_by(Job.created_at.desc()).limit(1))
    if matched and matched.status == "completed":
        return None if automatic else matched
    if automatic and matched:
        return None
    ready = ai_status()["available"]
    usage = consume(db, task.organization_id, "ai_runs", reserve=True) if ready else None
    job = Job(organization_id=task.organization_id, campaign_id=task.id, created_by=author_id, kind="analyze",
        status="queued" if ready else "blocked", error=None if ready else ai_status()["message"],
        payload={"requirements_version": task.requirements_version, "input_fingerprint": fingerprint,
                 "usage_id": usage.id if usage else None, "automatic": automatic, "execution_key": execution_key()})
    db.add(job); db.flush()
    db.add(AuditEvent(organization_id=task.organization_id, user_id=author_id, campaign_id=task.id,
        action="ai.queued" if ready else "ai.blocked", entity_id=job.id,
        description="ИИ-анализ поставлен в очередь" if ready else ai_status()["message"]))
    return job


def latest_analysis(db, task):
    return db.scalar(select(Job).where(Job.campaign_id == task.id, Job.organization_id == task.organization_id,
        Job.kind == "analyze", Job.status == "completed", Job.payload["requirements_version"].as_integer() == task.requirements_version)
        .order_by(Job.created_at.desc()).limit(1))


def validate_report(report, context, fragments):
    """Check evidence locally; no fabricated references or recommendations on unknown terms."""
    warnings = list(context["warnings"])
    docs = {d["document_id"]: d for d in context["documents"]}
    criteria = {c["id"]: c for c in report["criteria"]}
    if len(criteria) != len(report["criteria"]):
        raise ValueError("Повторяющиеся критерии в ответе ИИ")
    def values(value):
        if isinstance(value, dict):
            return " ".join(values(v) for v in value.values())
        if isinstance(value, list):
            return " ".join(values(v) for v in value)
        return str(value or "")
    task_text = normalize(values(context["trusted_task"]))
    for c in criteria.values():
        if normalize(c["task_excerpt"]) not in task_text:
            raise ValueError("Критерий ИИ не подтверждён требованиями пользователя")
    # Explicit task fields cannot disappear from the model's comparison matrix.
    for field, label in [("quantity", "Количество"), ("region", "Регион доставки"), ("budget", "Бюджет"), ("deadline", "Срок")]:
        value = context["trusted_task"].get(field)
        if value is not None and normalize(value) and not any(c["required"] and normalize(value).casefold() in normalize(c["task_excerpt"]).casefold() for c in criteria.values()):
            identifier = "required_" + field + "_" + str(len(criteria))
            criterion = {"id": identifier, "requirement": label + ": " + str(value), "required": True, "task_excerpt": str(value)}
            criteria[identifier] = criterion
            report["criteria"].append(criterion)
            warnings.append("ИИ не проверил поле «" + label + "»; предложение требует уточнения")
    required = {c["id"] for c in criteria.values() if c["required"]}

    def verify(evidence, document_id):
        checked = []
        for ref in evidence:
            source = fragments.get(ref["fragment_id"])
            if not source or source["document_id"] != document_id or normalize(ref["excerpt"]) not in normalize(source["text"]):
                warnings.append("Недостоверная ссылка ИИ на источник удалена")
                continue
            checked.append({**ref, "filename": source["filename"], "locator": source["locator"], "document_id": document_id})
        return checked

    seen = set()
    for offer in report["offers"]:
        identifier = offer["document_id"]
        if identifier not in docs or identifier in seen:
            raise ValueError("ИИ указал неизвестный или повторяющийся документ")
        seen.add(identifier)
        offer["filename"] = docs[identifier]["filename"]
        for field in ("supplier", "product", "quantity", "price", "tax", "delivery", "payment", "validity"):
            fact = offer[field]
            fact["evidence"] = verify(fact["evidence"], identifier)
            if not fact["evidence"]:
                fact["value"] = None
        if not offer["price"]["value"] or offer["price_basis"] != "total":
            offer["total_amount"] = None
        if offer["total_amount"] is not None and not contains_number(offer["total_amount"], offer["price"]["evidence"]):
            offer["total_amount"] = None
            warnings.append("Числовая цена ИИ не подтверждена цитатой и не участвует в расчёте")
        if not offer["tax"]["value"]:
            offer["tax_included"] = offer["tax_amount"] = None
        if offer["tax_amount"] is not None and not contains_number(offer["tax_amount"], offer["tax"]["evidence"]):
            offer["tax_amount"] = None
        if not offer["delivery"]["value"]:
            offer["delivery_included"] = offer["delivery_cost"] = offer["delivery_days"] = None
        if offer["delivery_cost"] is not None and not contains_number(offer["delivery_cost"], offer["delivery"]["evidence"]):
            offer["delivery_cost"] = None
        if offer["delivery_days"] is not None and not contains_number(offer["delivery_days"], offer["delivery"]["evidence"]):
            offer["delivery_days"] = None
        if not offer["validity"]["value"]:
            offer["valid_until"] = None
        checks = {c["criterion_id"]: c for c in offer["checks"]}
        if len(checks) != len(offer["checks"]) or not set(checks).issubset(criteria):
            raise ValueError("Некорректная матрица критериев")
        for check in checks.values():
            check["evidence"] = verify(check["evidence"], identifier)
            if not check["evidence"]:
                check["status"] = "unknown"
        for missing in required - set(checks):
            checks[missing] = {"criterion_id": missing, "status": "unknown", "explanation": "ИИ не проверил обязательное условие", "evidence": []}
        offer["checks"] = list(checks.values())
        failures = [c for c in checks.values() if c["criterion_id"] in required and c["status"] == "does_not_match"]
        unknowns = [c for c in checks.values() if c["criterion_id"] in required and c["status"] == "unknown"]
        full = None
        if offer["total_amount"] is not None and offer["currency"] and offer["tax"]["value"] and (offer["tax_included"] is True or (offer["tax_included"] is False and offer["tax_amount"] is not None)):
            amount = Decimal(str(offer["total_amount"]))
            if offer["tax_included"] is False:
                amount += Decimal(str(offer["tax_amount"]))
            if offer["delivery_included"] is True:
                full = amount
                offer["delivery_cost"] = 0
            elif offer["delivery_included"] is False and offer["delivery_cost"] is not None:
                full = amount + Decimal(str(offer["delivery_cost"]))
        expired = False
        if offer["valid_until"]:
            try:
                expired = date.fromisoformat(offer["valid_until"]) < date.today()
            except ValueError:
                offer["valid_until"] = None
                unknowns.append({"explanation": "Не удалось проверить срок действия"})
        offer["full_cost"] = format(full.normalize(), "f") if full is not None else None
        offer["assessment"] = "does_not_match" if failures else "needs_clarification" if (
            not required or unknowns or not docs[identifier]["complete"] or full is None or not offer["payment"]["value"]
            or offer["delivery_days"] is None or not offer["supplier"]["value"] or expired or offer["role"] != "offer") else "matches"
        offer["eligible"] = offer["assessment"] == "matches"
        offer["excluded_reasons"] = [c["explanation"] for c in failures + unknowns]
        if full is None:
            offer["excluded_reasons"].append("Полная стоимость с налогами и доставкой неизвестна")
        if not offer["payment"]["value"]:
            offer["excluded_reasons"].append("Не подтверждены условия оплаты")
        if offer["delivery_days"] is None:
            offer["excluded_reasons"].append("Не подтверждён срок доставки")
        if not offer["supplier"]["value"]:
            offer["excluded_reasons"].append("Не подтверждено название поставщика")
        if expired:
            offer["excluded_reasons"].append("Срок действия предложения истёк")
        if not docs[identifier]["complete"]:
            offer["excluded_reasons"].append("Документ прочитан не полностью")
        budget = context["trusted_task"].get("budget")
        if budget is not None and full is not None:
            if offer["currency"] != "RUB":
                offer["assessment"], offer["eligible"] = "needs_clarification", False
                offer["excluded_reasons"].append("Бюджет в рублях; для сравнения нужен согласованный курс валюты")
            elif full > Decimal(budget):
                offer["assessment"], offer["eligible"] = "does_not_match", False
                offer["excluded_reasons"].append("Полная стоимость превышает бюджет заказчика")
    for missing in docs.keys() - seen:
        warnings.append("ИИ не разобрал документ «" + docs[missing]["filename"] + "». Общая рекомендация требует повторного анализа")
    eligible = {o["document_id"] for o in report["offers"] if o["eligible"]}
    currencies = {o["currency"] for o in report["offers"] if o["eligible"]}
    if len(currencies) > 1:
        eligible.clear()
        warnings.append("Цены указаны в разных валютах. Общий рейтинг без согласованного курса не составляется")
    rank_seen = set()
    report["ranking"] = [r for r in report["ranking"] if r["document_id"] in eligible and not (r["document_id"] in rank_seen or rank_seen.add(r["document_id"]))]
    if report["recommended_document_id"] not in eligible or docs.keys() - seen or any(not d["complete"] for d in docs.values()):
        report["recommended_document_id"] = None
        report["recommendation_reason"] = "Окончательная рекомендация не сформирована: требуются подтверждения условий или полный разбор документов. Причины и вопросы перечислены по каждому предложению."
    leads = {item["source_id"]: item for item in context["leads"]}
    report["leads"] = [{**lead, "url": leads[lead["source_id"]]["url"], "title": leads[lead["source_id"]]["title"]} for lead in report["leads"] if lead["source_id"] in leads]
    for lead in report["leads"]:
        pages = leads[lead["source_id"]].get("site_pages", [])
        name, excerpt = normalize(lead["company_name"]), normalize(lead["company_excerpt"])
        company_source = next((p["url"] for p in pages if name and excerpt and name in excerpt and excerpt in normalize(p["text"])), None)
        if not company_source:
            lead["company_name"] = lead["company_excerpt"] = None
        lead["company_source"] = company_source
        email = (lead["email"] or "").lower().strip()
        source = next((p["url"] for p in pages if email and email in p.get("emails", [])), None)
        lead["email"] = email if source else None
        lead["contact_source"] = source
        activity = normalize(lead.get("activity_excerpt"))
        activity_source = next((p["url"] for p in pages if activity and activity in normalize(p["text"])), None)
        lead["activity_source"] = activity_source
        if not activity_source:
            lead["activity_excerpt"] = None
            if lead.get("entity_type") in {"supplier", "customer"}:
                lead["entity_type"] = "unknown"
        target_type = "supplier" if context["trusted_task"]["direction"] == "purchase" else "customer"
        lead["outreach_eligible"] = bool(lead["relevance"] == "potential" and lead.get("entity_type") == target_type
            and lead.get("company_name") and lead.get("email") and lead.get("activity_excerpt"))
    report["warnings"] = list(dict.fromkeys(warnings))
    return report


def prepare_outreach(db, task, job, report):
    """Only sourced contacts become candidates; generated texts stay as drafts."""
    identifiers = []
    for lead in report.get("leads", []):
        if not lead.get("outreach_eligible"):
            continue
        try:
            TypeAdapter(EmailStr).validate_python(lead["email"])
        except ValidationError:
            continue
        party = db.scalar(select(Counterparty).where(Counterparty.organization_id == task.organization_id,
            func.lower(Counterparty.email) == lead["email"]))
        if party and party.blocked:
            continue
        if not party:
            party = Counterparty(organization_id=task.organization_id, name=lead["company_name"], email=lead["email"], website=lead["url"],
                category="Кандидат из открытого сайта", source=lead["contact_source"] + "\n" + lead["company_excerpt"])
            db.add(party); db.flush()
        if not db.scalar(select(CampaignCounterparty.id).where(CampaignCounterparty.campaign_id == task.id, CampaignCounterparty.counterparty_id == party.id)):
            db.add(CampaignCounterparty(organization_id=task.organization_id, campaign_id=task.id, counterparty_id=party.id))
        existing = db.scalar(select(Outreach).where(Outreach.campaign_id == task.id, Outreach.counterparty_id == party.id,
            Outreach.requirements_version == task.requirements_version, Outreach.kind == "initial"))
        if existing:
            continue
        letter = report["email"]
        draft = Outreach(organization_id=task.organization_id, campaign_id=task.id, counterparty_id=party.id, created_by=job.created_by,
            requirements_version=task.requirements_version, recipient=lead["email"], subject=letter["title"].replace("\r", " ").replace("\n", " "),
            body=letter["body"], kind="initial", purpose="procurement_request" if task.direction == "purchase" else "requested_quote",
            basis="ИИ подготовил черновик по открытой странице " + lead["contact_source"] + ". Основание отправки и соответствие получателя требуют проверки.",
            attachment_ids=[o["document_id"] for o in report["offers"] if o["role"] in {"offer", "price_list"}][:20] if task.direction == "sales" else [], status="draft")
        db.add(draft); db.flush()
        identifiers.append(draft.id)
    return identifiers

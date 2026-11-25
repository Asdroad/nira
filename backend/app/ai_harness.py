"""Durable, task-scoped reuse and bounded AI execution; no implicit paid retries."""
import copy
import hashlib
import json
from decimal import Decimal
from sqlalchemy import select
from .config import get_settings
from .models import Job
from .ai_schema import Analysis
from .ai_provider import AIError

REVISION = "nira-harness-5"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def execution_key():
    s = get_settings()
    return digest([REVISION, s.ai_model, s.ai_max_context_chars, s.ai_max_output_tokens,
                   s.ai_max_input_price, s.ai_max_output_price])


def raw_analysis(report):
    """Project validated/enriched reports back into the model contract for local merging."""
    schema = Analysis.model_json_schema()
    def project(value, spec):
        if "$ref" in spec:
            spec = schema["$defs"][spec["$ref"].split("/")[-1]]
        if isinstance(value, dict) and "properties" in spec:
            return {k: project(value[k], v) for k, v in spec["properties"].items() if k in value}
        if isinstance(value, list) and "items" in spec:
            return [project(v, spec["items"]) for v in value]
        return copy.deepcopy(value)
    return Analysis.model_validate(project(report, schema)).model_dump()


def prepare_stage(db, task, context):
    # Only within this task/tenant and with unchanged requirements, documents and date.
    candidates = db.scalars(select(Job).where(Job.campaign_id == task.id,
        Job.organization_id == task.organization_id, Job.kind == "analyze", Job.status == "completed",
        Job.payload["execution_key"].as_string() == execution_key()).order_by(Job.created_at.desc())).all()
    base = next((j for j in candidates if j.result.get("base_fingerprint") == context["base_fingerprint"]), None)
    if not base:
        return context, None, {}, None
    previous = raw_analysis(base.result)
    context["warnings"] = list(dict.fromkeys(context["warnings"] + base.result.get("warnings", [])))
    hashes = base.result.get("lead_fingerprints", {})
    assessments = {l["source_id"]: l for l in previous["leads"]}
    reused, changed = {}, []
    for lead in context["leads"]:
        identifier = lead["source_id"]
        if hashes.get(identifier) == digest(lead) and identifier in assessments:
            reused[identifier] = assessments[identifier]
        else:
            changed.append(lead)
    stage = {k: context[k] for k in ("today", "trusted_task", "workflow")}
    stage.update(stage="leads", leads=changed)
    return stage, previous, reused, base.id


def merge_stage(data, previous, reused, stage):
    if previous is None:
        return data
    requested = {l["source_id"] for l in stage["leads"]}
    supplied = [l["source_id"] for l in data["leads"]]
    if len(set(supplied)) != len(supplied) or set(supplied) != requested:
        raise AIError("ИИ не проверил все новые источники. Результат не применён", charged=True)
    return {**previous, "leads": list(reused.values()) + data["leads"]}


def task_costs(db, task):
    paid = Decimal(0)
    uncertain = Decimal(0)
    legacy = 0
    for job in db.scalars(select(Job).where(Job.campaign_id == task.id,
            Job.organization_id == task.organization_id, Job.kind == "analyze")).all():
        meta = job.result.get("metadata", {})
        cost, reserved = meta.get("cost_usd"), meta.get("reserved_cost_usd")
        if cost is not None:
            paid += Decimal(str(cost))
        elif reserved is not None:
            uncertain += Decimal(str(reserved))
        elif job.status in {"completed", "failed", "cancelled"} and job.attempts and not job.payload.get("execution_key"):
            legacy += 1
    return {"cost_usd": float(paid), "reserved_or_unknown_usd": float(uncertain),
            "legacy_calls_without_cost": legacy, "task_budget_usd": get_settings().ai_task_budget_usd}


def reserve_budget(db, task, metadata):
    cost = task_costs(db, task)
    total = sum(Decimal(str(cost[k])) for k in ("cost_usd", "reserved_or_unknown_usd"))
    if total + Decimal(str(metadata["reserved_cost_usd"])) > Decimal(str(get_settings().ai_task_budget_usd)):
        raise AIError("Достигнут лимит расходов ИИ для задачи. Измените лимит в настройках, чтобы продолжить")

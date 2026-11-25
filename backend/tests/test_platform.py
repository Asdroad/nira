from datetime import datetime, timedelta
from sqlalchemy import select
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.models import Invitation, Membership, Usage, Subscription, now
from backend.app.services import add_months, quota_period
from backend.app.worker import process_one
from .conftest import register, task, approve_requirements, company


def test_registration_persistence_and_csrf(platform):
    client, factory = platform
    registered = register(client)
    assert "password_hash" not in registered["user"]
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/v1/tasks", json={"direction": "purchase", "title": "Труба", "quantity": "1 т", "region": "Москва"}).status_code == 403
    client.headers["X-CSRF-Token"] = registered["csrf_token"]
    created = task(client)
    assert client.get("/api/v1/bootstrap").json()["tasks"][0]["id"] == created["id"]
    assert client.get("/api/v1/tasks/" + created["id"]).json()["requirements_approved"] is False
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/bootstrap").status_code == 401
    result = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "CorrectPassword123!"})
    assert result.status_code == 200
    assert len(client.get("/api/v1/tasks").json()["items"]) == 1


def test_cross_organization_isolation(platform):
    client, factory = platform
    register(client)
    first = task(client)
    cp = company(client, first)
    upload = client.post("/api/v1/documents", files={"file": ("a.csv", b"name;amount\nA;12", "text/csv")}, data={"campaign_id": first["id"]})
    assert upload.status_code == 201
    document = upload.json()
    with TestClient(app) as second:
        register(second, "other@example.com", "Организация Б")
        assert second.get("/api/v1/tasks/" + first["id"]).status_code == 404
        assert second.get("/api/v1/documents/" + document["id"]).status_code == 404
        assert second.get("/api/v1/documents/" + document["id"] + "/download").status_code == 404
        assert second.get("/api/v1/jobs/" + document["job_id"]).status_code == 404
        assert second.put("/api/v1/counterparties/" + cp["id"] + "/stop", json={"blocked": True}).status_code == 404
        assert second.post("/api/v1/tasks", json={"direction": "purchase", "title": "Попытка", "quantity": "1", "region": "Москва", "owner_id": first["owner_id"]}).status_code == 422
        assert second.get("/api/v1/bootstrap").json()["tasks"] == []


def test_five_employee_slots_expiration_and_revoke(platform):
    client, factory = platform
    register(client)
    for i in range(5):
        assert client.post("/api/v1/team/invitations", json={"email": f"employee{i}@example.com"}).status_code == 201
    assert client.post("/api/v1/team/invitations", json={"email": "sixth@example.com"}).status_code == 409
    with factory() as db:
        invite = db.scalar(select(Invitation))
        invite.expires_at = now() - timedelta(seconds=1)
        db.commit()
    assert client.post("/api/v1/team/invitations", json={"email": "replacement@example.com"}).status_code == 201
    member = next(m for m in client.get("/api/v1/team").json()["items"] if m["email"] == "replacement@example.com")
    assert client.delete("/api/v1/team/" + member["id"]).status_code == 200
    assert client.get("/api/v1/team").json()["occupied"] == 4


def test_invitation_and_viewer_permissions(platform):
    client, factory = platform
    register(client)
    task(client)
    result = client.post("/api/v1/team/invitations", json={"email": "viewer@example.com", "role": "viewer"}).json()
    with TestClient(app) as viewer:
        response = viewer.post("/api/v1/auth/accept-invitation", json={"token": result["accept_token"], "name": "Наблюдатель", "password": "CorrectPassword123!"})
        assert response.status_code == 200, response.text
        viewer.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert viewer.get("/api/v1/tasks").json()["items"] == []
        assert viewer.post("/api/v1/tasks", json={"direction": "purchase", "title": "Попытка", "quantity": "1", "region": "Москва"}).status_code == 403
        assert viewer.put("/api/v1/subscription/period", json={"period": "year"}).status_code == 403
    assert client.post("/api/v1/auth/accept-invitation", json={"token": result["accept_token"], "name": "Другой", "password": "CorrectPassword123!"}).status_code == 410


def test_approval_invalidates_after_draft_or_requirements_change(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    cp = company(client, current)
    payload = {"counterparty_id": cp["id"], "recipient": "sales@example.com", "subject": "Запрос цены", "body": "Просим предоставить цену и срок поставки", "basis": "Запрос закупки"}
    outreach = client.post(f"/api/v1/tasks/{current['id']}/outreach", json=payload).json()
    approval = client.post(f"/api/v1/outreach/{outreach['id']}/request-approval").json()
    assert client.post(f"/api/v1/approvals/{approval['id']}/decide", json={"decision": "approved"}).status_code == 200
    assert client.post(f"/api/v1/outreach/{outreach['id']}/send").status_code == 503
    edited = client.put(f"/api/v1/outreach/{outreach['id']}", json={**payload, "subject": "Новая тема", "expected_version": 1})
    assert edited.status_code == 200
    assert client.post(f"/api/v1/outreach/{outreach['id']}/send").status_code == 409
    assert client.get("/api/v1/approvals?status=approved").json()["items"] == []
    pending = client.post(f"/api/v1/outreach/{outreach['id']}/request-approval").json()
    change = client.put(f"/api/v1/tasks/{current['id']}/requirements", json={"text": "Новые требования, другой объём закупки", "expected_version": current["requirements_version"], "approve": True})
    assert change.status_code == 200
    assert client.post(f"/api/v1/approvals/{pending['id']}/decide", json={"decision": "approved"}).status_code == 409
    assert client.put(f"/api/v1/tasks/{current['id']}/requirements", json={"text": "Старая версия требований", "expected_version": current["requirements_version"]}).status_code == 409


def test_quotes_unknown_cost_versions_and_selection(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    cp = company(client, current)
    endpoint = f"/api/v1/tasks/{current['id']}/quotes"
    payload = {"counterparty_id": cp["id"], "requirements_version": current["requirements_version"], "amount": "100.00", "delivery_days": 3, "tax": "Налоги включены согласно КП", "terms": "Предоплата 30%", "assessment": "matches", "evidence": [{"requirement": "Сталь С245", "confirmed": True, "source": "КП 123", "locator": "стр. 1", "excerpt": "Сталь С245"}]}
    assert client.post(endpoint, json=payload).status_code == 422
    first = client.post(endpoint, json={**payload, "assessment": "needs_clarification"}).json()
    assert first["full_cost"] is None
    assert first["suitable"] is False
    revised_response = client.post(endpoint, json={**payload, "previous_id": first["id"], "delivery_included": False, "delivery_cost": "10.25"})
    assert revised_response.status_code == 201, revised_response.text
    revised = revised_response.json()
    assert revised["full_cost"] == "110.25"
    assert revised["version"] == 2
    assert len(client.get(endpoint).json()["items"]) == 1
    assert client.get("/api/v1/tasks/" + current["id"]).json()["suitable"] == 1
    assert client.post(endpoint + "/" + revised["id"] + "/select", json={}).json()["order_placed"] is False
    assert client.post(endpoint, json={**payload, "delivery_included": True}).status_code == 409


def test_quota_guard_and_no_fake_ai_or_payment(platform):
    client, factory = platform
    registered = register(client)
    current = approve_requirements(client, task(client))
    result = client.post(f"/api/v1/tasks/{current['id']}/jobs", json={"kind": "analyze"})
    assert result.status_code == 201 and result.json()["status"] == "blocked"
    sub = client.get("/api/v1/subscription").json()
    assert next(x for x in sub["resources"] if x["resource"] == "search_queries")["used"] == 0
    assert client.post("/api/v1/subscription/checkout").status_code == 503
    ends = sub["ends_at"]
    assert client.put("/api/v1/subscription/period", json={"period": "year"}).json()["access_extended"] is False
    assert client.get("/api/v1/subscription").json()["ends_at"] == ends
    with factory() as db:
        usage = db.scalar(select(Usage).where(Usage.resource == "tasks"))
        usage.used = 100
        db.commit()
    result = client.post("/api/v1/tasks", json={"direction": "purchase", "title": "Лимит", "quantity": "1", "region": "Москва"})
    assert result.status_code == 429


def test_document_worker_dedup_and_failed_parser(platform):
    client, factory = platform
    register(client)
    content = "Продукт;Количество\nТруба;12".encode("utf-8")
    response = client.post("/api/v1/documents", files={"file": ("data.csv", content, "text/csv")})
    assert response.status_code == 201, response.text
    doc = response.json()
    assert process_one(factory)
    detail = client.get("/api/v1/documents/" + doc["id"]).json()
    assert detail["status"] == "extracted"
    assert detail["extracted"]["fragments"][1]["locator"]["row"] == 2
    assert client.get("/api/v1/documents/" + doc["id"] + "/download").content == content
    duplicate = client.post("/api/v1/documents", files={"file": ("copy.csv", content, "text/csv")})
    assert duplicate.json()["duplicate"] is True
    invalid = client.post("/api/v1/documents", files={"file": ("invalid.pdf", b"not a pdf", "application/pdf")}).json()
    assert process_one(factory)
    assert client.get("/api/v1/documents/" + invalid["id"]).json()["status"] == "error"
    usage = next(x for x in client.get("/api/v1/subscription").json()["resources"] if x["resource"] == "documents")
    assert usage["used"] == 1 and usage["reserved"] == 0
    assert client.post("/api/v1/documents", files={"file": ("x.exe", b"test", "application/octet-stream")}).status_code == 415


def test_cross_origin_and_csv_formula_injection(platform):
    client, factory = platform
    register(client)
    assert client.post("/api/v1/auth/logout", headers={"Origin": "https://untrusted.example"}).status_code == 403
    task(client, title="=DANGEROUS()")
    exported = client.get("/api/v1/reports/export.csv")
    assert exported.status_code == 200
    assert "'=DANGEROUS()" in exported.text


def test_monthly_anchors_and_yearly_monthly_quotas():
    jan = datetime(2026, 1, 31, 10, 0)
    assert add_months(jan, 1).date().isoformat() == "2026-02-28"
    assert add_months(jan, 2).date().isoformat() == "2026-03-31"
    assert add_months(datetime(2024, 2, 29), 12).date().isoformat() == "2025-02-28"
    sub = Subscription(quota_anchor=jan, period="year")
    start, end = quota_period(sub, datetime(2026, 3, 10))
    assert start.date().isoformat() == "2026-02-28" and end.date().isoformat() == "2026-03-31"

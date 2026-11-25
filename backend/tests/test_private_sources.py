from fastapi.testclient import TestClient
from backend.app.main import app
from .conftest import register, task, approve_requirements, company


def test_private_documents_and_duplicate_message_cannot_be_linked_by_employee(platform):
    owner, factory = platform
    register(owner)
    document = owner.post("/api/v1/documents", files={"file": ("private.csv", b"private;data", "text/csv")}).json()
    mail = {"external_id": "private-message", "sender": "sales@example.com", "recipient": "owner@example.com", "subject": "Приватное письмо", "body": "Документ владельца"}
    assert owner.post("/api/v1/messages/import", json=mail).status_code == 201
    invitation = owner.post("/api/v1/team/invitations", json={"email": "employee@example.com"}).json()
    with TestClient(app) as employee:
        auth = employee.post("/api/v1/auth/accept-invitation", json={"token": invitation["accept_token"], "name": "Сотрудник", "password": "CorrectPassword123!"}).json()
        employee.headers["X-CSRF-Token"] = auth["csrf_token"]
        current = approve_requirements(employee, task(employee))
        cp = company(employee, current)
        assert employee.get("/api/v1/documents").json()["items"] == []
        assert employee.get("/api/v1/documents/" + document["id"]).status_code == 404
        draft = {"counterparty_id": cp["id"], "recipient": "sales@example.com", "subject": "Запрос", "body": "Запрос условий", "basis": "Деловой запрос", "attachment_ids": [document["id"]]}
        assert employee.post(f"/api/v1/tasks/{current['id']}/outreach", json=draft).status_code == 404
        quote = {"counterparty_id": cp["id"], "document_id": document["id"], "requirements_version": current["requirements_version"], "amount": "100"}
        assert employee.post(f"/api/v1/tasks/{current['id']}/quotes", json=quote).status_code == 404
        assert employee.post("/api/v1/messages/import", json={**mail, "campaign_id": current["id"]}).status_code == 404

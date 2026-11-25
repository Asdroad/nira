from .conftest import register, task, approve_requirements, company


def test_closed_task_cannot_reopen_through_approval_or_requirements(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    cp = company(client, current)
    draft = client.post(f"/api/v1/tasks/{current['id']}/outreach", json={"counterparty_id": cp["id"], "recipient": "sales@example.com", "subject": "Запрос", "body": "Просим предоставить КП", "basis": "Контрагент предлагает поставку продукции"}).json()
    approval = client.post(f"/api/v1/outreach/{draft['id']}/request-approval").json()
    assert client.patch(f"/api/v1/tasks/{current['id']}/state", json={"status": "closed"}).status_code == 200
    assert client.post(f"/api/v1/approvals/{approval['id']}/decide", json={"decision": "approved"}).status_code == 409
    assert client.put(f"/api/v1/tasks/{current['id']}/requirements", json={"text": "Изменённые требования к продукции", "expected_version": current["requirements_version"], "approve": True}).status_code == 409
    assert client.post(f"/api/v1/tasks/{current['id']}/jobs", json={"kind": "search"}).status_code == 409
    assert client.get(f"/api/v1/tasks/{current['id']}").json()["status"] == "closed"
    assert client.get("/api/v1/approvals").json()["items"] == []

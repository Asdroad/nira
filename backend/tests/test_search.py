import httpx
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.search_adapters import run_search
from backend.app.worker import process_one
from .conftest import register, task, approve_requirements


def test_public_adapters_and_restricted_data():
    def handler(request):
        if request.url.host == "html.duckduckgo.com":
            assert request.url.params["q"] == "труба поставщик"
            return httpx.Response(200, text='<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsupplier.example%2F">Труба со склада</a><a class="result__snippet">Поставка труб</a></div>')
        assert request.url.host == "api.onlc.ru"
        assert request.url.params["filters[status]"] == "1"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": 123, "name": "Поставка трубы", "date": "2026-11-01T09:00:00+03:00", "startPrice": 0,
            "owner": {"name": "Hidden company"}, "highlighting": {"lots": ["<found>Труба</found> 40х40"]}}], "meta": {"total": 5}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_search("труба поставщик", ["web", "onlinecontract", "online_tender", "tender_online"], client=client)
    assert len(result["items"]) == 2
    assert result["items"][0]["url"] == "https://supplier.example/"
    tender = result["items"][1]
    assert tender["buyer"] is None and tender["amount"] is None
    assert tender["description"] == "Труба 40х40"
    assert result["sources"][1]["total"] == 5
    assert result["successful_requests"] == 2
    assert [s["status"] for s in result["sources"]] == ["ok", "ok", "unavailable", "unavailable"]


def test_partial_failure_and_challenge_are_not_empty_success():
    def handler(request):
        return httpx.Response(200, text='<form id="challenge-form" action="anomaly.js"></form>') if request.url.host.startswith("html.") else httpx.Response(403)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_search("труба", ["web", "onlinecontract"], client=client)
    assert result["successful_requests"] == 0
    assert all(s["status"] == "error" for s in result["sources"])


def queue(client, current, **fields):
    result = client.post(f"/api/v1/tasks/{current['id']}/jobs", json={"kind": "search", "query": "труба", "sources": ["onlinecontract"], **fields})
    assert result.status_code == 201, result.text
    return result.json()


def test_search_worker_persistence_quota_and_tenant_isolation(platform, monkeypatch):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    job = queue(client, current)
    assert client.post(f"/api/v1/tasks/{current['id']}/jobs", json={"kind": "search"}).status_code == 409
    sub = client.get("/api/v1/subscription").json()
    assert next(u for u in sub["resources"] if u["resource"] == "search_queries")["reserved"] == 1
    monkeypatch.setattr("backend.app.search_worker.run_search", lambda *args: {"items": [{"title": "Реальный источник", "url": "https://onlinecontract.ru/tenders/123"}], "sources": [], "successful_requests": 1})
    assert process_one(factory)
    detail = client.get("/api/v1/jobs/" + job["id"]).json()
    assert detail["status"] == "completed" and len(detail["result"]["items"]) == 1
    assert client.get(f"/api/v1/tasks/{current['id']}/jobs").json()["items"][0]["id"] == job["id"]
    assert client.get(f"/api/v1/tasks/{current['id']}").json()["found"] == 0
    usage = next(u for u in client.get("/api/v1/subscription").json()["resources"] if u["resource"] == "search_queries")
    assert usage["used"] == 1 and usage["reserved"] == 0
    assert process_one(factory) is False
    with TestClient(app) as other:
        register(other, "other@example.com", "Другая организация")
        assert other.get("/api/v1/jobs/" + job["id"]).status_code == 404
        assert other.get(f"/api/v1/tasks/{current['id']}/jobs").status_code == 404


def test_changed_requirements_cancel_before_network_and_refund(platform, monkeypatch):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    job = queue(client, current)
    monkeypatch.setattr("backend.app.search_worker.run_search", lambda *args: (_ for _ in ()).throw(AssertionError("Network must not be called")))
    client.put(f"/api/v1/tasks/{current['id']}/requirements", json={"text": "Новые требования", "expected_version": current["requirements_version"], "approve": True})
    assert process_one(factory)
    assert client.get("/api/v1/jobs/" + job["id"]).json()["status"] == "cancelled"
    usage = next(u for u in client.get("/api/v1/subscription").json()["resources"] if u["resource"] == "search_queries")
    assert usage["used"] == 0 and usage["reserved"] == 0


def test_failed_search_refunds_quota(platform, monkeypatch):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    job = queue(client, current)
    monkeypatch.setattr("backend.app.search_worker.run_search", lambda *args: {"items": [], "sources": [{"status": "error"}], "successful_requests": 0})
    assert process_one(factory)
    assert client.get("/api/v1/jobs/" + job["id"]).json()["status"] == "failed"
    usage = next(u for u in client.get("/api/v1/subscription").json()["resources"] if u["resource"] == "search_queries")
    assert usage["used"] == 0 and usage["reserved"] == 0

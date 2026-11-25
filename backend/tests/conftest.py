import pytest
from pydantic import SecretStr
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from backend.app.db import build_engine, Base, get_db
from backend.app.main import app, attempts
from backend.app.config import get_settings


@pytest.fixture
def platform(tmp_path, monkeypatch):
    engine = build_engine("sqlite:///" + (tmp_path / "test.db").as_posix())
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "storage_path", tmp_path / "files")
    monkeypatch.setattr(get_settings(), "openrouter_api_key", SecretStr(""))
    monkeypatch.setattr(get_settings(), "ai_auto_run", False)
    def override():
        with factory() as db:
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
    app.dependency_overrides[get_db] = override
    attempts.clear()
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
    engine.dispose()


def register(client, email="owner@example.com", name="Организация А"):
    response = client.post("/api/v1/auth/register", json={"email": email, "password": "CorrectPassword123!", "name": "Владелец", "organization": name, "industries": ["Металлопрокат"]})
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


def task(client, **fields):
    response = client.post("/api/v1/tasks", json={"direction": "purchase", "title": "Профильная труба", "quantity": "12 тонн", "region": "Москва", "requirements": "Сталь С245, доставка на склад", **fields})
    assert response.status_code == 201, response.text
    return response.json()


def approve_requirements(client, current):
    response = client.put(f"/api/v1/tasks/{current['id']}/requirements", json={"text": "Сталь С245, доставка на склад", "expected_version": current["requirements_version"], "approve": True})
    assert response.status_code == 200, response.text
    return response.json()


def company(client, current):
    response = client.post("/api/v1/counterparties", json={"name": "Поставщик", "email": "sales@example.com", "source": "Контакт добавлен сотрудником"})
    assert response.status_code == 201, response.text
    obj = response.json()
    response = client.post(f"/api/v1/tasks/{current['id']}/counterparties", json={"counterparty_id": obj["id"]})
    assert response.status_code == 200, response.text
    return obj

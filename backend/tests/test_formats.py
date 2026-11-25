from io import BytesIO
import pytest
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter
from backend.app.worker import process_one
from .conftest import register


@pytest.mark.parametrize("extension", ["docx", "xlsx", "pdf"])
def test_real_formats_and_source_coordinates(platform, extension):
    client, factory = platform
    register(client)
    buffer = BytesIO()
    if extension == "docx":
        document = Document()
        document.add_paragraph("Цена трубы с НДС: 100 рублей")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "Доставка включена"
        document.save(buffer)
    elif extension == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Предложение"
        sheet["A1"], sheet["B2"] = "Цена", "=100+20"
        workbook.save(buffer)
    else:
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.write(buffer)
    result = client.post("/api/v1/documents", files={"file": ("offer." + extension, buffer.getvalue(), "application/octet-stream")})
    assert result.status_code == 201, result.text
    assert process_one(factory)
    detail = client.get("/api/v1/documents/" + result.json()["id"]).json()
    if extension == "docx":
        assert detail["status"] == "extracted"
        assert detail["extracted"]["fragments"][0]["locator"] == {"paragraph": 1}
        assert detail["extracted"]["fragments"][1]["locator"] == {"table": 1, "row": 1}
    elif extension == "xlsx":
        assert detail["status"] == "extracted"
        formula = next(x for x in detail["extracted"]["fragments"] if x["locator"]["cell"] == "B2")
        assert formula["locator"]["sheet"] == "Предложение"
        assert formula["text"] == "=100+20"
        assert detail["extracted"]["warnings"]
    else:
        assert detail["status"] == "needs_ocr"
        assert detail["extracted"]["pages"] == 1
        assert detail["extracted"]["ocr_required"] is True


def test_password_spaces_are_significant(platform):
    client, factory = platform
    password = " StrongPassword123! "
    response = client.post("/api/v1/auth/register", json={"email": "spaces@example.com", "password": password, "name": "Владелец", "organization": "Организация"})
    assert response.status_code == 201
    assert client.post("/api/v1/auth/login", json={"email": "spaces@example.com", "password": password.strip()}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "spaces@example.com", "password": password}).status_code == 200

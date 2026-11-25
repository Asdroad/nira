from io import BytesIO
from pathlib import Path
import pytest
from PIL import Image, ImageDraw, ImageFont
from backend.app.local_ocr import ocr_status
from backend.app.worker import process_one
from .conftest import register


@pytest.mark.skipif(not ocr_status()["available"], reason="Local Tesseract rus+eng is not installed")
def test_russian_scan_through_upload_worker_and_api(platform, monkeypatch):
    client, factory = platform
    register(client)
    image = Image.new("RGB", (1800, 2400), "white")
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    if not font_path.exists():
        pytest.skip("Russian test font is not installed")
    font = ImageFont.truetype(str(font_path), 58)
    ImageDraw.Draw(image).multiline_text((160, 220), "Коммерческое предложение\nТруба стальная 40х40\nЦена 125000 рублей\nДоставка включена", font=font, fill="black", spacing=50)
    buffer = BytesIO()
    image.save(buffer, format="PDF", resolution=300)
    monkeypatch.setenv("NIRA_OCR_ENABLED", "false")
    result = client.post("/api/v1/documents", files={"file": ("scan.pdf", buffer.getvalue(), "application/pdf")})
    assert result.status_code == 201, result.text
    assert process_one(factory)
    detail = client.get("/api/v1/documents/" + result.json()["id"]).json()
    assert detail["status"] == "needs_ocr"
    monkeypatch.setenv("NIRA_OCR_ENABLED", "true")
    retry = client.post("/api/v1/documents/" + result.json()["id"] + "/reprocess", json={})
    assert retry.status_code == 201
    assert client.post("/api/v1/documents/" + result.json()["id"] + "/reprocess", json={}).status_code == 409
    assert process_one(factory)
    detail = client.get("/api/v1/documents/" + result.json()["id"]).json()
    assert detail["status"] == "extracted", detail
    fragments = detail["extracted"]["fragments"]
    text = " ".join(f["text"] for f in fragments).lower()
    assert "коммерческое" in text and "125000" in text and "доставка" in text
    assert all(f["method"] == "local_ocr" and f["locator"]["page"] == 1 for f in fragments)
    assert all(len(f["locator"]["bbox"]) == 4 for f in fragments)
    resources = client.get("/api/v1/subscription").json()["resources"]
    assert next(u for u in resources if u["resource"] == "documents")["used"] == 1
    assert next(u for u in resources if u["resource"] == "document_pages")["used"] == 1

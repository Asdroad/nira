"""Local CPU OCR with Tesseract. Documents never leave this computer."""
import csv
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from .config import ROOT, get_settings


def tesseract_executable():
    configured = get_settings().tesseract_path
    candidates = [configured, shutil.which("tesseract"), str(ROOT / ".tools/Tesseract-OCR/tesseract.exe"),
                  str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tesseract-OCR/tesseract.exe")]
    return next((str(Path(p)) for p in candidates if p and Path(p).is_file()), None)


def ocr_status():
    executable = tesseract_executable()
    if not get_settings().ocr_enabled or not executable:
        return {"available": False, "message": "Локальный OCR выключен или Tesseract не установлен"}
    try:
        result = subprocess.run([executable, "--list-langs"], capture_output=True, timeout=5)
        languages = result.stdout.decode("utf-8", errors="replace").splitlines()
        available = all(lang in languages for lang in get_settings().ocr_languages.split("+"))
        return {"available": available, "languages": get_settings().ocr_languages,
                "message": "OCR работает на этом компьютере" if available else "Не установлены языки OCR: " + get_settings().ocr_languages}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "message": "Tesseract не отвечает"}


def recognize_page(pdf_page, page_number, timeout=None):
    settings = get_settings()
    executable = tesseract_executable()
    if not settings.ocr_enabled or not executable:
        raise ValueError("Установите локальный OCR через scripts/setup-ocr.ps1")
    width, height = pdf_page.get_size()
    if width <= 0 or height <= 0:
        raise ValueError("Некорректный размер страницы")
    scale = min(300 / 72, (25_000_000 / (width * height)) ** 0.5)
    bitmap = pdf_page.render(scale=scale)
    try:
        with tempfile.TemporaryDirectory(prefix="nira-ocr-") as directory:
            image_path = Path(directory) / "page.png"
            bitmap.to_pil().save(image_path)
            env = {**os.environ, "OMP_THREAD_LIMIT": "2"}
            result = subprocess.run([executable, str(image_path), "stdout", "-l", settings.ocr_languages, "--psm", "3", "tsv"],
                                    capture_output=True, timeout=timeout or settings.ocr_page_timeout, env=env)
            if result.returncode:
                raise ValueError("Не удалось распознать страницу; проверьте установку Tesseract и языков")
    finally:
        bitmap.close()
    lines = {}
    for row in csv.DictReader(io.StringIO(result.stdout.decode("utf-8", errors="replace")), delimiter="\t"):
        text = row.get("text", "").strip()
        if not text or row.get("level") != "5":
            continue
        key = (row["block_num"], row["par_num"], row["line_num"])
        x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
        line = lines.setdefault(key, {"words": [], "bbox": [x, y, x + w, y + h], "confidence": []})
        line["words"].append(text)
        line["confidence"].append(float(row["conf"]))
        box = line["bbox"]
        line["bbox"] = [min(box[0], x), min(box[1], y), max(box[2], x + w), max(box[3], y + h)]
    return [{"locator": {"page": page_number, "line": n, "bbox": [round(v / scale, 2) for v in line["bbox"]], "bbox_unit": "pt_from_top_left"},
             "text": " ".join(line["words"]), "method": "local_ocr", "confidence": round(sum(line["confidence"]) / len(line["confidence"]), 1)}
            for n, line in enumerate(lines.values(), 1)]

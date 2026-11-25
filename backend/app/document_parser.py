"""Local file parsing in a separate, time-limited worker subprocess."""
import csv
import io
import json
import sys
import time
from pathlib import Path
from zipfile import ZipFile, BadZipFile

MAX_TEXT = 250000
MAX_CELLS = 50000
MAX_UNPACKED = 100 * 1024 * 1024


def inspect_archive(path, required):
    with ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > MAX_UNPACKED:
            raise ValueError("Превышен допустимый объём содержимого архива")
        if required not in archive.namelist():
            raise ValueError("Содержимое не соответствует формату документа")
        if any("vbaproject" in x.filename.lower() for x in entries):
            raise ValueError("Документы с макросами не поддерживаются")
        if any(x.file_size > 2 * 1024 * 1024 and x.file_size / max(1, x.compress_size) > 500 for x in entries):
            raise ValueError("Подозрительная степень сжатия документа")


def parse(path: Path, extension: str):
    fragments, page_count, warnings, ocr_required = [], 0, [], False
    if extension == "pdf":
        from pypdf import PdfReader
        if path.read_bytes()[:5] != b"%PDF-":
            raise ValueError("Содержимое файла не является PDF")
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("PDF защищён паролем")
        page_count = len(reader.pages)
        if page_count > 2000:
            raise ValueError("Не более 2000 страниц PDF")
        from .config import get_settings
        from .local_ocr import recognize_page
        raster = None
        ocr_pages = 0
        ocr_deadline = time.monotonic() + max(5, get_settings().document_timeout - 25)
        try:
            for n, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                if text.strip():
                    fragments.append({"locator": {"page": n}, "text": text})
                    continue
                if ocr_pages >= get_settings().ocr_max_pages:
                    ocr_required = True
                    warnings.append(f"Страница {n}: достигнут лимит {get_settings().ocr_max_pages} страниц OCR на документ")
                    continue
                ocr_pages += 1
                try:
                    remaining = ocr_deadline - time.monotonic()
                    if remaining < 3:
                        raise ValueError("Достигнут лимит времени OCR. Разделите документ на меньшие части")
                    if raster is None:
                        import pypdfium2 as pdfium
                        raster = pdfium.PdfDocument(str(path))
                    raster_page = raster[n - 1]
                    try:
                        recognized = recognize_page(raster_page, n, timeout=min(get_settings().ocr_page_timeout, remaining))
                    finally:
                        raster_page.close()
                    if not recognized:
                        raise ValueError("Текст не найден: страница пустая или скан требует проверки")
                    fragments.extend(recognized)
                except Exception as error:
                    ocr_required = True
                    warnings.append(f"Страница {n}: {str(error)[:300]}")
            if ocr_pages:
                warnings.append("Текст сканов распознан локально. Проверьте суммы, реквизиты и структуру таблиц по оригиналу.")
        finally:
            if raster is not None:
                raster.close()
    elif extension == "docx":
        from docx import Document
        inspect_archive(path, "word/document.xml")
        document = Document(path)
        for n, p in enumerate(document.paragraphs, 1):
            if p.text:
                fragments.append({"locator": {"paragraph": n}, "text": p.text})
        for n, table in enumerate(document.tables, 1):
            for row_n, row in enumerate(table.rows, 1):
                fragments.append({"locator": {"table": n, "row": row_n}, "text": " | ".join(c.text for c in row.cells)})
        page_count = 0
    elif extension == "xlsx":
        from openpyxl import load_workbook
        inspect_archive(path, "xl/workbook.xml")
        # Stored originals use opaque names, so openpyxl must receive a stream.
        workbook = load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=False, keep_links=False)
        cells = 0
        try:
            for sheet in workbook:
                if sheet.max_row and sheet.max_column and sheet.max_row * sheet.max_column > 200000:
                    raise ValueError("Слишком большая область листа Excel")
                for row in sheet:
                    for cell in row:
                        if cell.value is not None:
                            cells += 1
                            if cells > MAX_CELLS:
                                raise ValueError("Не более 50 000 непустых ячеек Excel")
                            text = str(cell.value)
                            if cell.data_type == "f":
                                warnings.append(f"{sheet.title}!{cell.coordinate}: формула сохранена без выполнения")
                            fragments.append({"locator": {"sheet": sheet.title, "cell": cell.coordinate}, "text": text})
        finally:
            workbook.close()
    elif extension == "csv":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1251")
            warnings.append("Использована кодировка Windows-1251")
        if "\x00" in text:
            raise ValueError("CSV содержит бинарные данные")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=";,\t,")
        except csv.Error:
            dialect = csv.excel
        cells = 0
        for n, row in enumerate(csv.reader(io.StringIO(text), dialect), 1):
            cells += len(row)
            if cells > MAX_CELLS:
                raise ValueError("Не более 50 000 ячеек CSV")
            fragments.append({"locator": {"row": n}, "text": " | ".join(row)})
    else:
        raise ValueError("Формат не поддерживается")
    length, result = 0, []
    for fragment in fragments:
        remaining = MAX_TEXT - length
        if remaining <= 0:
            warnings.append("Предпросмотр ограничен 250 000 символами; оригинал сохранён")
            break
        fragment["text"] = fragment["text"][:remaining]
        length += len(fragment["text"])
        result.append(fragment)
    return {"fragments": result, "pages": page_count, "warnings": warnings[:100], "ocr_required": ocr_required}


if __name__ == "__main__":
    try:
        output = {"ok": True, "data": parse(Path(sys.argv[1]), sys.argv[2])}
    except Exception as error:
        output = {"ok": False, "error": str(error)[:1000]}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))

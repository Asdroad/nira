"""Read-only pilot adapters. Public interfaces can change without notice."""
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit
import httpx
from lxml import html

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}
SOURCES = {
    "web": {"name": "Интернет · DuckDuckGo", "url": "https://html.duckduckgo.com/html/", "mode": "public_html"},
    "onlinecontract": {"name": "ONLINECONTRACT", "url": "https://onlinecontract.ru/tenders", "mode": "public_json"},
    "online_tender": {"name": "ТЕНДЕР — ОНЛАЙН", "url": "https://online-tender.ru/", "mode": "service_site", "message": "Сервис тендерного сопровождения. Открытый каталог закупок и API поиска не обнаружены при проверке 11.10.2026."},
    "tender_online": {"name": "ТЕНДЕР.онлайн", "url": "https://www.xn--d1abb4arh.xn--80asehdb/", "mode": "private_platform", "message": "Платформа проведения собственных закупок. На открытом сайте каталога и документации API поиска нет; доступ обсуждается через демо."},
}


def clean(value, limit=3000):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()[:limit]


def public_link(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme in {"http", "https"} and host and not parsed.username and host.lower() not in {"localhost", "127.0.0.1", "::1"}


def read_response(client, url, params):
    # Do not follow redirects to arbitrary hosts or download unbounded responses.
    with client.stream("GET", url, params=params) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 3 * 1024 * 1024:
                raise ValueError("Источник вернул слишком большой ответ")
            chunks.append(chunk)
        response._content = b"".join(chunks)
        return response


def search_web(client, query, limit):
    response = read_response(client, SOURCES["web"]["url"], {"q": query, "kl": "ru-ru"})
    tree = html.fromstring(response.text)
    if tree.xpath('//form[contains(@action,"anomaly")]') or "challenge-form" in response.text:
        raise ValueError("Поисковик запросил проверку посетителя. Попробуйте позже или подключите поисковый API.")
    results, seen = [], set()
    for block in tree.xpath('//div[contains(concat(" ",normalize-space(@class)," ")," result ")]'):
        links = block.xpath('.//a[contains(@class,"result__a")]')
        if not links:
            continue
        href = urljoin(str(response.url), links[0].get("href", ""))
        parsed = urlsplit(href)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            href = parse_qs(parsed.query).get("uddg", [href])[0]
        if not public_link(href) or urlsplit(href).hostname.endswith("duckduckgo.com") or href in seen:
            continue
        snippets = block.xpath('.//*[contains(@class,"result__snippet")]')
        seen.add(href)
        results.append({"source": "web", "kind": "web_page", "title": clean(links[0].text_content(), 500), "url": href,
                        "description": clean(snippets[0].text_content()) if snippets else "", "verification": "search_snippet"})
        if len(results) >= limit:
            break
    if not results and not tree.xpath('//*[contains(@class,"no-results")]') and "No results" not in response.text:
        raise ValueError("Не удалось распознать выдачу поисковика; отсутствие результатов не подтверждено")
    return results, None


def search_onlinecontract(client, query, limit, active_only):
    params = {"total": "true", "limit": limit, "offset": 0, "sort": "-startAt", "filters[search]": query, "filters[searchType]": 2}
    if active_only:
        params["filters[status]"] = 1
    response = read_response(client, "https://api.onlc.ru/purchases/v1/public/procedures", params)
    payload = response.json()
    if not isinstance(payload.get("data"), list):
        raise ValueError("Формат публичного интерфейса площадки изменился")
    results = []
    seen = set()
    for row in payload["data"][:limit]:
        identifier = str(row.get("id", ""))
        if not identifier.isdigit() or identifier in seen:
            continue
        seen.add(identifier)
        highlights = row.get("highlighting") or {}
        price = row.get("startPrice")
        results.append({"source": "onlinecontract", "kind": "tender", "external_id": identifier,
                        "title": clean(row.get("name"), 1000), "url": f"https://onlinecontract.ru/tenders/{identifier}",
                        "description": clean(" · ".join(highlights.get("lots") or [])), "deadline": row.get("date"),
                        "amount": price if isinstance(price, (int, float)) and price > 0 else None,
                        "currency": clean(row.get("currency"), 100), "status": clean(row.get("status"), 100),
                        # The public UI gates buyer identities. Do not exploit API/UI discrepancies.
                        "buyer": None, "verification": "public_listing", "active_filter": active_only})
    return results, (payload.get("meta") or {}).get("total")


def run_search(query, sources, limit=10, active_only=True, client=None):
    own_client = client is None
    client = client or httpx.Client(headers=HEADERS, timeout=httpx.Timeout(25, connect=10), follow_redirects=False)
    checked = datetime.now(timezone.utc).isoformat()
    items, reports = [], []
    successful_requests = 0
    try:
        for identifier in sources:
            source = SOURCES[identifier]
            report = {"id": identifier, "name": source["name"], "url": source["url"], "checked_at": checked, "count": 0}
            if identifier in {"online_tender", "tender_online"}:
                reports.append({**report, "status": "unavailable", "message": source["message"]})
                continue
            try:
                found, total = search_web(client, query, limit) if identifier == "web" else search_onlinecontract(client, query, limit, active_only)
                for item in found:
                    item["fetched_at"] = checked
                items.extend(found)
                successful_requests += 1
                reports.append({**report, "status": "ok", "count": len(found), "total": total,
                                "message": "Фрагменты поисковой выдачи; сведения нужно проверить на сайте." if identifier == "web" else "Публичный интерфейс без ключа. Заказчик скрыт; участие и часть данных требуют регистрации."})
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                message = str(exc) if not isinstance(exc, httpx.HTTPError) else ("Источник отказал в доступе" if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403} else "Источник временно недоступен; повторите позже")
                reports.append({**report, "status": "error", "message": message[:500]})
    finally:
        if own_client:
            client.close()
    return {"query": query, "active_only": active_only, "items": items, "sources": reports,
            "successful_requests": successful_requests, "checked_at": checked,
            "note": "Результаты поиска не являются проверенными контрагентами и не учитываются в показателе «Найдено». Регион и соответствие требованиям нужно проверить отдельно."}

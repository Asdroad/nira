"""Small public-site reader, with connections pinned to validated public IPs."""
import ipaddress
import re
import socket
import ssl
import codecs
from urllib.parse import urljoin, urlsplit
import httpcore
import httpx
from httpcore._backends.sync import SyncBackend
from lxml import html, etree
from .search_adapters import HEADERS, clean


class PublicBackend(SyncBackend):
    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host = host.decode() if isinstance(host, bytes) else host
        if port not in {80, 443}:
            raise httpcore.ConnectError("Only public HTTP ports are allowed")
        try:
            addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except OSError:
            raise httpcore.ConnectError("Public site DNS unavailable") from None
        if not addresses or any(not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast for address in addresses):
            raise httpcore.ConnectError("Non-public addresses are blocked")
        # Connect to the checked IP, keeping the original hostname for TLS SNI.
        return super().connect_tcp(addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options)


class PublicTransport(httpx.HTTPTransport):
    def __init__(self):
        super().__init__(trust_env=False)
        self._pool.close()
        self._pool = httpcore.ConnectionPool(ssl_context=ssl.create_default_context(), network_backend=PublicBackend(), max_connections=5)


def allowed_url(url):
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and parsed.port in {None, 80, 443})
    except ValueError:
        return False


def parse_page(data, content_type):
    declared = re.search(r'charset\s*=\s*["\x27]?([A-Za-z0-9._-]+)', content_type, re.I)
    if not declared:
        declared = re.search(r'<meta\b[^>]*charset\s*=\s*["\x27]?([A-Za-z0-9._-]+)', data[:8192].decode('ascii', errors='ignore'), re.I)
    encoding = declared.group(1) if declared else None
    if not encoding:
        try:
            data.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError as exc:
            encoding = 'utf-8' if exc.reason == 'unexpected end of data' and exc.end == len(data) else 'windows-1251'
    try:
        codecs.lookup(encoding)
        return html.fromstring(data, parser=html.HTMLParser(encoding=encoding))
    except (LookupError, etree.ParserError):
        raise ValueError('Page could not be decoded or parsed') from None


def read_site(client, url, budget):
    for _ in range(3):
        if not allowed_url(url) or budget[0] <= 0:
            raise ValueError("Site is outside public reading limits")
        budget[0] -= 1
        with client.stream("GET", url) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("location", ""))
                continue
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "").lower():
                raise ValueError("Not an HTML page")
            data, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 256000:
                    break
                data.append(chunk)
            return parse_page(b"".join(data), response.headers.get('content-type', '')), str(response.url)
    raise ValueError("Too many site redirects")


def discover_contacts(items, page_budget=10, client=None):
    own = client is None
    client = client or httpx.Client(transport=PublicTransport(), headers=HEADERS, timeout=httpx.Timeout(8, connect=4), trust_env=False)
    budget, records, domains = [page_budget], [], set()
    try:
        for index, item in enumerate(items):
            if budget[0] <= 0:
                break
            if item.get("source") != "web":
                continue
            host = (urlsplit(item.get("url", "")).hostname or "").lower()
            if not host or host in domains or len(domains) >= 5:
                continue
            domains.add(host)
            record = {"source_id": str(index), "url": item["url"], "pages": [], "emails": [], "status": "unavailable"}
            try:
                tree, source_url = read_site(client, item["url"], budget)
                contact_links = [urljoin(source_url, a.get("href", "")) for a in tree.xpath('//a[@href]')
                    if re.search(r"контакт|contacts?", clean(a.text_content()) + " " + a.get("href", ""), re.I)
                    and urlsplit(urljoin(source_url, a.get("href", ""))).hostname == urlsplit(source_url).hostname]
                pages = [(tree, source_url)]
                if contact_links and budget[0] > 0:
                    try:
                        pages.append(read_site(client, contact_links[0], budget))
                    except (httpx.HTTPError, ValueError):
                        pass
                for page, page_url in pages:
                    for node in page.xpath('//script|//style|//noscript'):
                        node.drop_tree()
                    text = clean(page.text_content(), 6000)
                    emails = set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text))
                    for href in page.xpath('//a[starts-with(@href,"mailto:")]/@href'):
                        address = href[7:].split('?')[0].strip()
                        if re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address):
                            emails.add(address)
                    valid = sorted(email.lower() for email in emails if len(email) <= 254)[:10]
                    record["pages"].append({"url": page_url, "text": text, "emails": valid})
                    record["emails"] += valid
                record["emails"] = list(dict.fromkeys(record["emails"]))
                record["status"] = "read"
            except (httpx.HTTPError, ValueError, OSError):
                pass
            records.append(record)
    finally:
        if own:
            client.close()
    return records, page_budget - budget[0]

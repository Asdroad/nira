import httpcore
import httpx
import pytest
from backend.app.web_contacts import PublicBackend, discover_contacts


def test_private_ip_cannot_be_connected(monkeypatch):
    for address in ['127.0.0.1', '10.1.2.3', '169.254.169.254', '::1', '224.0.0.1']:
        monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kw: [(2, 1, 6, '', (address, 443))])
        with pytest.raises(httpcore.ConnectError):
            PublicBackend().connect_tcp('hidden.example', 443)


def test_contact_reading_obeys_page_budget_and_records_source():
    requested = []
    def respond(request):
        requested.append(str(request.url))
        body = '<html><body>ООО Компания Трубы <a href="/contacts">Контакты</a></body></html>' if request.url.path == '/' else '<html><body>ООО Компания Трубы <a href="mailto:sales@example.com">Напишите нам</a></body></html>'
        return httpx.Response(200, text=body, headers={'content-type': 'text/html; charset=utf-8'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        records, count = discover_contacts([{'source': 'web', 'url': 'https://example.com/'}], 2, client)
    assert count == 2 and len(requested) == 2
    assert records[0]['emails'] == ['sales@example.com']
    assert records[0]['pages'][1]['url'] == 'https://example.com/contacts'
    assert 'ООО Компания Трубы' in records[0]['pages'][1]['text']


@pytest.mark.parametrize('encoding,content_type,meta', [
    ('utf-8', 'text/html', ''),
    ('cp1251', 'text/html; charset=windows-1251', ''),
    ('cp1251', 'text/html', '<meta charset="windows-1251">')])
def test_russian_site_text_preserves_company_and_activity(encoding, content_type, meta):
    body = f'<html><head>{meta}</head><body>Компания Трубы поставляет стальные трубы. sales@example.com</body></html>'.encode(encoding)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={'content-type':content_type}))) as client:
        records, count = discover_contacts([{'source':'web','url':'https://example.com/'}], 1, client)
    assert count == 1
    assert 'Компания Трубы поставляет стальные трубы.' in records[0]['pages'][0]['text']
    assert records[0]['emails'] == ['sales@example.com']


@pytest.mark.parametrize('body,content_type', [(b'', 'text/html'), (b'test', 'text/html; charset=unsupported-codec')])
def test_unreadable_page_does_not_abort_other_sites(body, content_type):
    def respond(request):
        if request.url.host == 'bad.example':
            return httpx.Response(200, content=body, headers={'content-type':content_type})
        return httpx.Response(200, text='<p>Компания Трубы: sales@example.com</p>', headers={'content-type':'text/html; charset=utf-8'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        records,count=discover_contacts([{'source':'web','url':'https://bad.example/'},{'source':'web','url':'https://good.example/'}],2,client)
    assert count == 2
    assert records[0]['status'] == 'unavailable'
    assert records[1]['status'] == 'read' and records[1]['emails'] == ['sales@example.com']


def test_redirect_to_private_address_is_blocked_by_public_backend(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda *args, **kw: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(httpcore.ConnectError):
        PublicBackend().connect_tcp('rebound.example', 443)

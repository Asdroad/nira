import copy
import io
import json
import httpx
import pytest
from pydantic import SecretStr
from docx import Document as Word
from sqlalchemy import select
from backend.app.config import get_settings
from backend.app.models import Campaign, Document, Job, Membership, Usage, Outreach, Counterparty
from backend.app.ai_provider import analyze_context, AIError
from backend.app.ai_service import context_for, validate_report, prepare_outreach
from backend.app.ai_worker import process_ai_one
from backend.app.worker import process_document_one
from .conftest import register, task, approve_requirements


def enable(monkeypatch, auto=False):
    monkeypatch.setattr(get_settings(), "openrouter_api_key", SecretStr("test-provider-key"))
    monkeypatch.setattr(get_settings(), "ai_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_auto_run", auto)


def upload_offers(client, current, factory):
    a = Word()
    a.add_paragraph("Компания А. Сталь С245, 12 тонн. Итоговая цена 125000 RUB, НДС включён. Доставка на склад в Москве включена, 5 дней. Оплата после поставки.")
    output = io.BytesIO(); a.save(output)
    bodies = [("A.docx", output.getvalue()), ("B.csv", "Компания Б;Сталь С235;12 тонн;Итого 100000 RUB;НДС включён;Доставка не указана;Оплата авансом".encode('utf-8'))]
    for filename, body in bodies:
        response = client.post('/api/v1/documents', data={'campaign_id': current['id']}, files={'file': (filename, body)})
        assert response.status_code == 201, response.text
    assert process_document_one(factory)
    assert process_document_one(factory)


def model_answer(context):
    criteria = [{'id': 'steel', 'requirement': 'Сталь С245', 'required': True, 'task_excerpt': 'Сталь С245'},
                {'id': 'quantity', 'requirement': '12 тонн', 'required': True, 'task_excerpt': '12 тонн'},
                {'id': 'region', 'requirement': 'Москва', 'required': True, 'task_excerpt': 'Москва'},
                {'id': 'delivery', 'requirement': 'доставка на склад', 'required': True, 'task_excerpt': 'доставка на склад'}]
    offers = []
    for index, d in enumerate(context['documents']):
        fragment = d['fragments'][0]
        evidence = [{'fragment_id': fragment['fragment_id'], 'excerpt': fragment['text']}]
        def fact(value):
            return {'value': value, 'evidence': copy.deepcopy(evidence) if value is not None else []}
        is_a = d['filename'].startswith('A')
        offers.append({'document_id': d['document_id'], 'role': 'offer', 'supplier': fact('Компания А' if is_a else 'Компания Б'),
            'product': fact('Сталь С245' if is_a else 'Сталь С235'), 'quantity': fact('12 тонн'), 'price': fact('125000 RUB' if is_a else '100000 RUB'),
            'tax': fact('НДС включён'), 'delivery': fact('Доставка на склад включена, 5 дней' if is_a else None), 'payment': fact('Оплата после поставки' if is_a else 'Оплата авансом'), 'validity': fact(None),
            'total_amount': 125000 if is_a else 100000, 'currency': 'RUB', 'price_basis': 'total', 'tax_included': True, 'tax_amount': None,
            'delivery_included': True if is_a else None, 'delivery_cost': None, 'delivery_days': 5 if is_a else None, 'valid_until': None,
            'checks': [{'criterion_id': 'steel', 'status': 'matches' if is_a else 'does_not_match', 'explanation': 'Марка стали подтверждена' if is_a else 'Предложена С235 вместо С245', 'evidence': copy.deepcopy(evidence)},
                       {'criterion_id': 'quantity', 'status': 'matches', 'explanation': '12 тонн подтверждены', 'evidence': copy.deepcopy(evidence)},
                       {'criterion_id': 'region', 'status': 'matches' if is_a else 'unknown', 'explanation': 'Москва подтверждена' if is_a else 'Регион доставки не указан', 'evidence': copy.deepcopy(evidence) if is_a else []},
                       {'criterion_id': 'delivery', 'status': 'matches' if is_a else 'unknown', 'explanation': 'Доставка на склад включена' if is_a else 'Доставка не указана', 'evidence': copy.deepcopy(evidence) if is_a else []}],
            'strengths': ['Поставка на склад'] if is_a else [], 'risks': [] if is_a else ['Доставка неизвестна'], 'clarification_questions': [] if is_a else ['Сколько стоит доставка?']})
    prepared = {'title': 'Запрос КП на сталь С245', 'body': 'Просим предложить 12 тонн стали С245 с доставкой на склад.', 'missing_information': ['Адрес склада']}
    return {'summary': 'А соответствует, Б дешевле, но сталь не соответствует.', 'criteria': criteria, 'offers': offers,
        'ranking': [{'document_id': o['document_id'], 'reason': 'А предпочтительнее Б: нужная сталь и известная доставка'} for o in offers],
        'recommended_document_id': offers[0]['document_id'] if offers else None, 'recommendation_reason': 'Компания А: сталь С245 и доставка подтверждены. Б предлагает другую марку стали.',
        'announcement': prepared, 'email': prepared, 'search_queries': ['сталь С245 поставщики Москва'], 'leads': [], 'questions': ['Укажите адрес склада']}


def test_missing_key_honest_status_no_paid_call(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    queued = client.post(f"/api/v1/tasks/{current['id']}/ai").json()
    assert queued['status'] == 'blocked'
    assert client.get('/api/v1/ai/status').json()['available'] is False
    assert not process_ai_one(factory)
    with factory() as db:
        assert not db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
    assert client.get(f"/api/v1/tasks/{current['id']}/ai/export.csv").status_code == 409


def test_openrouter_request_structured_no_secret_in_result(monkeypatch):
    enable(monkeypatch)
    data = model_answer({'documents': []})
    def respond(request):
        assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-provider-key'
        body = json.loads(request.content)
        assert body['response_format']['json_schema']['strict']
        schema = body['response_format']['json_schema']['schema']
        assert 'title' in schema['$defs']['PreparedText']['properties']
        assert 'maxItems' not in schema['properties']['offers']
        assert schema['$defs']['Citation']['required'] == ['fragment_id']
        assert 'excerpt' not in schema['$defs']['Citation']['properties']
        assert body['provider']['require_parameters']
        return httpx.Response(200, json={'model': 'tested-model', 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(data)}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        report, meta = analyze_context({'trusted_task': {}}, client)
    assert report['announcement']['body']
    assert meta['completion_tokens'] == 20
    assert 'test-provider-key' not in json.dumps([report, meta])


@pytest.mark.parametrize('status', [401, 402, 429, 500])
def test_provider_errors_do_not_leak_secrets(monkeypatch, status):
    enable(monkeypatch)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, text='test-provider-key private provider detail'))) as client:
        with pytest.raises(AIError) as error:
            analyze_context({}, client)
    assert 'test-provider-key' not in str(error.value)


def test_different_document_formats_auto_analysis_and_exports(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch, auto=True)
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', lambda context: (model_answer(context), {'provider': 'test', 'model': 'fixture'}))
    assert process_ai_one(factory)
    data = client.get(f"/api/v1/tasks/{current['id']}/ai").json()
    assert data['job']['status'] == 'completed' and not data['stale']
    report = data['report']
    a, b = report['offers']
    assert a['full_cost'] == '125000' and a['eligible']
    assert b['full_cost'] is None and b['assessment'] == 'does_not_match' and not b['eligible']
    assert report['recommended_document_id'] == a['document_id']
    assert [r['document_id'] for r in report['ranking']] == [a['document_id']]
    assert a['price']['evidence'][0]['locator']
    assert data['dispatch']['email']['status'] == 'not_configured'
    task_view = client.get('/api/v1/tasks/' + current['id']).json()
    assert task_view['received'] == 2 and task_view['suitable'] == 1
    csv = client.get(f"/api/v1/tasks/{current['id']}/ai/export.csv")
    assert csv.status_code == 200 and 'Компания А' in csv.text and 'Компания Б' in csv.text
    md = client.get(f"/api/v1/tasks/{current['id']}/ai/export.md")
    assert md.status_code == 200 and 'С235 вместо С245' in md.text and 'A.docx' in md.text
    assert len(client.get(f"/api/v1/tasks/{current['id']}/jobs").json()['items']) == 4  # 2 extraction + AI + automatic search
    with factory() as db:
        usage = db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
        assert usage.used == 1 and usage.reserved == 0
    register(client, 'other@example.com', 'Другая организация')
    assert client.get(f"/api/v1/tasks/{current['id']}/ai").status_code == 404
    assert client.get(f"/api/v1/tasks/{current['id']}/ai/export.md").status_code == 404


def test_fake_evidence_and_unconfirmed_numbers_cannot_win(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    with factory() as db:
        context, refs, _ = context_for(db, db.get(Campaign, current['id']))
    result = model_answer(context)
    result['offers'][0]['price']['evidence'][0]['excerpt'] = 'Дешёвый победитель 1 рубль'
    result['offers'][0]['total_amount'] = 1
    checked = validate_report(result, context, refs)
    assert checked['offers'][0]['price']['value'] is None
    assert checked['recommended_document_id'] is None and not checked['ranking']
    result = model_answer(context)
    result['offers'][0]['total_amount'] = 1
    assert validate_report(result, context, refs)['offers'][0]['full_cost'] is None


def test_task_region_cannot_be_omitted_from_comparison(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    with factory() as db:
        context, refs, _ = context_for(db, db.get(Campaign, current['id']))
    assert any(c['id'] == 'task_region' and c['task_excerpt'] == 'Москва' for c in context['required_task_fields'])
    assert context['workflow']['role'] == 'ЗАКАЗЧИК'
    result = model_answer(context)
    result['criteria'] = [c for c in result['criteria'] if c['id'] != 'region']
    for offer in result['offers']:
        offer['checks'] = [c for c in offer['checks'] if c['criterion_id'] != 'region']
    checked = validate_report(result, context, refs)
    assert not checked['offers'][0]['eligible']
    assert checked['recommended_document_id'] is None
    assert any('Регион доставки' in warning for warning in checked['warnings'])


def test_unit_price_is_not_total_and_incomplete_documents_not_ranked(platform):
    client, factory = platform
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    with factory() as db:
        context, refs, _ = context_for(db, db.get(Campaign, current['id']))
    result = model_answer(context)
    result['offers'][0]['price_basis'] = 'unit'
    assert validate_report(result, context, refs)['recommended_document_id'] is None
    result = model_answer(context)
    context['documents'][0]['complete'] = False
    assert validate_report(result, context, refs)['recommended_document_id'] is None


def test_requirements_change_during_call_cancels_and_requeues(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch, auto=True)
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    def answer(context):
        response = client.put(f"/api/v1/tasks/{current['id']}/requirements", json={'text': 'Сталь С245, доставка на склад, только предоплата', 'expected_version': 1, 'approve': True})
        assert response.status_code == 200
        return model_answer(context), {}
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', answer)
    assert process_ai_one(factory)
    jobs = client.get(f"/api/v1/tasks/{current['id']}/jobs").json()['items']
    ai = [j for j in jobs if j['kind'] == 'analyze']
    assert ai[0]['status'] == 'queued' and ai[0]['payload']['requirements_version'] == 2
    assert ai[1]['status'] == 'cancelled'
    assert client.get(f"/api/v1/tasks/{current['id']}/ai/export.csv").status_code == 409


def test_revoked_author_does_not_call_provider(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch)
    register(client)
    current = approve_requirements(client, task(client))
    client.post(f"/api/v1/tasks/{current['id']}/ai")
    def forbidden(context):
        raise AssertionError('Must not call model')
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', forbidden)
    with factory() as db:
        db.scalar(select(Membership)).status = 'revoked'
        db.commit()
    assert process_ai_one(factory)
    with factory() as db:
        assert db.scalar(select(Job).where(Job.kind == 'analyze')).status == 'cancelled'
        usage = db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
        assert usage.used == 0 and usage.reserved == 0


def test_sourced_contacts_create_drafts_once_and_ignore_fake_email(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch)
    register(client)
    current = approve_requirements(client, task(client))
    with factory() as db:
        t = db.get(Campaign, current['id'])
        db.add(Job(organization_id=t.organization_id, campaign_id=t.id, created_by=t.created_by, kind='search', status='completed', payload={'requirements_version': 1}, result={
            'items': [{'title': 'Компания Трубы', 'url': 'https://example.com/', 'source': 'web'}],
            'contacts': [{'source_id': '0', 'pages': [{'url': 'https://example.com/contacts', 'text': 'ООО Компания Трубы поставляет сталь С245', 'emails': ['sales@example.com']}]}]}))
        db.commit()
    def answer(context):
        data = model_answer(context)
        data['leads'] = [{'source_id': '0', 'relevance': 'potential', 'entity_type': 'supplier', 'activity_excerpt': 'поставляет сталь С245', 'reason': 'Предлагает сталь', 'company_name': 'Компания Трубы', 'company_excerpt': 'ООО Компания Трубы', 'email': 'sales@example.com'}]
        return data, {}
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', answer)
    for attempt in range(2):
        assert client.post(f"/api/v1/tasks/{current['id']}/ai").status_code == 201
        assert process_ai_one(factory) is (attempt == 0)  # Unchanged input reuses the report.
    with factory() as db:
        draft = db.scalars(select(Outreach)).all()
        assert len(draft) == 1 and draft[0].status == 'draft' and draft[0].accepted_at is None
        assert draft[0].recipient == 'sales@example.com'
        assert len(db.scalars(select(Counterparty)).all()) == 1
    data = client.get(f"/api/v1/tasks/{current['id']}/ai").json()
    assert len(data['outreach']) == 1
    with factory() as db:
        ctx, refs, _ = context_for(db, db.get(Campaign, current['id']))
    fake = answer(ctx)[0]
    fake['leads'][0]['email'] = 'made-up@example.com'
    assert validate_report(fake, ctx, refs)['leads'][0]['email'] is None


@pytest.mark.parametrize('entity_type,activity', [('marketplace', 'поставляет сталь С245'), ('customer', 'поставляет сталь С245'), ('unknown', None), ('supplier', 'несуществующая цитата')])
def test_wrong_contact_role_or_fabricated_activity_never_creates_draft(platform, monkeypatch, entity_type, activity):
    client, factory = platform
    enable(monkeypatch)
    register(client)
    current = approve_requirements(client, task(client))
    job = client.post(f"/api/v1/tasks/{current['id']}/ai").json()
    with factory() as db:
        campaign = db.get(Campaign, current['id'])
        context, refs, _ = context_for(db, campaign)
        context['leads'] = [{'source_id': '0', 'url': 'https://example.com', 'title': 'Компания Трубы',
            'site_pages': [{'url': 'https://example.com/contacts', 'text': 'ООО Компания Трубы поставляет сталь С245', 'emails': ['sales@example.com']}]}]
        report = model_answer(context)
        report['leads'] = [{'source_id': '0', 'relevance': 'potential', 'entity_type': entity_type, 'activity_excerpt': activity,
            'reason': 'Кандидат', 'company_name': 'Компания Трубы', 'company_excerpt': 'ООО Компания Трубы', 'email': 'sales@example.com'}]
        checked = validate_report(report, context, refs)
        assert not checked['leads'][0]['outreach_eligible']
        assert prepare_outreach(db, campaign, db.get(Job, job['id']), checked) == []
        assert not db.scalar(select(Outreach))
        assert not db.scalar(select(Counterparty))


def test_new_document_marks_report_stale_and_blocks_export(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch)
    register(client)
    current = approve_requirements(client, task(client))
    upload_offers(client, current, factory)
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', lambda context: (model_answer(context), {}))
    client.post(f"/api/v1/tasks/{current['id']}/ai")
    assert process_ai_one(factory)
    response = client.post('/api/v1/documents', data={'campaign_id': current['id']}, files={'file': ('new.csv', b'new proposal')})
    assert response.status_code == 201
    assert client.get(f"/api/v1/tasks/{current['id']}/ai").json()['stale']
    assert client.get(f"/api/v1/tasks/{current['id']}/ai/export.csv").status_code == 409

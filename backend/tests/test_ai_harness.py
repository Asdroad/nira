import copy
import json
import httpx
import pytest
from sqlalchemy import select
from backend.app.config import get_settings
from backend.app.models import Campaign, Job, Usage
from backend.app.ai_provider import AIError, analyze_context, request_plan
from backend.app.ai_harness import execution_key, task_costs
from backend.app.ai_service import queue_analysis, citation_pieces
from backend.app.ai_worker import process_ai_one
from .conftest import register, task, approve_requirements
from .test_ai import enable, model_answer, upload_offers


def setup_task(platform, monkeypatch):
    client, factory = platform
    enable(monkeypatch)
    register(client)
    current = approve_requirements(client, task(client))
    return client, factory, current


def add_search(factory, current, text='Компания поставляет сталь С245', title='Компания'):
    with factory() as db:
        t = db.get(Campaign, current['id'])
        job = Job(organization_id=t.organization_id, campaign_id=t.id, created_by=t.created_by,
            kind='search', status='completed', payload={'requirements_version': t.requirements_version},
            result={'items': [{'title': title, 'url': 'https://example.com/', 'description': text}],
                    'contacts': [{'source_id': '0', 'pages': [{'url': 'https://example.com/', 'text': text, 'emails': []}]}]})
        db.add(job); db.commit()
        return job.id


def lead_answer(context):
    return {'leads': [{'source_id': l['source_id'], 'relevance': 'needs_check', 'entity_type': 'unknown',
        'activity_excerpt': None, 'reason': 'Нужна проверка', 'company_name': None, 'company_excerpt': None, 'email': None}
        for l in context['leads']]}


def test_manual_duplicate_free_and_model_change_invalidates(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    calls = []
    def answer(ctx):
        calls.append(copy.deepcopy(ctx))
        return model_answer(ctx), {'cost_usd': 0.001}
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', answer)
    path = f"/api/v1/tasks/{current['id']}/ai"
    first = client.post(path).json()
    assert process_ai_one(factory)
    second = client.post(path).json()
    assert second['id'] == first['id'] and second['status'] == 'completed'
    assert not process_ai_one(factory) and len(calls) == 1
    with factory() as db:
        used = db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
        assert used.used == 1 and used.reserved == 0
    monkeypatch.setattr(get_settings(), 'ai_model', 'qwen/qwen3.8-omni-flash')
    assert client.post(path).json()['id'] != first['id']
    assert process_ai_one(factory) and len(calls) == 2


def test_search_only_delta_reuses_cp_and_never_sends_documents(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    upload_offers(client, current, factory)
    calls = []
    def answer(ctx):
        calls.append(copy.deepcopy(ctx))
        return (lead_answer(ctx) if ctx.get('stage') == 'leads' else model_answer(ctx)), {'cost_usd': 0.001}
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', answer)
    path = f"/api/v1/tasks/{current['id']}/ai"
    client.post(path); assert process_ai_one(factory)
    original = client.get(path).json()['report']
    search_id = add_search(factory, current)
    client.post(path); assert process_ai_one(factory)
    assert calls[-1]['stage'] == 'leads' and 'documents' not in calls[-1]
    updated = client.get(path).json()['report']
    assert updated['offers'] == original['offers']
    assert updated['announcement'] == original['announcement']
    assert updated['recommended_document_id'] == original['recommended_document_id']
    assert updated['metadata']['base_report_id']
    with factory() as db:
        search = db.get(Job, search_id)
        search.result = {**search.result, 'non_model_debug': 'new retrieval timestamp'}
        db.commit()
    client.post(path); assert process_ai_one(factory)
    assert len(calls) == 2  # Changed search metadata, identical lead content: zero calls.
    assert client.get(path).json()['report']['metadata']['cost_usd'] == 0
    add_search(factory, current, text='Компания теперь поставляет другой товар')
    client.post(path); assert process_ai_one(factory)
    assert len(calls) == 3 and len(calls[-1]['leads']) == 1


def test_changed_requirements_and_other_task_do_not_reuse_base(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    calls = []
    def answer(ctx):
        calls.append(ctx)
        return model_answer(ctx), {'cost_usd': 0.001}
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', answer)
    path = f"/api/v1/tasks/{current['id']}/ai"
    client.post(path); assert process_ai_one(factory)
    response = client.put(f"/api/v1/tasks/{current['id']}/requirements", json={
        'text': 'Сталь С245, доставка на склад. Дополнительная проверка', 'expected_version': 1, 'approve': True})
    assert response.status_code == 200
    client.post(path); assert process_ai_one(factory)
    other = approve_requirements(client, task(client))
    client.post(f"/api/v1/tasks/{other['id']}/ai"); assert process_ai_one(factory)
    assert len(calls) == 3 and all('stage' not in c for c in calls)


def test_budget_preflight_zero_network(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(get_settings(), 'ai_request_budget_usd', 0.00001)
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        with pytest.raises(AIError, match='лимит расходов'):
            analyze_context({'trusted_task': {}}, client)
    assert not calls


def test_task_budget_blocks_before_call_and_reserves_unknown_cost(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    calls = []
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', lambda ctx: calls.append(ctx))
    with factory() as db:
        t = db.get(Campaign, current['id'])
        db.add(Job(organization_id=t.organization_id, campaign_id=t.id, created_by=t.created_by,
            kind='analyze', status='failed', payload={'execution_key': execution_key()}, result={'metadata': {'reserved_cost_usd': 0.0999}}))
        db.commit()
    path = f"/api/v1/tasks/{current['id']}/ai"
    client.post(path); assert process_ai_one(factory)
    data = client.get(path).json()
    assert data['job']['status'] == 'blocked' and not calls
    assert data['costs']['reserved_or_unknown_usd'] == 0.0999
    with factory() as db:
        usage = db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
        assert usage.used == usage.reserved == 0


def test_truncated_paid_response_keeps_cost_and_no_retry(monkeypatch):
    enable(monkeypatch)
    requests = []
    def respond(req):
        requests.append(req)
        body = json.loads(req.content)
        assert body['model'] == 'openai/gpt-6-luna'
        assert body['reasoning']['effort'] == 'none' and 'temperature' not in body
        assert body['provider']['max_price'] == {'prompt': 0.1, 'completion': 0.5, 'request': 0}
        assert body['max_tokens'] == 2400
        return httpx.Response(200, json={'id': 'gen-test', 'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}],
            'usage': {'cost': 0.0012, 'prompt_tokens': 100, 'completion_tokens': 2400}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(AIError) as caught:
            analyze_context({}, client)
    assert caught.value.charged and caught.value.metadata['cost_usd'] == 0.0012
    assert caught.value.metadata['generation_id'] == 'gen-test' and len(requests) == 1


def test_402_not_automatically_repeated_and_no_usage_charge(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    calls = []
    def fail(ctx):
        calls.append(ctx)
        raise AIError('На OpenRouter недостаточно средств', {'http_status': 402, 'cost_usd': 0})
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', fail)
    path = f"/api/v1/tasks/{current['id']}/ai"
    client.post(path); assert process_ai_one(factory)
    with factory() as db:
        t = db.get(Campaign, current['id'])
        assert queue_analysis(db, t, t.created_by, automatic=True) is None
        usage = db.scalar(select(Usage).where(Usage.resource == 'ai_runs'))
        assert usage.used == usage.reserved == 0
        assert task_costs(db, t)['cost_usd'] == 0
    assert len(calls) == 1


def test_lead_stage_missing_result_fails_and_preserves_paid_cost(platform, monkeypatch):
    client, factory, current = setup_task(platform, monkeypatch)
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', lambda ctx: (model_answer(ctx), {'cost_usd': 0.001}))
    path = f"/api/v1/tasks/{current['id']}/ai"
    client.post(path); assert process_ai_one(factory)
    add_search(factory, current)
    monkeypatch.setattr('backend.app.ai_worker.analyze_context', lambda ctx: ({'leads': []}, {'cost_usd': 0.0005}))
    client.post(path); assert process_ai_one(factory)
    data = client.get(path).json()
    assert data['job']['status'] == 'failed' and data['stale']
    assert data['job']['result']['metadata']['cost_usd'] == 0.0005
    assert data['costs']['cost_usd'] == 0.0015


@pytest.mark.parametrize('fake_id', [False, True])
def test_provider_copies_source_text_and_rejects_unknown_ids(monkeypatch, fake_id):
    enable(monkeypatch)
    source = 'Сталь С245. Итого 125000 RUB. Доставка на склад в Москве включена.'
    context = {'documents': [{'document_id': 'doc', 'filename': 'A.docx', 'fragments': [
        {'fragment_id': 'doc:0', 'text': source}]}]}
    data = model_answer(context)
    def wire(value):
        if isinstance(value, dict):
            if 'fragment_id' in value:
                return {'fragment_id': 'made-up-id' if fake_id else value['fragment_id']}
            return {k: wire(v) for k,v in value.items()}
        return [wire(v) for v in value] if isinstance(value, list) else value
    response = {'id': 'gen-source', 'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(wire(data))}}], 'usage': {'cost': 0.001}}
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as client:
        if fake_id:
            with pytest.raises(AIError) as error:
                analyze_context(context, client)
            assert error.value.charged and error.value.metadata['cost_usd'] == 0.001
        else:
            report, _ = analyze_context(context, client)
            assert report['offers'][0]['product']['evidence'][0]['excerpt'] == source


def test_long_fragments_remain_quoteable_and_keep_boundary_amount():
    text = 'Описание. ' * 159 + '125 000 RUB с доставкой. ' + 'Продолжение. ' * 190
    parts = list(citation_pieces(text))
    assert len(parts) > 1 and all(len(p) <= 1600 and p in text for p in parts)
    assert any('125 000 RUB' in p for p in parts)

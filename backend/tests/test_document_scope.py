from pathlib import Path
from alembic.config import Config
from alembic import command
from sqlalchemy.orm import Session
from backend.app.db import build_engine
from backend.app.models import Organization, User, Document
from .conftest import register, task


def test_same_offer_upload_attaches_independently_to_each_task(platform):
    client, _ = platform
    register(client)
    a, b = task(client), task(client, direction='sales')
    body = 'Компания А;Сталь С245;125000 RUB'.encode('utf-8')
    def upload(current):
        r = client.post('/api/v1/documents', data={'campaign_id': current['id']}, files={'file': ('offer.csv', body)})
        assert r.status_code == 201, r.text
        return r.json()
    original, reused = upload(a), upload(b)
    assert original['id'] != reused['id']
    assert reused['campaign_id'] == b['id']
    assert client.get('/api/v1/documents/'+original['id']).json()['campaign_id'] == a['id']
    assert upload(b)['duplicate']
    assert len(client.get('/api/v1/documents', params={'campaign_id':b['id']}).json()['items']) == 1
    for d in [original, reused]:
        assert client.get('/api/v1/documents/'+d['id']+'/download').content == body


def test_sqlite_document_migration_preserves_originals_and_incoming_references(tmp_path):
    engine = build_engine('sqlite:///'+(tmp_path/'migration.db').as_posix())
    cfg = Config(str(Path(__file__).resolve().parents[1]/'alembic.ini'))
    with engine.connect() as connection:
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, '30a12721f7c0')
    with Session(engine) as db:
        org = Organization(name='Migration test')
        user = User(email='migration@example.com',name='User',password_hash='unused')
        db.add_all([org,user]); db.flush()
        doc = Document(organization_id=org.id, uploaded_by=user.id,filename='original.csv',extension='csv',size=10,sha256='a'*64,blob='original/blob')
        db.add(doc); db.commit()
        identifier=doc.id
    with engine.connect() as connection:
        connection.exec_driver_sql('CREATE TABLE document_refs (document_id TEXT REFERENCES documents(id))')
        connection.exec_driver_sql('INSERT INTO document_refs VALUES (?)', (identifier,))
        connection.commit()
        cfg.attributes['connection'] = connection
        command.upgrade(cfg, 'head')
        assert connection.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
        assert not connection.exec_driver_sql('PRAGMA foreign_key_check').all()
        assert connection.exec_driver_sql('SELECT document_id FROM document_refs').scalar() == identifier
        assert connection.exec_driver_sql('SELECT filename, blob, sha256 FROM documents WHERE id=?',(identifier,)).one() == ('original.csv','original/blob','a'*64)
    engine.dispose()

import pytest
from app.app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_index(client):
    rv = client.get('/')
    assert rv.status_code == 200
    assert b'Service Release' in rv.data

def test_health(client):
    rv = client.get('/api/health')
    assert rv.status_code == 200
    assert rv.json['status'] == 'Healthy'

def test_rollouts(client):
    rv = client.get('/api/rollouts')
    assert rv.status_code == 200
    assert isinstance(rv.json, list)

def test_workflow(client):
    rv = client.get('/api/workflow')
    assert rv.status_code == 200
    assert rv.json[0]['stage'] == 'S1_SPEC'

def test_dreams(client):
    rv = client.get('/api/dreams')
    assert rv.status_code == 200
    assert rv.json['id'] == 'DP-001'

def test_receipts(client):
    rv = client.get('/api/receipts')
    assert rv.status_code == 200
    assert 'signature' in rv.json[0]

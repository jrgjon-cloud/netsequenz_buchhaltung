import pytest
from app import create_app, db
from app.config import TestConfig

@pytest.fixture
def app():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

def test_healthcheck_ok(client):
    response = client.get('/health')
    assert response.status_code == 200
    json_data = response.get_json()
    assert json_data["status"] == "ok"
    assert json_data["gobd_integrity"] is True

def test_healthcheck_error(client, app):
    # Simulate DB error by dropping table
    with app.app_context():
        db.drop_all()

    response = client.get('/health')
    assert response.status_code == 500
    json_data = response.get_json()
    assert json_data["status"] == "error"

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

from app.models import Transaction, TransactionStatus, SourceSystem, TransactionDirection
import datetime
from decimal import Decimal

def test_update_transaction_save_and_lock(client, app):
    with app.app_context():
        # Create a pending transaction
        tx = Transaction(
            source_system=SourceSystem.manual,
            external_id='MANUAL-TEST-01',
            direction=TransactionDirection.expense,
            status=TransactionStatus.review_pending,
            invoice_date=datetime.date(2023, 10, 26),
            counterparty='Initial Vendor',
            description='Initial Description',
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19'
        )
        db.session.add(tx)
        db.session.commit()
        tx_id = tx.id

    # Update via POST request simulating save_and_lock
    response = client.post(f'/transactions/{tx_id}/update', data={
        'counterparty': 'Updated Vendor',
        'description': 'Updated Description',
        'invoice_date': '2023-10-27',
        'payment_date': '2023-10-28',
        'amount_net': '200.00',
        'tax_rate': '19.00',
        'amount_tax': '38.00',
        'amount_gross': '238.00',
        'tax_case': 'standard_19',
        'euer_line': 'Bürobedarf',
        'save_and_lock': '1'
    })

    assert response.status_code == 302 # Redirects to dashboard

    with app.app_context():
        updated_tx = Transaction.query.get(tx_id)
        assert updated_tx.status == TransactionStatus.locked
        assert updated_tx.counterparty == 'Updated Vendor'
        assert updated_tx.amount_net == Decimal('200.00')
        assert updated_tx.euer_line == 'Bürobedarf'
        assert updated_tx.payment_date == datetime.date(2023, 10, 28)
        assert updated_tx.record_hash is not None # Hash should be generated

def test_update_transaction_save_only(client, app):
    with app.app_context():
        tx = Transaction(
            source_system=SourceSystem.manual,
            external_id='MANUAL-TEST-02',
            direction=TransactionDirection.expense,
            status=TransactionStatus.review_pending,
            invoice_date=datetime.date(2023, 10, 26),
            counterparty='Initial Vendor',
            description='Initial Description',
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19'
        )
        db.session.add(tx)
        db.session.commit()
        tx_id = tx.id

    # Update via POST request (no save_and_lock)
    response = client.post(f'/transactions/{tx_id}/update', data={
        'counterparty': 'Saved Vendor',
        'description': 'Saved Description',
        'invoice_date': '2023-10-27',
        'amount_net': '150.00',
        'tax_rate': '7.00',
        'amount_tax': '10.50',
        'amount_gross': '160.50',
        'tax_case': 'reduced_7',
    })

    assert response.status_code == 302

    with app.app_context():
        updated_tx = Transaction.query.get(tx_id)
        assert updated_tx.status == TransactionStatus.review_pending # Stays pending
        assert updated_tx.counterparty == 'Saved Vendor'
        assert updated_tx.tax_rate == Decimal('7.00')

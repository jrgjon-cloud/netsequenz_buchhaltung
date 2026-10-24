import pytest
from unittest.mock import patch
from decimal import Decimal
from datetime import date
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from app.config import TestConfig
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus
from app.services.invoiceninja_sync import sync_from_invoiceninja, create_invoice_via_cockpit
from app.services.payment_poller import poll_payments

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

@patch('app.services.invoiceninja_sync.requests.get')
@patch('app.services.invoiceninja_sync.fetch_invoiceninja_pdf')
def test_sync_from_invoiceninja(mock_fetch_pdf, mock_get, app):
    mock_fetch_pdf.return_value = b'fake_pdf_content'

    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {
        "data": [
            {
                "status_id": "2",
                "number": "INV-001",
                "amount": "119.00",
                "balance": "0.00",
                "date": "2024-01-01",
                "is_deleted": False,
                "line_items": [
                    {
                        "tax_rate1": "19.00",
                        "notes": "Test Item"
                    }
                ],
                "payments": [
                    {
                        "date": "2024-01-05"
                    }
                ],
                "client": {
                    "name": "Test Client"
                }
            }
        ]
    }

    with app.app_context():
        results = sync_from_invoiceninja(base_url="http://test", api_token="test")

        assert results["created"] == 1
        assert results["errors"] == []

        tx = Transaction.query.filter_by(external_id="INV-001").first()
        assert tx is not None
        assert tx.amount_gross == Decimal('119.00')
        assert tx.tax_rate == Decimal('19.00')
        assert tx.tax_case == 'standard_19'
        assert tx.payment_date == date(2024, 1, 5)
        assert tx.document_sha256 is not None

@patch('app.services.invoiceninja_sync.requests.post')
@patch('app.services.invoiceninja_sync.requests.get')
@patch('app.services.invoiceninja_sync.sync_from_invoiceninja')
def test_create_invoice_via_cockpit(mock_sync, mock_get, mock_post, app):
    mock_get.return_value.status_code = 200
    mock_get.return_value.json.return_value = {"data": [{"id": "client_1"}]}

    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"data": {"number": "INV-002"}}

    with app.app_context():
        # Setup fake tx that sync_from_invoiceninja would normally create
        tx = Transaction(
            source_system=SourceSystem.invoice_ninja,
            external_id="INV-002",
            direction=TransactionDirection.income,
            description="test",
            counterparty="test",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19',
            invoice_date=date.today()
        )
        db.session.add(tx)
        db.session.commit()

        created_tx = create_invoice_via_cockpit(
            client_name="Test Client",
            client_email="test@example.com",
            subject="Test Subject",
            item_description="Test Item",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            base_url="http://test",
            api_token="test"
        )

        assert created_tx is not None
        assert created_tx.external_id == "INV-002"

@patch('app.services.payment_poller.requests.get')
def test_poll_payments(mock_get, app):
    # Mock GoCardless payments & payouts, and SumUp
    def mock_requests_get(*args, **kwargs):
        class MockResponse:
            def __init__(self, json_data, status_code=200):
                self.json_data = json_data
                self.status_code = status_code
            def json(self):
                return self.json_data

        url = args[0]
        if 'payments' in url:
            return MockResponse({
                "payments": [
                    {
                        "id": "PM001",
                        "status": "confirmed",
                        "amount": 11900,
                        "charge_date": "2024-02-01",
                        "description": "INV-001"
                    }
                ]
            })
        elif 'payouts' in url:
            return MockResponse({
                "payouts": [
                    {
                        "id": "PO001",
                        "deducted_fees": 150,
                        "created_at": "2024-02-02"
                    }
                ]
            })
        elif 'sumup' in url:
            return MockResponse({
                "items": [
                    {
                        "transaction_code": "TX001",
                        "amount": 59.50,
                        "fee_amount": 1.50,
                        "timestamp": "2024-02-03",
                        "product_summary": "INV-003"
                    }
                ]
            })
        return MockResponse({}, 404)

    mock_get.side_effect = mock_requests_get

    with app.app_context():
        # Setup open invoices
        inv1 = Transaction(
            source_system=SourceSystem.invoice_ninja,
            external_id="INV-001",
            direction=TransactionDirection.income,
            description="Test 1",
            counterparty="Client 1",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19',
            invoice_date=date(2024, 1, 1)
        )
        inv3 = Transaction(
            source_system=SourceSystem.invoice_ninja,
            external_id="INV-003",
            direction=TransactionDirection.income,
            description="Test 3",
            counterparty="Client 3",
            amount_net=Decimal('50.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('9.50'),
            amount_gross=Decimal('59.50'),
            tax_case='standard_19',
            invoice_date=date(2024, 1, 3)
        )
        db.session.add(inv1)
        db.session.add(inv3)
        db.session.commit()

        # Test poller
        app.config['GOCARDLESS_ACCESS_TOKEN'] = 'test_gc'
        app.config['SUMUP_API_KEY'] = 'test_su'
        results = poll_payments()

        assert results["matched_invoices"] == 2
        assert results["fees_created"] == 2

        # Verify inv1 payment date
        inv1 = Transaction.query.filter_by(external_id="INV-001").first()
        assert inv1.payment_date == date(2024, 2, 1)

        # Verify inv3 payment date
        inv3 = Transaction.query.filter_by(external_id="INV-003").first()
        assert inv3.payment_date == date(2024, 2, 3)

        # Verify GoCardless fee
        gc_fee = Transaction.query.filter_by(external_id="GC-FEE-PO001").first()
        assert gc_fee is not None
        assert gc_fee.amount_gross == Decimal('1.50')
        assert gc_fee.counterparty == 'GoCardless SAS'
        assert gc_fee.tax_case == 'eu_expense_13b_check'

        # Verify SumUp fee
        su_fee = Transaction.query.filter_by(external_id="SUMUP-FEE-TX001").first()
        assert su_fee is not None
        assert su_fee.amount_gross == Decimal('1.50')
        assert su_fee.counterparty == 'SumUp'

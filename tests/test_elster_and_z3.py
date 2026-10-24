import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from datetime import date, datetime
import zipfile
import io

from app import create_app, db
from app.config import TestConfig
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from app.services.gobd_ledger import lock_transaction, verify_ledger_integrity
from app.services.tax_calculator import calculate_elster_metrics
from app.services.z3_exporter import generate_gobd_z3_zip

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

def test_mark_paid_on_locked_transaction(app, client):
    with app.app_context():
        tx = Transaction(
            source_system=SourceSystem.manual,
            external_id="TX-LOCK-1",
            direction=TransactionDirection.income,
            status=TransactionStatus.review_pending,
            invoice_date=date(2025, 1, 1),
            payment_date=None,  # No payment date initially
            counterparty="Test Client",
            description="Test Invoice",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19'
        )
        db.session.add(tx)
        db.session.commit()

        lock_transaction(tx.id)

        tx_locked = db.session.get(Transaction, tx.id)
        assert tx_locked.status == TransactionStatus.locked
        assert tx_locked.payment_date is None

        # Call mark_paid route
        response = client.post(f'/transactions/{tx.id}/mark_paid', data={'payment_date': '2025-01-15'}, follow_redirects=True)
        assert response.status_code == 200

        tx_updated = db.session.get(Transaction, tx.id)
        assert tx_updated.payment_date == date(2025, 1, 15)

        # Verify ledger integrity
        assert verify_ledger_integrity() == True

        # Now try to change it again
        response2 = client.post(f'/transactions/{tx.id}/mark_paid', data={'payment_date': '2025-01-20'}, follow_redirects=True)
        # Should be blocked
        tx_final = db.session.get(Transaction, tx.id)
        assert tx_final.payment_date == date(2025, 1, 15) # did not change

@patch('app.services.paperless_sync.requests.get')
def test_gocardless_fee_deduplication(mock_get, app):
    from app.services.paperless_sync import sync_from_paperless
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        'results': [
            {
                'id': 101,
                'archive_serial_number': None,
                'content': 'Invoice from GoCardless SAS',
                'created': '2025-02-15T10:00:00Z',
                'title': 'GoCardless Fee'
            }
        ]
    }

    mock_pdf_response = MagicMock()
    mock_pdf_response.status_code = 200

    from reportlab.pdfgen import canvas
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer)
    c.drawString(100, 100, "Hello World")
    c.save()

    mock_pdf_response.content = pdf_buffer.getvalue()

    mock_get.side_effect = [mock_response, mock_pdf_response]

    with app.app_context():
        # Insert existing fee transaction
        fee_tx = Transaction(
            source_system=SourceSystem.manual,
            external_id="GC-FEE-123",
            direction=TransactionDirection.expense,
            status=TransactionStatus.review_pending,
            invoice_date=date(2025, 2, 10),
            payment_date=date(2025, 2, 10),
            counterparty="GoCardless SAS",
            description="Fee",
            amount_net=Decimal('10.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('1.90'),
            amount_gross=Decimal('11.90'), # Match amount gross... oh wait OCR might not get this.
            tax_case='standard_19'
        )
        db.session.add(fee_tx)
        db.session.commit()

        # Setup Paperless config
        app.config['PAPERLESS_URL'] = 'http://test'
        app.config['PAPERLESS_API_TOKEN'] = 'test'

        result = sync_from_paperless()
        assert result['imported'] == 1 # The existing transaction was updated and considered "imported" in terms of document link

        updated_fee = Transaction.query.filter_by(external_id="GC-FEE-123").first()
        import hashlib
        expected_hash = hashlib.sha256(pdf_buffer.getvalue()).hexdigest()
        assert updated_fee.document_sha256 == expected_hash

def test_calculate_elster_metrics(app):
    with app.app_context():
        # Standard Income Kz 81
        tx1 = Transaction(
            source_system=SourceSystem.manual,
            external_id="TX1",
            direction=TransactionDirection.income,
            status=TransactionStatus.locked,
            invoice_date=date(2025, 1, 15),
            payment_date=date(2025, 1, 20),
            counterparty="Client",
            description="Income",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19',
            ustva_kz_base='81'
        )

        # Expense Vorsteuer Kz 66
        tx2 = Transaction(
            source_system=SourceSystem.manual,
            external_id="TX2",
            direction=TransactionDirection.expense,
            status=TransactionStatus.locked,
            invoice_date=date(2025, 1, 10),
            payment_date=date(2025, 1, 12),
            counterparty="Vendor",
            description="Expense",
            amount_net=Decimal('50.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('9.50'),
            amount_gross=Decimal('59.50'),
            tax_case='standard_19',
            ustva_kz_tax='66'
        )

        # EU 13b Expense
        tx3 = Transaction(
            source_system=SourceSystem.manual,
            external_id="TX3",
            direction=TransactionDirection.expense,
            status=TransactionStatus.locked,
            invoice_date=date(2025, 1, 5),
            payment_date=date(2025, 1, 5),
            counterparty="EU Vendor",
            description="Software",
            amount_net=Decimal('200.00'),
            tax_rate=Decimal('0.00'),
            amount_tax=Decimal('0.00'),
            amount_gross=Decimal('200.00'),
            tax_case='eu_expense_13b_check',
            ustva_kz_base='46'
        )

        # 10-day rule: invoice Dec 28, paid Jan 5 -> Belongs to previous year!
        tx4 = Transaction(
            source_system=SourceSystem.manual,
            external_id="TX4",
            direction=TransactionDirection.expense,
            status=TransactionStatus.locked,
            invoice_date=date(2024, 12, 28),
            payment_date=date(2025, 1, 5),
            counterparty="Telco",
            description="Phone Bill",
            amount_net=Decimal('10.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('1.90'),
            amount_gross=Decimal('11.90'),
            tax_case='standard_19',
            ustva_kz_tax='66',
            recurring_10day_rule=True
        )

        db.session.add_all([tx1, tx2, tx3, tx4])
        db.session.commit()

        metrics = calculate_elster_metrics(2025, 'year')

        assert metrics['kz_81_base'] == Decimal('100.00')
        assert metrics['kz_81_tax'] == Decimal('19.00')
        assert metrics['kz_66_vorsteuer'] == Decimal('9.50')

        # 13b
        assert metrics['kz_46_base'] == Decimal('200.00')
        assert metrics['kz_47_tax'] == Decimal('38.00')
        assert metrics['kz_67_vorsteuer_13b'] == Decimal('38.00')

        # Zahllast = 19.00 + 38.00 - 9.50 - 38.00 = 9.50
        assert metrics['kz_83_zahllast'] == Decimal('9.50')

        # The 10-day rule transaction should not be in 2025
        metrics_2024 = calculate_elster_metrics(2024, 'year')
        assert metrics_2024['kz_66_vorsteuer'] == Decimal('1.90')

def test_gobd_z3_export(app, client):
    with app.app_context():
        tx = Transaction(
            source_system=SourceSystem.manual,
            external_id="EXPORT-1",
            direction=TransactionDirection.income,
            status=TransactionStatus.review_pending,
            invoice_date=date(2025, 5, 5),
            payment_date=date(2025, 5, 5),
            counterparty="Export Client",
            description="Export Test",
            amount_net=Decimal('100.00'),
            tax_rate=Decimal('19.00'),
            amount_tax=Decimal('19.00'),
            amount_gross=Decimal('119.00'),
            tax_case='standard_19'
        )
        db.session.add(tx)
        db.session.commit()
        lock_transaction(tx.id)

        response = client.get('/export/gobd-z3?year=2025')
        assert response.status_code == 200
        assert response.headers['Content-Disposition'] == 'attachment; filename=gobd_export_2025.zip'

        zip_data = response.data
        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            namelist = z.namelist()
            assert 'transactions_2025.csv' in namelist
            assert 'audit_log_2025.csv' in namelist
            assert 'index.xml' in namelist
            assert 'VERFAHRENSDOKUMENTATION.md' in namelist

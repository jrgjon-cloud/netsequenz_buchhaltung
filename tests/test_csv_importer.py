import pytest
import io
from decimal import Decimal
import datetime
from app import create_app, db
from app.config import TestConfig
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from app.services.csv_importer import import_papierkram_csv, parse_amount, parse_date

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

def test_parse_date():
    assert parse_date("15.10.2025") == datetime.date(2025, 10, 15)
    assert parse_date("2025-10-15") == datetime.date(2025, 10, 15)
    assert parse_date("") is None

def test_parse_amount():
    assert parse_amount("1.000,00") == Decimal('1000.00')
    assert parse_amount("1,000.00") == Decimal('1000.00')
    assert parse_amount("1234,56") == Decimal('1234.56')
    assert parse_amount("1234.56") == Decimal('1234.56')
    assert parse_amount("-1.000,00") == Decimal('-1000.00')
    assert parse_amount("") == Decimal('0.00')

def test_import_csv_duplicates_and_elster(app):
    csv_data = """Belegnummer;Datum;Kunde;Betrag Netto;Steuersatz;Betrag Brutto;Steuer;Typ
INV-001;15.10.2025;Test Client;1.000,00;19,00;1.190,00;190,00;Einnahme
EXP-001;16.10.2025;Test Vendor;500,00;19,00;595,00;95,00;Ausgabe
EXP-002;17.10.2025;Test Vendor;100,00;0,00;100,00;0,00;Ausgabe
"""
    # Initial import
    result = import_papierkram_csv(csv_data)
    assert result["imported"] == 3
    assert result["skipped"] == 0
    assert len(result["errors"]) == 0

    tx_inv = Transaction.query.filter_by(external_id="INV-001").first()
    assert tx_inv.amount_net == Decimal('1000.00')
    assert tx_inv.direction == TransactionDirection.income
    assert tx_inv.ustva_kz_base == '81'
    assert tx_inv.tax_case == 'standard_19'

    tx_exp1 = Transaction.query.filter_by(external_id="EXP-001").first()
    assert tx_exp1.direction == TransactionDirection.expense
    assert tx_exp1.ustva_kz_tax == '66'

    tx_exp2 = Transaction.query.filter_by(external_id="EXP-002").first()
    assert tx_exp2.direction == TransactionDirection.expense
    assert tx_exp2.ustva_kz_tax is None

    # Import same data again (Duplicate detection)
    result2 = import_papierkram_csv(csv_data)
    assert result2["imported"] == 0
    assert result2["skipped"] == 3

def test_dashboard_route(client):
    response = client.get('/')
    assert response.status_code == 200
    assert b"GoBD Accounting Dashboard" in response.data

def test_import_csv_route(client):
    csv_data = """Belegnummer,Datum,Kunde,Betrag Netto,Steuersatz,Betrag Brutto,Steuer,Typ
INV-002,15.10.2025,Test Client 2,1000.00,19.00,1190.00,190.00,Einnahme"""
    data = {'file': (io.BytesIO(csv_data.encode('utf-8')), 'test.csv')}
    response = client.post('/import/csv', data=data, content_type='multipart/form-data', follow_redirects=True)
    assert response.status_code == 200
    assert b"Import completed: 1 imported, 0 skipped." in response.data

    with client.application.app_context():
         tx = Transaction.query.filter_by(external_id="INV-002").first()
         assert tx is not None
         assert tx.status == TransactionStatus.review_pending

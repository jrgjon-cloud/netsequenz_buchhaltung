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

def test_import_papierkram_complex_cases(app):
    csv_data = """Belegnummer;Datum;Kunde;Betrag Netto;Steuersatz;Betrag Brutto;Steuer;Typ;Herkunftsort;Kategorien
R-001;15.10.2025;Test Kunde;4,20;19,00;5,00;0,80;Rechnung;;
B-001;16.10.2025;;100,00;19,00;119,00;19,00;Ausgabe;;Büromaterial
B-002;17.10.2025;EU Firma;100,00;0,00;100,00;0,00;Ausgabe;eu;Fremdleistung
B-003;18.10.2025;Finanzamt;100,00;0,00;100,00;0,00;Einnahme;;Umsatzsteuervorauszahlung Guthaben
"""
    result = import_papierkram_csv(csv_data)
    assert result["imported"] == 4
    assert result["skipped"] == 0
    assert len(result["errors"]) == 0

    tx_r1 = Transaction.query.filter_by(external_id="R-001").first()
    assert tx_r1.amount_net == Decimal('4.20')
    assert tx_r1.tax_rate == Decimal('19.00') # Normed from 19.047
    assert tx_r1.ustva_kz_base == '81'

    tx_b1 = Transaction.query.filter_by(external_id="B-001").first()
    assert tx_b1.counterparty == 'Büromaterial'

    tx_b2 = Transaction.query.filter_by(external_id="B-002").first()
    assert tx_b2.tax_case == 'eu_expense_13b_check'

    tx_b3 = Transaction.query.filter_by(external_id="B-003").first()
    assert tx_b3.direction == TransactionDirection.income
    assert tx_b3.tax_case == 'ust_refund'

def test_import_papierkram_zip(app):
    from app.services.csv_importer import import_papierkram_zip
    import zipfile
    import io

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zf:
        zf.writestr("CSV/Belege.csv", "Belegnummer;Datum;Kunde;Betrag Netto;Steuer;Betrag Brutto;Typ\nB-00385;15.10.2025;Kunde;100.00;19.00;119.00;Ausgabe")
        zf.writestr("Belege/B-00385_1.pdf", b"fake pdf content")

    zip_buffer.seek(0)
    result = import_papierkram_zip(zip_buffer)
    assert result["imported"] == 1

    tx = Transaction.query.filter_by(external_id="B-00385").first()
    assert tx is not None
    import hashlib
    expected_hash = hashlib.sha256(b"fake pdf content").hexdigest()
    assert tx.document_sha256 == expected_hash

def test_contact_converter():
    from app.services.contact_converter import convert_papierkram_contacts_to_invoiceninja
    csv_data = """Name;Kontaktart;Kunden-/Lieferantennr.;Straße (Post);PLZ (Post);Stadt (Post);Land (Post);E-Mail;Telefon;UStID;IBAN;BIC;SEPA-Mandatsreferenz;SEPA-Lastschrift akzeptiert;Vorname (Standardempfänger);Nachname (Standardempfänger)
Test Kunde;Kunde;12345;Musterstraße 1;12345;Musterstadt;Deutschland;test@example.com;0123456789;DE123456789;DE12345678901234567890;TESTDEFF;MANDATE123;Ja;Max;Mustermann
Test Lieferant;Lieferant;54321;Lieferstraße 1;54321;Lieferstadt;Deutschland;lieferant@example.com;0987654321;DE987654321;DE09876543210987654321;LIEFERDEFF;;Nein;Lisa;Lieferant
"""
    result = convert_papierkram_contacts_to_invoiceninja(csv_data)

    assert "Number,Name,First Name,Last Name,Email,Phone,Address 1,Postal Code,City,Country,VAT Number,Custom Value 1 (IBAN),Custom Value 2 (BIC),Custom Value 3 (SEPA Mandate),Public Notes" in result
    assert "12345,Test Kunde,Max,Mustermann,test@example.com,0123456789,Musterstraße 1,12345,Musterstadt,Deutschland,DE123456789,DE12345678901234567890,TESTDEFF,MANDATE123,SEPA akzeptiert: Ja" in result
    assert "54321" not in result # Lieferant should be filtered out

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

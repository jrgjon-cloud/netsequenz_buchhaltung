import pytest
from decimal import Decimal
from app.services.einvoice_parser import parse_en16931_xml

def test_parse_cii_xml_standard_19():
    xml_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
    <rsm:ExchangedDocument>
        <ram:ID>INV-2023-001</ram:ID>
        <ram:IssueDateTime>
            <udt:DateTimeString format="102">20231024</udt:DateTimeString>
        </ram:IssueDateTime>
    </rsm:ExchangedDocument>
    <rsm:SupplyChainTradeTransaction>
        <ram:ApplicableHeaderTradeAgreement>
            <ram:SellerTradeParty>
                <ram:Name>Test Vendor GmbH</ram:Name>
                <ram:PostalTradeAddress>
                    <ram:CountryID>DE</ram:CountryID>
                </ram:PostalTradeAddress>
            </ram:SellerTradeParty>
        </ram:ApplicableHeaderTradeAgreement>
        <ram:ApplicableHeaderTradeSettlement>
            <ram:ApplicableTradeTax>
                <ram:CalculatedAmount>19.00</ram:CalculatedAmount>
                <ram:TypeCode>VAT</ram:TypeCode>
                <ram:BasisAmount>100.00</ram:BasisAmount>
                <ram:CategoryCode>S</ram:CategoryCode>
                <ram:RateApplicablePercent>19.00</ram:RateApplicablePercent>
            </ram:ApplicableTradeTax>
            <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
                <ram:LineTotalAmount>100.00</ram:LineTotalAmount>
                <ram:TaxBasisTotalAmount>100.00</ram:TaxBasisTotalAmount>
                <ram:TaxTotalAmount>19.00</ram:TaxTotalAmount>
                <ram:GrandTotalAmount>119.00</ram:GrandTotalAmount>
            </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        </ram:ApplicableHeaderTradeSettlement>
    </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""

    result = parse_en16931_xml(xml_content)
    assert result['invoice_number'] == 'INV-2023-001'
    assert result['invoice_date'].strftime('%Y-%m-%d') == '2023-10-24'
    assert result['counterparty'] == 'Test Vendor GmbH'
    assert result['amount_net'] == Decimal('100.00')
    assert result['amount_tax'] == Decimal('19.00')
    assert result['amount_gross'] == Decimal('119.00')
    assert result['tax_rate'] == Decimal('19.00')
    assert result['is_reverse_charge'] is False

def test_parse_cii_xml_reverse_charge():
    xml_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100" xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100" xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
    <rsm:ExchangedDocument>
        <ram:ID>INV-EU-002</ram:ID>
    </rsm:ExchangedDocument>
    <rsm:SupplyChainTradeTransaction>
        <ram:ApplicableHeaderTradeSettlement>
            <ram:ApplicableTradeTax>
                <ram:CalculatedAmount>0.00</ram:CalculatedAmount>
                <ram:TypeCode>VAT</ram:TypeCode>
                <ram:BasisAmount>500.00</ram:BasisAmount>
                <ram:CategoryCode>AE</ram:CategoryCode>
                <ram:RateApplicablePercent>0.00</ram:RateApplicablePercent>
            </ram:ApplicableTradeTax>
            <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
                <ram:TaxBasisTotalAmount>500.00</ram:TaxBasisTotalAmount>
                <ram:TaxTotalAmount>0.00</ram:TaxTotalAmount>
                <ram:GrandTotalAmount>500.00</ram:GrandTotalAmount>
            </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        </ram:ApplicableHeaderTradeSettlement>
    </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""

    result = parse_en16931_xml(xml_content)
    assert result['amount_net'] == Decimal('500.00')
    assert result['is_reverse_charge'] is True

from unittest.mock import patch, MagicMock
from app import create_app, db
from app.services.paperless_sync import sync_from_paperless, upload_receipt_to_paperless_and_ledger
from app.models import Transaction, SourceSystem

@pytest.fixture
def app():
    app = create_app('app.config.TestConfig')
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()

@pytest.fixture
def client(app):
    return app.test_client()

@patch('app.services.paperless_sync.requests.get')
def test_sync_from_paperless_mocked(mock_get, app):
    # Mock Document List
    mock_list_response = MagicMock()
    mock_list_response.status_code = 200
    mock_list_response.json.return_value = {
        'results': [
            {
                'id': 100,
                'archive_serial_number': None,
                    'content': 'Test Receipt 50,00 EUR Reverse Charge',
                'created': '2023-10-25T14:30:00Z',
                'title': 'Test Scan'
            }
        ]
    }

    # Mock PDF Download - Generate a valid empty PDF
    from reportlab.pdfgen import canvas
    import io
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer)
    c.drawString(100, 100, "Test Document")
    c.save()

    mock_pdf_response = MagicMock()
    mock_pdf_response.status_code = 200
    mock_pdf_response.content = pdf_buffer.getvalue()

    # Setup mock returns side_effect
    def mock_get_side_effect(url, **kwargs):
        if '/api/documents/' in url and '/download/' not in url:
            return mock_list_response
        elif '/download/' in url:
            return mock_pdf_response
        return MagicMock(status_code=404)

    mock_get.side_effect = mock_get_side_effect

    app.config['PAPERLESS_URL'] = 'http://test-paperless'
    app.config['PAPERLESS_API_TOKEN'] = 'test-token'

    result = sync_from_paperless()
    assert result['imported'] == 1
    assert result['skipped'] == 0
    assert result['errors'] == []

    tx = Transaction.query.filter_by(external_id='DOC-100').first()
    assert tx is not None
    assert tx.source_system == SourceSystem.paperless
    assert tx.amount_gross == Decimal('50.00')
    assert tx.is_reverse_charge is True if hasattr(tx, 'is_reverse_charge') else tx.tax_case == 'reverse_charge_13b_eu'

@patch('app.services.paperless_sync.requests.post')
def test_upload_receipt_mocked(mock_post, app):
    mock_post_response = MagicMock()
    mock_post_response.status_code = 200
    mock_post.return_value = mock_post_response

    app.config['PAPERLESS_URL'] = 'http://test-paperless'
    app.config['PAPERLESS_API_TOKEN'] = 'test-token'

    from reportlab.pdfgen import canvas
    import io
    pdf_buffer = io.BytesIO()
    c = canvas.Canvas(pdf_buffer)
    c.drawString(100, 100, "Upload test content")
    c.save()
    file_bytes = pdf_buffer.getvalue()

    tx = upload_receipt_to_paperless_and_ledger(file_bytes, 'test-upload.pdf')
    assert tx is not None
    assert tx.external_id.startswith('UPLOAD-')
    assert tx.source_system == SourceSystem.paperless

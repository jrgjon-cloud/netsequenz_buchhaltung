import requests
import re
import hashlib
from decimal import Decimal
from datetime import datetime
from flask import current_app
from app import db
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from app.services.einvoice_parser import extract_xml_from_pdf, parse_en16931_xml

def get_paperless_client(paperless_url=None, api_token=None):
    url = paperless_url or current_app.config.get('PAPERLESS_URL')
    token = api_token or current_app.config.get('PAPERLESS_API_TOKEN')

    headers = {}
    if token:
        headers['Authorization'] = f'Token {token}'

    return url, headers

def extract_amounts_from_ocr(content):
    # Basic fallback extraction for amounts if no XML is found
    # Looks for lines with 'Summe', 'Gesamt', etc. followed by an amount
    amounts = re.findall(r'(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})\b', content)

    max_amount = Decimal('0.00')
    for amt_str in amounts:
        # Convert German format (1.234,56) to standard format (1234.56)
        clean_amt = amt_str.replace('.', '').replace(',', '.')
        try:
            val = Decimal(clean_amt)
            if val > max_amount:
                max_amount = val
        except:
            pass

    return max_amount

def sync_from_paperless(paperless_url=None, api_token=None) -> dict:
    url, headers = get_paperless_client(paperless_url, api_token)

    if not url or not headers.get('Authorization'):
        return {"imported": 0, "skipped": 0, "errors": ["Paperless URL or Token not configured"]}

    result = {"imported": 0, "skipped": 0, "errors": []}

    try:
        response = requests.get(f"{url}/api/documents/", headers=headers, timeout=10)
        response.raise_for_status()
        documents = response.json().get('results', [])

        for doc in documents:
            doc_id = doc.get('id')
            asn = doc.get('archive_serial_number')

            # External ID: ASN if available, otherwise DOC-{id}
            external_id = f"ASN-{asn}" if asn else f"DOC-{doc_id}"

            # Check if transaction already exists
            existing = Transaction.query.filter_by(
                source_system=SourceSystem.paperless,
                external_id=external_id
            ).first()

            if existing:
                result["skipped"] += 1
                continue

            # Fetch original PDF
            pdf_response = requests.get(f"{url}/api/documents/{doc_id}/download/", headers=headers, timeout=10)
            if pdf_response.status_code != 200:
                result["errors"].append(f"Could not download PDF for Document {doc_id}")
                continue

            pdf_bytes = pdf_response.content
            document_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

            # Deduplicate by document_sha256 to handle manual uploads that are now synced
            existing_upload = Transaction.query.filter_by(
                source_system=SourceSystem.paperless,
                document_sha256=document_sha256
            ).first()

            if existing_upload:
                if existing_upload.external_id.startswith('UPLOAD-'):
                    # Update placeholder ID to the real synced ID
                    existing_upload.external_id = external_id
                    db.session.add(AuditLog(
                        transaction_id=existing_upload.id,
                        action='updated_external_id',
                        details=f'Updated external_id from UPLOAD placeholder to {external_id} during sync'
                    ))
                    db.session.commit()
                result["skipped"] += 1
                continue

            # Stage 1: XML-First
            xml_bytes = extract_xml_from_pdf(pdf_bytes)
            invoice_data = None

            if xml_bytes:
                try:
                    invoice_data = parse_en16931_xml(xml_bytes)
                except Exception as e:
                    # XML parse failed, fallback to OCR
                    pass

            if not invoice_data:
                # Stage 2: OCR-Fallback
                content = doc.get('content', '')
                created_str = doc.get('created', doc.get('created_date'))

                invoice_date = None
                if created_str:
                    try:
                        # Handle standard ISO 8601 formatting from Paperless
                        invoice_date = datetime.fromisoformat(created_str.replace('Z', '+00:00')).date()
                    except:
                        pass
                if not invoice_date:
                    invoice_date = datetime.today().date()

                amount_gross = extract_amounts_from_ocr(content)

                # Try to guess tax and reverse charge
                is_reverse_charge = False
                if re.search(r'Reverse Charge|§ 13b|Steuerschuldnerschaft des Leistungsempfängers', content, re.IGNORECASE):
                    is_reverse_charge = True

                # Assume 19% standard if not reverse charge and amount > 0
                tax_rate = Decimal('0.00')
                if not is_reverse_charge and amount_gross > 0:
                    tax_rate = Decimal('19.00')

                amount_net = amount_gross
                amount_tax = Decimal('0.00')
                if tax_rate > 0:
                    # Simple net calculation
                    amount_net = round(amount_gross / (Decimal('1') + (tax_rate / Decimal('100'))), 2)
                    amount_tax = amount_gross - amount_net

                invoice_data = {
                    "invoice_number": None,
                    "invoice_date": invoice_date,
                    "counterparty": "Unknown (Paperless)",
                    "country": None,
                    "amount_net": amount_net,
                    "amount_tax": amount_tax,
                    "amount_gross": amount_gross,
                    "tax_rate": tax_rate,
                    "is_reverse_charge": is_reverse_charge
                }

            # Map Taxes
            tax_case = 'other'
            ustva_kz_base = None
            ustva_kz_tax = None

            if invoice_data.get('is_reverse_charge'):
                tax_case = 'reverse_charge_13b_eu'
                ustva_kz_base = '46'
                ustva_kz_tax = '67'
            elif invoice_data.get('tax_rate', Decimal('0')) > 0:
                if invoice_data.get('tax_rate') == Decimal('19.00') or invoice_data.get('tax_rate') == Decimal('19'):
                    tax_case = 'standard_19'
                elif invoice_data.get('tax_rate') == Decimal('7.00') or invoice_data.get('tax_rate') == Decimal('7'):
                    tax_case = 'reduced_7'
                else:
                    tax_case = 'other'
                ustva_kz_tax = '66'

            # Determine invoice date
            inv_date = invoice_data.get('invoice_date')
            if not inv_date:
                inv_date = datetime.today().date()

            # Create Transaction
            tx = Transaction(
                source_system=SourceSystem.paperless,
                external_id=external_id,
                direction=TransactionDirection.expense,
                status=TransactionStatus.review_pending,
                invoice_date=inv_date,
                payment_date=None,
                counterparty=invoice_data.get('counterparty') or f"Paperless Document {doc_id}",
                description=doc.get('title', f"Paperless Upload {doc_id}"),
                amount_net=invoice_data.get('amount_net', Decimal('0.00')),
                tax_rate=invoice_data.get('tax_rate', Decimal('0.00')),
                amount_tax=invoice_data.get('amount_tax', Decimal('0.00')),
                amount_gross=invoice_data.get('amount_gross', Decimal('0.00')),
                tax_case=tax_case,
                ustva_kz_base=ustva_kz_base,
                ustva_kz_tax=ustva_kz_tax,
                document_sha256=document_sha256
            )

            db.session.add(tx)
            db.session.flush()

            db.session.add(AuditLog(
                transaction_id=tx.id,
                action='imported',
                details=f'Imported from Paperless-ngx (External ID: {external_id})'
            ))

            result["imported"] += 1
            db.session.commit()

    except requests.exceptions.RequestException as e:
        result["errors"].append(f"API Error: {str(e)}")
    except Exception as e:
        db.session.rollback()
        result["errors"].append(f"Error processing documents: {str(e)}")

    return result


def upload_receipt_to_paperless_and_ledger(file_bytes, filename, paperless_url=None, api_token=None) -> Transaction:
    url, headers = get_paperless_client(paperless_url, api_token)

    if not url or not headers.get('Authorization'):
        raise Exception("Paperless API not configured.")

    # 1. Post to Paperless
    files = {'document': (filename, file_bytes)}
    post_resp = requests.post(f"{url}/api/documents/post_document/", headers=headers, files=files)
    post_resp.raise_for_status()

    # 2. Extract Data Locally Immediately (Don't wait for Paperless OCR)
    document_sha256 = hashlib.sha256(file_bytes).hexdigest()

    invoice_data = None
    if filename.lower().endswith('.pdf'):
        xml_bytes = extract_xml_from_pdf(file_bytes)
        if xml_bytes:
            try:
                invoice_data = parse_en16931_xml(xml_bytes)
            except:
                pass

    if not invoice_data:
        # Fallback empty data if not parsed
        invoice_data = {
            "invoice_number": None,
            "invoice_date": datetime.today().date(),
            "counterparty": "Unknown (Manual Upload)",
            "country": None,
            "amount_net": Decimal('0.00'),
            "amount_tax": Decimal('0.00'),
            "amount_gross": Decimal('0.00'),
            "tax_rate": Decimal('0.00'),
            "is_reverse_charge": False
        }

    # Map Taxes
    tax_case = 'other'
    ustva_kz_base = None
    ustva_kz_tax = None

    if invoice_data.get('is_reverse_charge'):
        tax_case = 'reverse_charge_13b_eu'
        ustva_kz_base = '46'
        ustva_kz_tax = '67'
    elif invoice_data.get('tax_rate', Decimal('0')) > 0:
        if invoice_data.get('tax_rate') == Decimal('19.00') or invoice_data.get('tax_rate') == Decimal('19'):
            tax_case = 'standard_19'
        elif invoice_data.get('tax_rate') == Decimal('7.00') or invoice_data.get('tax_rate') == Decimal('7'):
            tax_case = 'reduced_7'
        else:
            tax_case = 'other'
        ustva_kz_tax = '66'

    inv_date = invoice_data.get('invoice_date')
    if not inv_date:
        inv_date = datetime.today().date()

    # Create Transaction directly
    # We use a placeholder external_id until it's synced. Using hash for now.
    external_id = f"UPLOAD-{document_sha256[:10]}"

    tx = Transaction(
        source_system=SourceSystem.paperless,
        external_id=external_id,
        direction=TransactionDirection.expense,
        status=TransactionStatus.review_pending,
        invoice_date=inv_date,
        payment_date=None,
        counterparty=invoice_data.get('counterparty') or "Manual Upload",
        description=filename,
        amount_net=invoice_data.get('amount_net', Decimal('0.00')),
        tax_rate=invoice_data.get('tax_rate', Decimal('0.00')),
        amount_tax=invoice_data.get('amount_tax', Decimal('0.00')),
        amount_gross=invoice_data.get('amount_gross', Decimal('0.00')),
        tax_case=tax_case,
        ustva_kz_base=ustva_kz_base,
        ustva_kz_tax=ustva_kz_tax,
        document_sha256=document_sha256
    )

    db.session.add(tx)
    db.session.flush()

    db.session.add(AuditLog(
        transaction_id=tx.id,
        action='imported',
        details=f'Direct upload to Paperless and Ledger (External ID: {external_id})'
    ))

    db.session.commit()
    return tx

def fetch_paperless_pdf(external_id, paperless_url=None, api_token=None) -> bytes | None:
    url, headers = get_paperless_client(paperless_url, api_token)

    if not url or not headers.get('Authorization'):
        return None

    # Extract ID from external_id (e.g. DOC-123 or ASN-456)
    doc_id = None
    if external_id.startswith('DOC-'):
        doc_id = external_id.replace('DOC-', '')
    elif external_id.startswith('ASN-'):
        # For ASN we need to search first
        asn = external_id.replace('ASN-', '')
        search_resp = requests.get(f"{url}/api/documents/?query=archive_serial_number:{asn}", headers=headers)
        if search_resp.status_code == 200:
            results = search_resp.json().get('results', [])
            if results:
                doc_id = results[0]['id']

    if not doc_id:
        return None

    try:
        response = requests.get(f"{url}/api/documents/{doc_id}/download/", headers=headers)
        response.raise_for_status()
        return response.content
    except:
        return None

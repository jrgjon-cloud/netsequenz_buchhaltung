import csv
import io
import re
import zipfile
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from sqlalchemy.exc import IntegrityError
from app import db
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from app.services.gobd_ledger import lock_transaction

def parse_date(date_str):
    if not date_str:
        return None
    # Try DD.MM.YYYY
    match = re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', date_str)
    if match:
        return datetime(int(match.group(3)), int(match.group(2)), int(match.group(1))).date()
    # Try YYYY-MM-DD
    match = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', date_str)
    if match:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
    return None

def parse_amount(amount_str):
    if not amount_str:
        return Decimal('0.00')
    # Remove currency symbols and spaces
    amount_str = re.sub(r'[^\d,\.-]', '', amount_str)
    if not amount_str:
         return Decimal('0.00')

    # Check if it's German format (1.234,56) or English format (1234.56)
    if ',' in amount_str and '.' in amount_str:
        if amount_str.rfind(',') > amount_str.rfind('.'):
            # German
            amount_str = amount_str.replace('.', '').replace(',', '.')
        else:
            # English
            amount_str = amount_str.replace(',', '')
    elif ',' in amount_str:
        # German format with no thousands separator
        amount_str = amount_str.replace(',', '.')

    try:
        return Decimal(amount_str)
    except InvalidOperation:
        return Decimal('0.00')

def import_papierkram_csv(file_stream_or_text, auto_lock=False):
    if isinstance(file_stream_or_text, bytes):
        content = file_stream_or_text.decode('utf-8')
    elif hasattr(file_stream_or_text, 'read'):
        content = file_stream_or_text.read()
        if isinstance(content, bytes):
            content = content.decode('utf-8')
    else:
        content = file_stream_or_text

    # Auto-detect delimiter
    first_line = content.split('\n')[0] if '\n' in content else content
    delimiter = ';' if ';' in first_line else ','

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    # Map headers flexibly (case-insensitive)
    def find_column(headers, *keywords):
        headers_lower = [h.lower().strip() for h in headers if h]
        # First priority: Exact match
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in headers_lower:
                idx = headers_lower.index(kw_lower)
                return headers[idx]

        # Second priority: Substring match
        for kw in keywords:
            kw_lower = kw.lower()
            for i, h in enumerate(headers_lower):
                if kw_lower in h:
                    return headers[i]

        return None

    headers = reader.fieldnames
    if not headers:
         return {"imported": 0, "skipped": 0, "errors": ["Empty CSV"]}

    col_id = find_column(headers, 'belegnummer', 'rechnungsnr.', 'rechnungsnummer', 'id', 'external_id')
    col_invoice_date = find_column(headers, 'rechnungsdatum', 'belegdatum', 'datum', 'invoice_date')
    col_payment_date = find_column(headers, 'zahlung am', 'letztes bezahldatum', 'zahlungsdatum', 'bezahlt am', 'payment_date')
    col_direction = find_column(headers, 'typ', 'art', 'direction')
    col_counterparty = find_column(headers, 'kunde', 'lieferant', 'kontakt', 'gegenpartei', 'counterparty')
    col_description = find_column(headers, 'betreff', 'gegenstand', 'beschreibung', 'verwendungszweck', 'notiz', 'description')

    col_amount_net = find_column(headers, 'netto', 'betrag netto', 'amount_net')
    col_tax_rate = find_column(headers, 'steuersatz', 'ust-%', 'tax_rate')
    col_amount_tax = find_column(headers, 'ust.', 'steuer', 'ust-betrag', 'amount_tax')
    col_amount_gross = find_column(headers, 'brutto', 'betrag brutto', 'amount_gross')

    col_origin = find_column(headers, 'herkunftsort')
    col_euer_line = find_column(headers, 'kategorien', 'kategorie', 'eür-zeile', 'euer_line')
    col_note = find_column(headers, 'notiz')
    col_goods_services = find_column(headers, 'waren & dienstleistungen')

    result = {"imported": 0, "skipped": 0, "errors": []}

    for row_idx, row in enumerate(reader):
        try:
            # Fallback for ID
            external_id = row.get(col_id) if col_id else None
            if not external_id:
                # Deterministic hash
                import hashlib
                row_str = "".join(str(v) for v in row.values())
                external_id = hashlib.md5(row_str.encode()).hexdigest()

            # Dates
            invoice_date_str = row.get(col_invoice_date) if col_invoice_date else None
            invoice_date = parse_date(invoice_date_str)
            if not invoice_date:
                result["errors"].append(f"Row {row_idx + 1}: Missing or invalid invoice_date")
                continue

            payment_date_str = row.get(col_payment_date) if col_payment_date else None
            payment_date = parse_date(payment_date_str)

            # Direction and special checks (UStVA refund)
            euer_line = row.get(col_euer_line) if col_euer_line else None
            euer_line_str = (euer_line or '').lower()

            # Counterparty & description Fallbacks
            counterparty = row.get(col_counterparty) if col_counterparty else None
            description = row.get(col_description) if col_description else None

            if not description:
                if col_note and row.get(col_note):
                    description = row.get(col_note)
                elif col_goods_services and row.get(col_goods_services):
                    description = row.get(col_goods_services)
                else:
                    description = f"Rechnung {external_id}"

            if not counterparty:
                if 'ustva' in description.lower() or 'finanzamt' in description.lower() or 'ustva' in euer_line_str:
                    counterparty = 'Finanzamt'
                elif euer_line:
                    counterparty = euer_line
                else:
                    counterparty = 'Unbekannt'

            # Amounts
            amount_net = parse_amount(row.get(col_amount_net) if col_amount_net else None)
            amount_gross = parse_amount(row.get(col_amount_gross) if col_amount_gross else None)
            amount_tax = parse_amount(row.get(col_amount_tax) if col_amount_tax else None)

            tax_rate_str = row.get(col_tax_rate) if col_tax_rate else None
            if tax_rate_str:
                tax_rate = parse_amount(tax_rate_str)
            else:
                 if amount_net != Decimal('0'):
                     tax_rate = (abs(amount_tax) / abs(amount_net)) * 100
                 else:
                     tax_rate = Decimal('0')

            # Normalize tax_rate (e.g. 19.05% -> 19.00%)
            if Decimal('18.0') <= tax_rate <= Decimal('20.0'):
                tax_rate = Decimal('19.00')
            elif Decimal('6.0') <= tax_rate <= Decimal('8.0'):
                tax_rate = Decimal('7.00')
            else:
                tax_rate = round(tax_rate, 2)

            # Direction
            direction_str = str(row.get(col_direction) if col_direction else '').lower()

            is_ust_refund = False
            if str(external_id).startswith('B-') and 'umsatzsteuervorauszahlung guthaben' in euer_line_str and amount_net > 0:
                direction = TransactionDirection.income
                is_ust_refund = True
            elif 'rechnung' == direction_str or 'einnahme' in direction_str or 'income' in direction_str:
                direction = TransactionDirection.income
            elif 'ausgabe' in direction_str or 'expense' in direction_str or 'beleg' in str(external_id).lower():
                direction = TransactionDirection.expense
            else:
                # Infer from amounts
                if amount_net < 0:
                    direction = TransactionDirection.expense
                else:
                    direction = TransactionDirection.income

            # For Belege.csv, negatives mean expenses
            if str(external_id).startswith('B-') and amount_net < 0 and not is_ust_refund:
                direction = TransactionDirection.expense

            # Normalize amounts to be positive
            amount_net = abs(amount_net)
            amount_gross = abs(amount_gross)
            amount_tax = abs(amount_tax)

            origin = str(row.get(col_origin)).lower() if col_origin else ''

            # ELSTER logic
            ustva_kz_base = None
            ustva_kz_tax = None
            tax_case = 'other'

            if is_ust_refund:
                tax_case = 'ust_refund'
            elif direction == TransactionDirection.expense and origin == 'eu' and amount_tax == Decimal('0'):
                tax_case = 'eu_expense_13b_check'
            elif direction == TransactionDirection.income:
                 if tax_rate == Decimal('19') or tax_rate == Decimal('19.00'):
                      tax_case = 'standard_19'
                      ustva_kz_base = '81'
                 elif tax_rate == Decimal('7') or tax_rate == Decimal('7.00'):
                      tax_case = 'reduced_7'
                      ustva_kz_base = '86'
            elif direction == TransactionDirection.expense:
                 if tax_rate > 0:
                      ustva_kz_tax = '66'

            # Create or update transaction
            existing_tx = Transaction.query.filter_by(
                source_system=SourceSystem.papierkram_legacy,
                external_id=external_id
            ).first()

            if existing_tx:
                result["skipped"] += 1
                continue

            tx = Transaction(
                source_system=SourceSystem.papierkram_legacy,
                external_id=external_id,
                direction=direction,
                status=TransactionStatus.review_pending,
                invoice_date=invoice_date,
                payment_date=payment_date,
                counterparty=counterparty,
                description=description,
                amount_net=amount_net,
                tax_rate=tax_rate,
                amount_tax=amount_tax,
                amount_gross=amount_gross,
                tax_case=tax_case,
                ustva_kz_base=ustva_kz_base,
                ustva_kz_tax=ustva_kz_tax,
                euer_line=euer_line
            )
            db.session.add(tx)
            db.session.flush() # Get ID for audit log

            db.session.add(AuditLog(
                transaction_id=tx.id,
                action='imported',
                details=f'Imported from Papierkram CSV'
            ))

            if auto_lock:
                 lock_transaction(tx.id)

            result["imported"] += 1
            db.session.commit()

        except Exception as e:
            result["errors"].append(f"Row {row_idx + 1}: {str(e)}")
            db.session.rollback()

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        result["errors"].append(f"Integrity Error during final commit: {str(e)}")

    # Add audit log for the overall import
    db.session.add(AuditLog(
        action='csv_import_completed',
        details=f'Imported: {result["imported"]}, Skipped: {result["skipped"]}, Errors: {len(result["errors"])}'
    ))
    db.session.commit()

    return result

def import_papierkram_zip(zip_bytes_or_file, auto_lock=False):
    if hasattr(zip_bytes_or_file, 'read'):
        zf = zipfile.ZipFile(zip_bytes_or_file)
    else:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes_or_file))

    pdf_hashes = {}
    csv_rechnungen = None
    csv_belege = None

    for name in zf.namelist():
        if name.endswith('.pdf'):
            # Calculate SHA-256
            data = zf.read(name)
            sha256_hash = hashlib.sha256(data).hexdigest()

            # Extract prefix for matching (e.g. B-00385 or R-00200)
            filename = name.split('/')[-1]
            match = re.search(r'([A-Z]-\d+)', filename)
            if match:
                prefix = match.group(1)
                pdf_hashes[prefix] = sha256_hash
        elif name.endswith('Rechnungen.csv'):
            csv_rechnungen = zf.read(name)
        elif name.endswith('Belege.csv'):
            csv_belege = zf.read(name)

    result_combined = {"imported": 0, "skipped": 0, "errors": []}

    if csv_rechnungen:
        res = import_papierkram_csv(csv_rechnungen, auto_lock=auto_lock)
        result_combined['imported'] += res['imported']
        result_combined['skipped'] += res['skipped']
        result_combined['errors'].extend(res['errors'])

    if csv_belege:
        res = import_papierkram_csv(csv_belege, auto_lock=auto_lock)
        result_combined['imported'] += res['imported']
        result_combined['skipped'] += res['skipped']
        result_combined['errors'].extend(res['errors'])

    # Update transactions with document_sha256
    if pdf_hashes:
        for prefix, sha256_hash in pdf_hashes.items():
            txs = Transaction.query.filter(Transaction.external_id.like(f"%{prefix}%")).all()
            for tx in txs:
                if not tx.document_sha256:
                    tx.document_sha256 = sha256_hash
        db.session.commit()

    return result_combined

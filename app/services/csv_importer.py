import csv
import io
import re
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
        for h in headers_lower:
             for kw in keywords:
                 if kw.lower() in h:
                     return next((orig_h for orig_h in headers if orig_h.lower().strip() == h), None)
        return None

    headers = reader.fieldnames
    if not headers:
         return {"imported": 0, "skipped": 0, "errors": ["Empty CSV"]}

    col_id = find_column(headers, 'belegnummer', 'id', 'external_id')
    col_invoice_date = find_column(headers, 'rechnungsdatum', 'belegdatum', 'datum', 'invoice_date')
    col_payment_date = find_column(headers, 'zahlungsdatum', 'bezahlt am', 'payment_date')
    col_direction = find_column(headers, 'typ', 'art', 'direction')
    col_counterparty = find_column(headers, 'kunde', 'lieferant', 'kontakt', 'gegenpartei', 'counterparty')
    col_description = find_column(headers, 'beschreibung', 'betreff', 'verwendungszweck', 'description')
    col_amount_net = find_column(headers, 'netto', 'betrag netto', 'amount_net')
    col_tax_rate = find_column(headers, 'steuersatz', 'ust-%', 'tax_rate')
    col_amount_tax = find_column(headers, 'steuer', 'ust-betrag', 'amount_tax')
    col_amount_gross = find_column(headers, 'brutto', 'betrag brutto', 'amount_gross')
    col_euer_line = find_column(headers, 'eür-zeile', 'kategorie', 'euer_line')

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

            # Amounts
            amount_net = parse_amount(row.get(col_amount_net) if col_amount_net else None)
            amount_gross = parse_amount(row.get(col_amount_gross) if col_amount_gross else None)
            amount_tax = parse_amount(row.get(col_amount_tax) if col_amount_tax else None)

            tax_rate_str = row.get(col_tax_rate) if col_tax_rate else None
            if tax_rate_str:
                tax_rate = parse_amount(tax_rate_str)
            else:
                 if amount_net != Decimal('0'):
                     tax_rate = (amount_tax / amount_net) * 100
                 else:
                     tax_rate = Decimal('0')

            # Direction
            direction_str = str(row.get(col_direction) if col_direction else '').lower()
            if 'einnahme' in direction_str or 'income' in direction_str:
                direction = TransactionDirection.income
            elif 'ausgabe' in direction_str or 'expense' in direction_str:
                direction = TransactionDirection.expense
            else:
                # Infer from amounts
                if amount_net < 0:
                    direction = TransactionDirection.expense
                else:
                    direction = TransactionDirection.income

            # Normalize amounts to be positive
            amount_net = abs(amount_net)
            amount_gross = abs(amount_gross)
            amount_tax = abs(amount_tax)

            # Counterparty & description
            counterparty = row.get(col_counterparty) if col_counterparty else 'Unbekannt'
            description = row.get(col_description) if col_description else 'Keine Beschreibung'

            # ELSTER logic
            ustva_kz_base = None
            ustva_kz_tax = None
            tax_case = 'other'

            if direction == TransactionDirection.income:
                 if tax_rate == Decimal('19') or tax_rate == Decimal('19.00'):
                      tax_case = 'standard_19'
                      ustva_kz_base = '81'
                 elif tax_rate == Decimal('7') or tax_rate == Decimal('7.00'):
                      tax_case = 'reduced_7'
                      ustva_kz_base = '86'
            elif direction == TransactionDirection.expense:
                 if tax_rate > 0:
                      ustva_kz_tax = '66'

            euer_line = row.get(col_euer_line) if col_euer_line else None

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

import io
import csv
import zipfile
from app.models import Transaction, AuditLog, TransactionStatus
from sqlalchemy import extract

def generate_gobd_z3_zip(year: int) -> bytes:
    """
    Generates a GoBD/GDPdU-compliant ZIP archive in memory for tax auditors (Z3 Data Access).
    """
    transactions = Transaction.query.filter(
        extract('year', Transaction.invoice_date) == year,
        Transaction.status == TransactionStatus.locked
    ).order_by(Transaction.lock_sequence.asc()).all()

    audit_logs = AuditLog.query.filter(
        extract('year', AuditLog.timestamp) == year
    ).order_by(AuditLog.timestamp.asc()).all()

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # transactions_{year}.csv
        tx_buffer = io.StringIO()
        tx_writer = csv.writer(tx_buffer, delimiter=';')
        tx_writer.writerow([
            'id', 'lock_sequence', 'source_system', 'external_id', 'direction',
            'invoice_date', 'payment_date', 'counterparty', 'description',
            'amount_net', 'tax_rate', 'amount_tax', 'amount_gross', 'tax_case',
            'previous_hash', 'record_hash', 'document_sha256'
        ])
        for tx in transactions:
            tx_writer.writerow([
                tx.id, tx.lock_sequence, tx.source_system.value, tx.external_id, tx.direction.value,
                tx.invoice_date.isoformat(), tx.payment_date.isoformat() if tx.payment_date else '',
                tx.counterparty, tx.description,
                f"{tx.amount_net:.2f}", f"{tx.tax_rate:.2f}", f"{tx.amount_tax:.2f}", f"{tx.amount_gross:.2f}", tx.tax_case,
                tx.previous_hash or '', tx.record_hash or '', tx.document_sha256 or ''
            ])
        zip_file.writestr(f'transactions_{year}.csv', tx_buffer.getvalue().encode('utf-8'))

        # audit_log_{year}.csv
        audit_buffer = io.StringIO()
        audit_writer = csv.writer(audit_buffer, delimiter=';')
        audit_writer.writerow(['id', 'transaction_id', 'action', 'timestamp', 'details'])
        for log in audit_logs:
            audit_writer.writerow([
                log.id, log.transaction_id or '', log.action, log.timestamp.isoformat(), log.details or ''
            ])
        zip_file.writestr(f'audit_log_{year}.csv', audit_buffer.getvalue().encode('utf-8'))

        # index.xml
        index_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<index>
    <dataset>
        <name>Transactions {year}</name>
        <file>transactions_{year}.csv</file>
    </dataset>
    <dataset>
        <name>Audit Log {year}</name>
        <file>audit_log_{year}.csv</file>
    </dataset>
</index>
"""
        zip_file.writestr('index.xml', index_xml.encode('utf-8'))

        # VERFAHRENSDOKUMENTATION.md
        verfahrensdoku = f"""# Verfahrensdokumentation {year}

Die Datenverarbeitung erfolgt über ein Flask-basiertes Dashboard.
Belege werden per Paperless-ngx per SHA-256 Hash verknüpft (document_sha256).
Ausgangsrechnungen stammen von Invoice Ninja.
Jede festgeschriebene Buchung erhält einen kryptografisch verketteten SHA-256 Hash zur Sicherstellung der Unveränderbarkeit (GoBD).
"""
        zip_file.writestr('VERFAHRENSDOKUMENTATION.md', verfahrensdoku.encode('utf-8'))

    return zip_buffer.getvalue()

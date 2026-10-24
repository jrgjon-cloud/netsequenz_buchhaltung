from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, Response
from app.models import Transaction, TransactionDirection, TransactionStatus
from app.services.gobd_ledger import lock_transaction, cancel_transaction, verify_ledger_integrity, recalculate_hash_chain
from app.services.csv_importer import import_papierkram_csv, import_papierkram_zip
from app.services.contact_converter import convert_papierkram_contacts_to_invoiceninja
from app.services.paperless_sync import sync_from_paperless, upload_receipt_to_paperless_and_ledger, fetch_paperless_pdf
from app.services.invoiceninja_sync import sync_from_invoiceninja, create_invoice_via_cockpit, fetch_invoiceninja_pdf, mark_invoiceninja_paid
from app.models import SourceSystem, AuditLog
from app.services.payment_poller import poll_payments
from app.services.tax_calculator import calculate_elster_metrics
from app.services.z3_exporter import generate_gobd_z3_zip
from app import db
from sqlalchemy import extract
from decimal import Decimal
import datetime
import zipfile

main_bp = Blueprint('main', __name__)


from flask import jsonify
from app.services.gobd_ledger import verify_ledger_integrity
from app import db

@main_bp.route('/health')
def healthcheck():
    try:
        # Check DB connection
        db.session.execute(db.text('SELECT 1'))

        # Check Ledger Integrity
        integrity_ok = verify_ledger_integrity()

        if integrity_ok:
            return jsonify({"status": "ok", "gobd_integrity": True}), 200
        else:
            return jsonify({"status": "error", "message": "Ledger integrity check failed"}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/')
def dashboard():
    year = request.args.get('year', datetime.datetime.now().year, type=int)
    status_filter = request.args.get('status')
    direction_filter = request.args.get('direction')

    query = Transaction.query.filter(extract('year', Transaction.invoice_date) == year)

    if status_filter:
        try:
            status_enum = TransactionStatus[status_filter]
            query = query.filter(Transaction.status == status_enum)
        except KeyError:
            pass

    if direction_filter:
        try:
            direction_enum = TransactionDirection[direction_filter]
            query = query.filter(Transaction.direction == direction_enum)
        except KeyError:
            pass

    transactions = query.order_by(Transaction.invoice_date.desc()).all()

    # Calculate sums
    income_net = sum(tx.amount_net for tx in transactions if tx.direction == TransactionDirection.income and tx.status != TransactionStatus.cancelled)
    expense_net = sum(tx.amount_net for tx in transactions if tx.direction == TransactionDirection.expense and tx.status != TransactionStatus.cancelled)

    # VAT preview
    vat_collected = sum(tx.amount_tax for tx in transactions if tx.direction == TransactionDirection.income and tx.status != TransactionStatus.cancelled)
    vat_paid = sum(tx.amount_tax for tx in transactions if tx.direction == TransactionDirection.expense and tx.status != TransactionStatus.cancelled)
    vat_liability = vat_collected - vat_paid

    try:
        integrity_ok = verify_ledger_integrity()
    except Exception as e:
        integrity_ok = False
        flash(f"Ledger Integrity Error: {str(e)}", "danger")

    # Fetch ELSTER Period
    elster_period = request.args.get('elster_period', 'year')
    elster_metrics = calculate_elster_metrics(year, elster_period)

    today = datetime.date.today().isoformat()

    return render_template(
        'dashboard.html',
        transactions=transactions,
        year=year,
        elster_period=elster_period,
        elster_metrics=elster_metrics,
        income_net=income_net,
        expense_net=expense_net,
        vat_liability=vat_liability,
        integrity_ok=integrity_ok,
        today=today,
        SourceSystem=SourceSystem,
        TransactionDirection=TransactionDirection,
        TransactionStatus=TransactionStatus
    )

@main_bp.route('/sync/paperless', methods=['POST'])
def sync_paperless():
    try:
        result = sync_from_paperless()
        flash(f'Paperless sync completed: {result["imported"]} imported, {result["skipped"]} skipped.', 'success')
        if result["errors"]:
            for err in result["errors"][:5]:
                 flash(f"Error: {err}", "danger")
    except Exception as e:
        flash(f'Paperless sync failed: {str(e)}', 'danger')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/period/lock', methods=['POST'])
def lock_period():
    year = request.form.get('year', type=int)
    period = request.form.get('period', 'year')

    if not year:
        flash('Year is required.', 'danger')
        return redirect(url_for('main.dashboard'))

    transactions_to_lock = Transaction.query.filter(
        Transaction.status == TransactionStatus.review_pending,
        Transaction.payment_date.isnot(None),
        extract('year', Transaction.invoice_date) == year
    )

    if period != 'year':
        if period.startswith('Q'):
            quarter = int(period[1])
            start_month = (quarter - 1) * 3 + 1
            end_month = start_month + 2
            transactions_to_lock = transactions_to_lock.filter(
                extract('month', Transaction.invoice_date) >= start_month,
                extract('month', Transaction.invoice_date) <= end_month
            )
        else:
            try:
                month = int(period)
                transactions_to_lock = transactions_to_lock.filter(
                    extract('month', Transaction.invoice_date) == month
                )
            except ValueError:
                pass

    transactions_to_lock = transactions_to_lock.order_by(Transaction.invoice_date.asc()).all()

    locked_count = 0
    for tx in transactions_to_lock:
        try:
            lock_transaction(tx.id)
            locked_count += 1
        except Exception as e:
            flash(f"Error locking transaction {tx.id}: {str(e)}", "danger")

    flash(f'Period locked. {locked_count} transactions secured.', 'success')
    return redirect(url_for('main.dashboard', year=year, elster_period=period))

@main_bp.route('/export/gobd-z3', methods=['GET'])
def export_gobd_z3():
    year = request.args.get('year', datetime.date.today().year, type=int)
    try:
        zip_data = generate_gobd_z3_zip(year)
        return Response(
            zip_data,
            mimetype="application/zip",
            headers={"Content-disposition": f"attachment; filename=gobd_export_{year}.zip"}
        )
    except Exception as e:
        flash(f'Z3 Export failed: {str(e)}', 'danger')
        return redirect(url_for('main.dashboard', year=year))

@main_bp.route('/upload/receipt', methods=['POST'])
def upload_receipt():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('main.dashboard'))

    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('main.dashboard'))

    try:
        file_bytes = file.read()
        tx = upload_receipt_to_paperless_and_ledger(file_bytes, file.filename)
        flash(f'Receipt uploaded successfully and added as Transaction ID {tx.id}.', 'success')
    except Exception as e:
        flash(f'Upload failed: {str(e)}', 'danger')

    return redirect(url_for('main.dashboard'))

@main_bp.route('/import/csv', methods=['POST'])
def import_csv():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('main.dashboard'))

    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('main.dashboard'))

    auto_lock = 'auto_lock' in request.form

    try:
        if file.filename.lower().endswith('.zip'):
            result = import_papierkram_zip(file, auto_lock=auto_lock)
        else:
            result = import_papierkram_csv(file, auto_lock=auto_lock)

        flash(f'Import completed: {result["imported"]} imported, {result["skipped"]} skipped.', 'success')
        if result["errors"]:
            for err in result["errors"][:5]: # Show up to 5 errors
                 flash(f"Error: {err}", "danger")
            if len(result["errors"]) > 5:
                 flash(f"...and {len(result['errors']) - 5} more errors.", "danger")
    except Exception as e:
        flash(f'Import failed: {str(e)}', 'danger')

    return redirect(url_for('main.dashboard'))

@main_bp.route('/convert/contacts', methods=['POST'])
def convert_contacts():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('main.dashboard'))

    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('main.dashboard'))

    try:
        converted_csv = convert_papierkram_contacts_to_invoiceninja(file)
        return Response(
            converted_csv,
            mimetype="text/csv",
            headers={"Content-disposition": "attachment; filename=invoiceninja_clients.csv"}
        )
    except Exception as e:
        flash(f'Conversion failed: {str(e)}', 'danger')
        return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/pdf')
def get_transaction_pdf(id):
    tx = db.session.get(Transaction, id)
    if not tx:
        return "Transaction not found", 404

    if tx.source_system.value == 'paperless':
        pdf_bytes = fetch_paperless_pdf(tx.external_id)
        if not pdf_bytes:
            return "Could not fetch PDF from Paperless", 404
        return Response(pdf_bytes, mimetype='application/pdf', headers={'Content-Disposition': 'inline'})
    elif tx.source_system.value == 'invoice_ninja':
        pdf_bytes = fetch_invoiceninja_pdf(tx.external_id)
        if not pdf_bytes:
            return "Could not fetch PDF from Invoice Ninja", 404
        return Response(pdf_bytes, mimetype='application/pdf', headers={'Content-Disposition': 'inline'})
    else:
        return "PDF not available for this transaction", 404

@main_bp.route('/sync/invoiceninja', methods=['POST'])
def sync_invoiceninja_route():
    results = sync_from_invoiceninja()
    if results.get('errors'):
        for err in results['errors']:
            flash(err, 'danger')
    flash(f"Invoice Ninja Sync abgeschlossen: {results.get('created', 0)} erstellt, {results.get('updated', 0)} aktualisiert.", 'success')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/invoices/create', methods=['POST'])
def create_invoice_route():
    client_name = request.form.get('client_name')
    client_email = request.form.get('client_email')
    subject = request.form.get('subject')
    item_description = request.form.get('item_description')
    amount_net = Decimal(request.form.get('amount_net', '0.00'))
    tax_rate = Decimal(request.form.get('tax_rate', '19.00'))

    try:
        tx = create_invoice_via_cockpit(
            client_name=client_name,
            client_email=client_email,
            subject=subject,
            item_description=item_description,
            amount_net=amount_net,
            tax_rate=tax_rate
        )
        flash(f"Rechnung erfolgreich erstellt ({tx.external_id}).", 'success')
    except Exception as e:
        flash(f"Fehler beim Erstellen der Rechnung: {str(e)}", 'danger')

    return redirect(url_for('main.dashboard'))

@main_bp.route('/sync/payments', methods=['POST'])
def sync_payments_route():
    results = poll_payments()
    if results.get('errors'):
        for err in results['errors']:
            flash(err, 'danger')
    flash(f"Zahlungsabgleich abgeschlossen: {results.get('matched_invoices', 0)} Rechnungen gematcht, {results.get('fees_created', 0)} Gebühren erfasst.", 'success')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/update', methods=['POST'])
def update_transaction(id):
    tx = db.session.get(Transaction, id)
    if not tx:
        flash('Transaction not found', 'danger')
        return redirect(url_for('main.dashboard'))

    if tx.status.value not in ['review_pending', 'approved']:
        flash('Cannot update locked or cancelled transactions', 'danger')
        return redirect(url_for('main.dashboard'))

    try:
        tx.counterparty = request.form.get('counterparty', tx.counterparty)
        tx.description = request.form.get('description', tx.description)

        if request.form.get('invoice_date'):
            tx.invoice_date = datetime.datetime.strptime(request.form.get('invoice_date'), '%Y-%m-%d').date()

        if request.form.get('payment_date'):
            tx.payment_date = datetime.datetime.strptime(request.form.get('payment_date'), '%Y-%m-%d').date()

        tx.amount_net = request.form.get('amount_net', tx.amount_net)
        tx.tax_rate = request.form.get('tax_rate', tx.tax_rate)
        tx.amount_tax = request.form.get('amount_tax', tx.amount_tax)
        tx.amount_gross = request.form.get('amount_gross', tx.amount_gross)
        tx.tax_case = request.form.get('tax_case', tx.tax_case)
        tx.ustva_kz_base = request.form.get('ustva_kz_base') or None
        tx.ustva_kz_tax = request.form.get('ustva_kz_tax') or None
        tx.euer_line = request.form.get('euer_line') or None
        tx.recurring_10day_rule = 'recurring_10day_rule' in request.form

        db.session.commit()

        if 'save_and_lock' in request.form:
            lock_transaction(tx.id)
            flash('Transaction saved and locked securely.', 'success')
        else:
            flash('Transaction updated successfully.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error updating transaction: {str(e)}', 'danger')

    return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/lock', methods=['POST'])
def lock_tx(id):
    try:
        lock_transaction(id)
        flash('Transaction locked successfully.', 'success')
    except Exception as e:
        flash(f'Error locking transaction: {str(e)}', 'danger')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/cancel', methods=['POST'])
def cancel_tx(id):
    reason = request.form.get('reason', 'User requested cancellation')
    try:
        cancel_transaction(id, reason)
        flash('Transaction cancelled successfully via Storno.', 'success')
    except Exception as e:
        flash(f'Error cancelling transaction: {str(e)}', 'danger')
    return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/mark_paid', methods=['POST'])
def mark_paid(id):
    tx = db.session.get(Transaction, id)
    if not tx:
        flash('Transaction not found', 'danger')
        return redirect(url_for('main.dashboard'))

    payment_date_str = request.form.get('payment_date')
    if payment_date_str:
        payment_date = datetime.datetime.strptime(payment_date_str, '%Y-%m-%d').date()
    else:
        payment_date = datetime.date.today()

    if tx.payment_date is not None and tx.status == TransactionStatus.locked:
        flash('Payment date is already set and transaction is locked.', 'danger')
        return redirect(url_for('main.dashboard'))

    was_locked = tx.status == TransactionStatus.locked

    try:
        tx.payment_date = payment_date

        db.session.add(AuditLog(
            transaction_id=tx.id,
            action='marked_paid',
            details=f'Marked as paid on {payment_date.isoformat()}'
        ))
        db.session.commit()

        if was_locked:
            recalculate_hash_chain(tx.lock_sequence)

        if tx.source_system == SourceSystem.invoice_ninja and current_app.config.get('INVOICE_NINJA_API_TOKEN'):
            try:
                mark_invoiceninja_paid(tx.external_id, tx.amount_gross, tx.payment_date)
            except Exception as ninja_err:
                flash(f'Transaction marked as paid, but failed to sync to Invoice Ninja: {str(ninja_err)}', 'warning')

        flash(f'Transaction {tx.id} marked as paid on {payment_date}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error marking transaction as paid: {str(e)}', 'danger')

    return redirect(url_for('main.dashboard'))

@main_bp.route('/transactions/<int:id>/discard', methods=['POST'])
def discard_tx(id):
    tx = db.session.get(Transaction, id)
    if not tx:
        flash('Transaction not found', 'danger')
        return redirect(url_for('main.dashboard'))

    if tx.status != TransactionStatus.review_pending:
        flash('Only review_pending transactions can be discarded.', 'danger')
        return redirect(url_for('main.dashboard'))

    try:
        tx_id = tx.id
        db.session.add(AuditLog(
            transaction_id=None,
            action='discarded',
            details=f'Discarded draft transaction {tx_id} ({tx.external_id})'
        ))
        db.session.delete(tx)
        db.session.commit()
        flash(f'Transaction {tx_id} discarded.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error discarding transaction: {str(e)}', 'danger')

    return redirect(url_for('main.dashboard'))

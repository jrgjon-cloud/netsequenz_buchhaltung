from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from app.models import Transaction, TransactionDirection, TransactionStatus
from app.services.gobd_ledger import lock_transaction, cancel_transaction, verify_ledger_integrity
from app.services.csv_importer import import_papierkram_csv
from app import db
from sqlalchemy import extract
import datetime

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

    return render_template(
        'dashboard.html',
        transactions=transactions,
        year=year,
        income_net=income_net,
        expense_net=expense_net,
        vat_liability=vat_liability,
        integrity_ok=integrity_ok
    )

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

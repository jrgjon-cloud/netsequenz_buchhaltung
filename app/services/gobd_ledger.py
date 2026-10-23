import hashlib
from datetime import datetime
from sqlalchemy import event
from sqlalchemy.orm import object_session
from app import db
from app.models import Transaction, TransactionStatus, AuditLog

def calculate_transaction_hash(transaction, previous_hash):
    """
    Calculates a deterministic SHA-256 hash for a transaction.
    """
    # Create a canonical string representation of the transaction
    canonical_string = f"{transaction.id}|{transaction.source_system.value}|{transaction.external_id}|" \
                       f"{transaction.direction.value}|{transaction.invoice_date.isoformat()}|" \
                       f"{transaction.payment_date.isoformat() if transaction.payment_date else ''}|" \
                       f"{transaction.counterparty}|{transaction.description}|" \
                       f"{transaction.amount_net}|{transaction.tax_rate}|{transaction.amount_tax}|" \
                       f"{transaction.amount_gross}|{transaction.tax_case}|{previous_hash or ''}"

    return hashlib.sha256(canonical_string.encode('utf-8')).hexdigest()


def lock_transaction(transaction_id):
    """
    Locks a transaction, calculating its hash and linking it to the previous locked transaction.
    """
    transaction = Transaction.query.get(transaction_id)
    if not transaction:
        raise ValueError("Transaction not found")

    if transaction.status == TransactionStatus.locked:
        raise ValueError("Transaction is already locked")

    # Check required fields for locking (e.g., payment date is required for Ist-Versteuerung mostly,
    # but based on requirements let's assume standard fields are there).
    required_fields = ['source_system', 'external_id', 'direction', 'invoice_date', 'counterparty',
                       'description', 'amount_net', 'tax_rate', 'amount_tax', 'amount_gross', 'tax_case']
    for field in required_fields:
        if getattr(transaction, field) is None:
            raise ValueError(f"Missing required field: {field}")

    # Find the last locked transaction to get the previous hash
    last_locked = Transaction.query.filter_by(status=TransactionStatus.locked)\
                                   .order_by(Transaction.locked_at.desc())\
                                   .first()

    previous_hash = last_locked.record_hash if last_locked else None

    # Update transaction
    transaction.status = TransactionStatus.locked
    transaction.locked_at = datetime.utcnow()
    transaction.previous_hash = previous_hash
    transaction.record_hash = calculate_transaction_hash(transaction, previous_hash)

    # Log the action
    audit_log = AuditLog(
        transaction_id=transaction.id,
        action='locked',
        details=f'Transaction locked with hash {transaction.record_hash}'
    )
    db.session.add(audit_log)
    db.session.commit()

    return transaction


def verify_ledger_integrity():
    """
    Iterates through all locked transactions and verifies the hash chain.
    """
    locked_transactions = Transaction.query.filter_by(status=TransactionStatus.locked)\
                                           .order_by(Transaction.locked_at.asc())\
                                           .all()

    expected_previous_hash = None
    for tx in locked_transactions:
        # Check if previous hash matches the expected one
        if tx.previous_hash != expected_previous_hash:
            raise ValueError(f"Integrity check failed at Transaction {tx.id}: Invalid previous hash")

        # Check if current hash is correct
        calculated_hash = calculate_transaction_hash(tx, tx.previous_hash)
        if tx.record_hash != calculated_hash:
            raise ValueError(f"Integrity check failed at Transaction {tx.id}: Invalid record hash")

        expected_previous_hash = tx.record_hash

    return True


def cancel_transaction(transaction_id, reason):
    """
    Cancels a locked transaction by creating a reverse storno transaction and locking it immediately.
    """
    original_tx = Transaction.query.get(transaction_id)
    if not original_tx:
        raise ValueError("Transaction not found")

    if original_tx.status != TransactionStatus.locked:
        raise ValueError("Only locked transactions can be cancelled via storno")

    # Create storno transaction with negative amounts
    storno_tx = Transaction(
        source_system=original_tx.source_system,
        external_id=f"{original_tx.external_id}_STORNO",
        direction=original_tx.direction,
        status=TransactionStatus.review_pending,
        invoice_date=datetime.utcnow().date(), # or original_tx.invoice_date depending on accounting rules
        payment_date=datetime.utcnow().date(),
        counterparty=original_tx.counterparty,
        description=f"STORNO: {original_tx.description} ({reason})",
        amount_net=-original_tx.amount_net,
        tax_rate=original_tx.tax_rate,
        amount_tax=-original_tx.amount_tax,
        amount_gross=-original_tx.amount_gross,
        tax_case=original_tx.tax_case,
        ustva_kz_base=original_tx.ustva_kz_base,
        ustva_kz_tax=original_tx.ustva_kz_tax,
        euer_line=original_tx.euer_line,
        recurring_10day_rule=original_tx.recurring_10day_rule,
        storno_of_id=original_tx.id
    )

    db.session.add(storno_tx)
    db.session.commit()

    # Log storno creation
    db.session.add(AuditLog(
        transaction_id=original_tx.id,
        action='storno_created',
        details=f'Storno transaction {storno_tx.id} created. Reason: {reason}'
    ))
    db.session.commit()

    # Lock the storno transaction immediately
    return lock_transaction(storno_tx.id)


# SQLAlchemy Event Listeners to protect locked records
@event.listens_for(Transaction, 'before_update')
def receive_before_update(mapper, connection, target):
    session = object_session(target)
    # Check if the state before the update had status locked
    if session.is_modified(target):
        # We need to look at the database state or history
        state = db.inspect(target)
        status_history = state.attrs.status.history

        # If it's already locked and we are not just locking it now
        if target.status == TransactionStatus.locked and not status_history.added:
            # Check which fields are modified
            protected_fields = ['amount_net', 'tax_rate', 'amount_tax', 'amount_gross',
                                'invoice_date', 'payment_date', 'tax_case', 'direction']

            for field in protected_fields:
                history = getattr(state.attrs, field).history
                if history.has_changes():
                    raise ValueError(f"Cannot modify financial/tax field '{field}' of a locked transaction")

@event.listens_for(Transaction, 'before_delete')
def receive_before_delete(mapper, connection, target):
    if target.status == TransactionStatus.locked:
        raise ValueError("Cannot delete a locked transaction")

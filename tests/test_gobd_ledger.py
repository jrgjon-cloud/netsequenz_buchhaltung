import pytest
from datetime import date
import sys
import os

# Add parent directory to path so tests can find app
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from app.config import TestConfig
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from app.services.gobd_ledger import lock_transaction, verify_ledger_integrity, cancel_transaction

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

def test_create_transaction(app):
    tx = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    db.session.add(tx)
    db.session.commit()

    saved_tx = Transaction.query.filter_by(external_id='INV-001').first()
    assert saved_tx is not None
    assert saved_tx.status == TransactionStatus.review_pending

def test_lock_transaction(app):
    tx1 = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services 1',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    db.session.add(tx1)
    db.session.commit()

    # Lock first transaction
    locked_tx1 = lock_transaction(tx1.id)
    assert locked_tx1.status == TransactionStatus.locked
    assert locked_tx1.record_hash is not None
    assert locked_tx1.previous_hash is None

    tx2 = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-002',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 2),
        counterparty='Test Client',
        description='Consulting Services 2',
        amount_net=2000.00,
        tax_rate=19.00,
        amount_tax=380.00,
        amount_gross=2380.00,
        tax_case='standard_19'
    )
    db.session.add(tx2)
    db.session.commit()

    # Lock second transaction
    locked_tx2 = lock_transaction(tx2.id)
    assert locked_tx2.status == TransactionStatus.locked
    assert locked_tx2.previous_hash == locked_tx1.record_hash
    assert locked_tx2.record_hash is not None

def test_verify_ledger_integrity(app):
    tx1 = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services 1',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    tx2 = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-002',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 2),
        counterparty='Test Client',
        description='Consulting Services 2',
        amount_net=2000.00,
        tax_rate=19.00,
        amount_tax=380.00,
        amount_gross=2380.00,
        tax_case='standard_19'
    )
    db.session.add_all([tx1, tx2])
    db.session.commit()

    lock_transaction(tx1.id)
    lock_transaction(tx2.id)

    assert verify_ledger_integrity() is True

    # Simulate manipulation
    manipulated_tx = Transaction.query.get(tx1.id)
    manipulated_tx.record_hash = 'invalidhash'
    db.session.commit()

    with pytest.raises(ValueError, match="Integrity check failed"):
        verify_ledger_integrity()

def test_prevent_modification_of_locked_transaction(app):
    tx = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    db.session.add(tx)
    db.session.commit()

    lock_transaction(tx.id)

    locked_tx = Transaction.query.get(tx.id)
    locked_tx.amount_net = 9999.99

    with pytest.raises(ValueError, match="Cannot modify financial/tax field"):
        db.session.commit()

def test_prevent_deletion_of_locked_transaction(app):
    tx = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    db.session.add(tx)
    db.session.commit()

    lock_transaction(tx.id)

    locked_tx = Transaction.query.get(tx.id)
    db.session.delete(locked_tx)

    with pytest.raises(ValueError, match="Cannot delete a locked transaction"):
        db.session.commit()

def test_cancel_transaction(app):
    tx = Transaction(
        source_system=SourceSystem.invoice_ninja,
        external_id='INV-001',
        direction=TransactionDirection.income,
        invoice_date=date(2023, 10, 1),
        counterparty='Test Client',
        description='Consulting Services',
        amount_net=1000.00,
        tax_rate=19.00,
        amount_tax=190.00,
        amount_gross=1190.00,
        tax_case='standard_19'
    )
    db.session.add(tx)
    db.session.commit()

    lock_transaction(tx.id)

    storno_tx = cancel_transaction(tx.id, reason="Wrong amount")

    assert storno_tx.status == TransactionStatus.locked
    assert storno_tx.storno_of_id == tx.id
    assert storno_tx.amount_net == -1000.00
    assert storno_tx.amount_gross == -1190.00
    assert "STORNO" in storno_tx.description

    # Verify ledger integrity holds after storno
    assert verify_ledger_integrity() is True

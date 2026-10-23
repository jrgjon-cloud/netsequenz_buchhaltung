from app import db
import enum
from datetime import datetime

class SourceSystem(enum.Enum):
    invoice_ninja = 'invoice_ninja'
    paperless = 'paperless'
    papierkram_legacy = 'papierkram_legacy'
    manual = 'manual'

class TransactionDirection(enum.Enum):
    income = 'income'
    expense = 'expense'

class TransactionStatus(enum.Enum):
    review_pending = 'review_pending'
    approved = 'approved'
    locked = 'locked'
    cancelled = 'cancelled'

class Transaction(db.Model):
    __tablename__ = 'transactions'

    id = db.Column(db.Integer, primary_key=True)
    source_system = db.Column(db.Enum(SourceSystem), nullable=False)
    external_id = db.Column(db.String, nullable=False)
    direction = db.Column(db.Enum(TransactionDirection), nullable=False)
    status = db.Column(db.Enum(TransactionStatus), default=TransactionStatus.review_pending, nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    payment_date = db.Column(db.Date, nullable=True)
    counterparty = db.Column(db.String, nullable=False)
    description = db.Column(db.String, nullable=False)
    amount_net = db.Column(db.Numeric(10, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False)
    amount_tax = db.Column(db.Numeric(10, 2), nullable=False)
    amount_gross = db.Column(db.Numeric(10, 2), nullable=False)
    tax_case = db.Column(db.String, nullable=False)
    ustva_kz_base = db.Column(db.String, nullable=True)
    ustva_kz_tax = db.Column(db.String, nullable=True)
    euer_line = db.Column(db.String, nullable=True)
    recurring_10day_rule = db.Column(db.Boolean, default=False, nullable=False)
    document_sha256 = db.Column(db.String(64), nullable=True)

    storno_of_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    previous_hash = db.Column(db.String(64), nullable=True)
    record_hash = db.Column(db.String(64), unique=True, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('source_system', 'external_id', name='uq_source_external_id'),
    )

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey('transactions.id'), nullable=True)
    action = db.Column(db.String, nullable=False)  # e.g., 'created', 'status_changed', 'locked', 'exported'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    details = db.Column(db.String, nullable=True)  # JSON string or plain text describing the change

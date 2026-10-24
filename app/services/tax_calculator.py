from typing import Dict, Any
from decimal import Decimal
from datetime import date
from sqlalchemy import extract
from app.models import Transaction, TransactionDirection, TransactionStatus
from app import db

def calculate_elster_metrics(year: int, period: str = 'year', only_locked: bool = False) -> Dict[str, Any]:
    """
    Calculates the required metrics for the German ELSTER tax declaration (UStVA & EÜR).
    Follows strict Date Logic per § 11 EStG (Zufluss-/Abflussprinzip & 10-day rule).
    """

    query = Transaction.query.filter(Transaction.status != TransactionStatus.cancelled)

    if only_locked:
        query = query.filter(Transaction.status == TransactionStatus.locked)

    transactions = query.all()

    # We will compute the metrics using Python for precise 10-day rule handling and period filtering

    # UStVA Kennziffern
    kz_81_base = Decimal('0.00')
    kz_81_tax = Decimal('0.00')
    kz_86_base = Decimal('0.00')
    kz_86_tax = Decimal('0.00')
    kz_46_base = Decimal('0.00')
    kz_66_vorsteuer = Decimal('0.00')

    # EÜR Lines
    euer_income_net = Decimal('0.00')
    euer_vat_collected = Decimal('0.00')
    euer_vat_refunded = Decimal('0.00')
    euer_vat_paid = Decimal('0.00')

    euer_expenses_by_line = {
        'Wareneingang': Decimal('0.00'),
        'Fremdleistungen': Decimal('0.00'),
        'Rechts- und Beratungskosten': Decimal('0.00'),
        'Buchführungskosten': Decimal('0.00'),
        'Bürobedarf': Decimal('0.00'),
        'Nebenkosten des Geldverkehrs': Decimal('0.00'),
        'Sonstige unbeschränkt abziehbare Betriebsausgaben': Decimal('0.00'),
    }

    unpaid_count = 0
    total_income_gross = Decimal('0.00')
    total_expense_gross = Decimal('0.00')

    for tx in transactions:
        # Determine the effective date for tax purposes
        effective_date = tx.payment_date

        if not effective_date:
            unpaid_count += 1
            effective_date = tx.invoice_date

        # 10-day rule logic
        if tx.recurring_10day_rule:
            if effective_date.month == 1 and effective_date.day <= 10:
                if tx.invoice_date.month == 12 and tx.invoice_date.day >= 22 and tx.invoice_date.year == effective_date.year - 1:
                    effective_date = date(effective_date.year - 1, 12, 31)
            elif effective_date.month == 12 and effective_date.day >= 22:
                if tx.invoice_date.month == 1 and tx.invoice_date.day <= 10 and tx.invoice_date.year == effective_date.year + 1:
                    effective_date = date(effective_date.year + 1, 1, 1)

        # Check if the transaction falls into the requested period
        if effective_date.year != year:
            continue

        if period != 'year':
            if period.startswith('Q'):
                quarter = int(period[1])
                tx_quarter = (effective_date.month - 1) // 3 + 1
                if tx_quarter != quarter:
                    continue
            else:
                try:
                    month = int(period)
                    if effective_date.month != month:
                        continue
                except ValueError:
                    pass

        # Calculate UStVA Kennziffern
        if tx.direction == TransactionDirection.income:
            total_income_gross += tx.amount_gross
            euer_vat_collected += tx.amount_tax

            if tx.tax_case == 'ust_refund':
                euer_vat_refunded += tx.amount_net
            else:
                euer_income_net += tx.amount_net

            if tx.ustva_kz_base == '81' or tx.tax_case == 'standard_19':
                kz_81_base += tx.amount_net
                kz_81_tax += tx.amount_tax
            elif tx.ustva_kz_base == '86' or tx.tax_case == 'reduced_7':
                kz_86_base += tx.amount_net
                kz_86_tax += tx.amount_tax

        elif tx.direction == TransactionDirection.expense:
            total_expense_gross += tx.amount_gross
            euer_vat_paid += tx.amount_tax

            if tx.tax_case in ['reverse_charge_13b_eu', 'eu_expense_13b_check'] or tx.ustva_kz_base == '46':
                kz_46_base += tx.amount_net

            if tx.ustva_kz_tax == '66':
                kz_66_vorsteuer += tx.amount_tax

            if tx.euer_line in euer_expenses_by_line:
                euer_expenses_by_line[tx.euer_line] += tx.amount_net
            else:
                euer_expenses_by_line['Sonstige unbeschränkt abziehbare Betriebsausgaben'] += tx.amount_net

    kz_47_tax = round(kz_46_base * Decimal('0.19'), 2)
    kz_67_vorsteuer_13b = kz_47_tax
    kz_83_zahllast = (kz_81_tax + kz_86_tax + kz_47_tax) - (kz_66_vorsteuer + kz_67_vorsteuer_13b)

    euer_profit = total_income_gross - total_expense_gross

    return {
        'unpaid_count': unpaid_count,

        'kz_81_base': kz_81_base,
        'kz_81_tax': kz_81_tax,
        'kz_86_base': kz_86_base,
        'kz_86_tax': kz_86_tax,
        'kz_46_base': kz_46_base,
        'kz_47_tax': kz_47_tax,
        'kz_66_vorsteuer': kz_66_vorsteuer,
        'kz_67_vorsteuer_13b': kz_67_vorsteuer_13b,
        'kz_83_zahllast': kz_83_zahllast,

        'euer_income_net': euer_income_net,
        'euer_vat_collected': euer_vat_collected,
        'euer_vat_refunded': euer_vat_refunded,
        'euer_expenses_by_line': euer_expenses_by_line,
        'euer_vat_paid': euer_vat_paid,
        'euer_profit': euer_profit
    }

import requests
from decimal import Decimal
from app import db
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus, AuditLog
from flask import current_app
import dateutil.parser
from datetime import datetime

def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return dateutil.parser.parse(date_str).date()
    except:
        return None

def poll_payments(gocardless_token=None, sumup_key=None):
    if gocardless_token is None:
        gocardless_token = current_app.config.get('GOCARDLESS_ACCESS_TOKEN', '')
    if sumup_key is None:
        sumup_key = current_app.config.get('SUMUP_API_KEY', '')

    gocardless_env = current_app.config.get('GOCARDLESS_ENV', 'live')
    gc_base_url = 'https://api.gocardless.com' if gocardless_env == 'live' else 'https://api-sandbox.gocardless.com'

    results = {"matched_invoices": 0, "fees_created": 0, "errors": []}

    # 1. GoCardless Polling
    if gocardless_token:
        gc_headers = {
            'Authorization': f'Bearer {gocardless_token}',
            'GoCardless-Version': '2015-07-06',
            'Accept': 'application/json'
        }

        # Check Payments
        try:
            res_pay = requests.get(f"{gc_base_url}/payments?limit=100", headers=gc_headers)
            if res_pay.status_code == 200:
                payments = res_pay.json().get('payments', [])
                for payment in payments:
                    status = payment.get('status')
                    if status in ['confirmed', 'paid_out']:
                        amount_eur = Decimal(str(payment.get('amount', 0))) / Decimal('100')
                        charge_date = _parse_date(payment.get('charge_date'))
                        desc = payment.get('description', '')
                        meta = payment.get('metadata', {})
                        invoice_number = meta.get('invoice_number', '')

                        # Find open invoice
                        open_invoices = Transaction.query.filter_by(
                            direction=TransactionDirection.income,
                            payment_date=None
                        ).all()

                        matched_tx = None
                        for tx in open_invoices:
                            if invoice_number and invoice_number in tx.external_id:
                                matched_tx = tx
                                break
                            if tx.external_id in desc:
                                matched_tx = tx
                                break
                            if tx.amount_gross == amount_eur:
                                matched_tx = tx
                                break

                        if matched_tx and charge_date:
                            matched_tx.payment_date = charge_date
                            db.session.add(AuditLog(transaction_id=matched_tx.id, action='payment_matched', details=f'GoCardless payment {payment["id"]}'))
                            db.session.commit()
                            results["matched_invoices"] += 1

            # Check Payouts for fees
            res_pout = requests.get(f"{gc_base_url}/payouts?limit=100", headers=gc_headers)
            if res_pout.status_code == 200:
                payouts = res_pout.json().get('payouts', [])
                for payout in payouts:
                    deducted_fees = Decimal(str(payout.get('deducted_fees', 0))) / Decimal('100')
                    if deducted_fees > 0:
                        payout_id = payout.get('id')
                        fee_ext_id = f"GC-FEE-{payout_id}"

                        existing_fee = Transaction.query.filter_by(external_id=fee_ext_id).first()
                        if not existing_fee:
                            fee_tx = Transaction(
                                source_system=SourceSystem.manual,
                                external_id=fee_ext_id,
                                direction=TransactionDirection.expense,
                                status=TransactionStatus.review_pending,
                                invoice_date=_parse_date(payout.get('created_at')) or datetime.utcnow().date(),
                                payment_date=_parse_date(payout.get('created_at')) or datetime.utcnow().date(),
                                counterparty='GoCardless SAS',
                                description=f"GoCardless Gebühren Payout {payout_id}",
                                amount_net=deducted_fees,
                                tax_rate=Decimal('0.00'),
                                amount_tax=Decimal('0.00'),
                                amount_gross=deducted_fees,
                                tax_case='eu_expense_13b_check',
                                euer_line='Nebenkosten des Geldverkehrs'
                            )
                            db.session.add(fee_tx)
                            db.session.add(AuditLog(action='fee_created', details=f'GoCardless fee {fee_ext_id}'))
                            db.session.commit()
                            results["fees_created"] += 1
        except Exception as e:
            results["errors"].append(f"GoCardless error: {str(e)}")

    # (Sumup to be implemented in next step)



    # 2. SumUp Polling
    if sumup_key:
        su_headers = {
            'Authorization': f'Bearer {sumup_key}',
            'Accept': 'application/json'
        }

        try:
            res_su = requests.get("https://api.sumup.com/v0.1/me/transactions/history?status=SUCCESSFUL", headers=su_headers)
            if res_su.status_code == 200:
                su_transactions = res_su.json().get('items', [])
                for su_tx in su_transactions:
                    tx_code = su_tx.get('transaction_code')
                    amount = Decimal(str(su_tx.get('amount', 0)))
                    fee_amount = Decimal(str(su_tx.get('fee_amount', 0)))
                    tx_date = _parse_date(su_tx.get('timestamp'))
                    product_summary = su_tx.get('product_summary', '')

                    # Match open invoice
                    open_invoices = Transaction.query.filter_by(
                        direction=TransactionDirection.income,
                        payment_date=None
                    ).all()

                    matched_tx = None
                    for tx in open_invoices:
                        if product_summary and tx.external_id in product_summary:
                            matched_tx = tx
                            break
                        if tx.amount_gross == amount:
                            matched_tx = tx
                            break

                    if matched_tx and tx_date:
                        matched_tx.payment_date = tx_date
                        db.session.add(AuditLog(transaction_id=matched_tx.id, action='payment_matched', details=f'SumUp transaction {tx_code}'))
                        db.session.commit()
                        results["matched_invoices"] += 1

                    # Create fee if any
                    if fee_amount > 0:
                        fee_ext_id = f"SUMUP-FEE-{tx_code}"
                        existing_fee = Transaction.query.filter_by(external_id=fee_ext_id).first()
                        if not existing_fee:
                            fee_tx = Transaction(
                                source_system=SourceSystem.manual,
                                external_id=fee_ext_id,
                                direction=TransactionDirection.expense,
                                status=TransactionStatus.review_pending,
                                invoice_date=tx_date or datetime.utcnow().date(),
                                payment_date=tx_date or datetime.utcnow().date(),
                                counterparty='SumUp',
                                description=f"SumUp Gebühren {tx_code}",
                                amount_net=fee_amount,
                                tax_rate=Decimal('0.00'),
                                amount_tax=Decimal('0.00'),
                                amount_gross=fee_amount,
                                tax_case='eu_expense_13b_check',
                                euer_line='Nebenkosten des Geldverkehrs'
                            )
                            db.session.add(fee_tx)
                            db.session.add(AuditLog(action='fee_created', details=f'SumUp fee {fee_ext_id}'))
                            db.session.commit()
                            results["fees_created"] += 1
        except Exception as e:
            results["errors"].append(f"SumUp error: {str(e)}")


    return results

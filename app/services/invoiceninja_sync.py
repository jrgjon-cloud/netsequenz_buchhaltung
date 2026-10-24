import requests
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from app import db
from app.models import Transaction, SourceSystem, TransactionDirection, TransactionStatus
from flask import current_app
import hashlib
import dateutil.parser
import json

def _parse_date(date_str):
    if not date_str:
        return None
    try:
        return dateutil.parser.parse(date_str).date()
    except:
        return None

def fetch_invoiceninja_pdf(external_id, base_url=None, api_token=None):
    """
    Lädt das PDF zu einer Rechnung aus Invoice Ninja herunter.
    external_id is the invoice number (e.g. R-00209). We need to find the ID first.
    Or maybe external_id here means the internal Invoice Ninja ID?
    The prompt for fetch_invoiceninja_pdf: "Lädt das PDF zu einer Rechnung aus Invoice Ninja herunter"
    Let's assume the ID is the internal ID, or we have to query it. We stored external_id as `number` (R-00209) in sync.
    So we'll fetch invoices?number=R-00209 to get the ID, then download.
    """
    if base_url is None:
        base_url = current_app.config.get('INVOICE_NINJA_URL', 'http://invoiceninja-web:80')
    if api_token is None:
        api_token = current_app.config.get('INVOICE_NINJA_API_TOKEN', '')

    headers = {
        'X-API-Token': api_token,
        'X-Requested-With': 'XMLHttpRequest'
    }

    # 1. find the invoice ID by number
    res = requests.get(f"{base_url}/api/v1/invoices?number={external_id}", headers=headers)
    if res.status_code == 200:
        data = res.json().get('data', [])
        if data:
            inv_id = data[0].get('id')
            if inv_id:
                # 2. download PDF
                pdf_res = requests.get(f"{base_url}/api/v1/invoices/{inv_id}/download", headers=headers)
                if pdf_res.status_code == 200:
                    return pdf_res.content
    return b""

def sync_from_invoiceninja(base_url=None, api_token=None):
    if base_url is None:
        base_url = current_app.config.get('INVOICE_NINJA_URL', 'http://invoiceninja-web:80')
    if api_token is None:
        api_token = current_app.config.get('INVOICE_NINJA_API_TOKEN', '')

    headers = {
        'X-API-Token': api_token,
        'X-Requested-With': 'XMLHttpRequest'
    }

    try:
        response = requests.get(f"{base_url}/api/v1/invoices?include=client,payments", headers=headers)
        response.raise_for_status()
        data = response.json().get('data', [])
    except Exception as e:
        return {"created": 0, "updated": 0, "errors": [str(e)]}

    results = {"created": 0, "updated": 0, "errors": []}

    for inv in data:
        try:
            status_id = str(inv.get('status_id', ''))
            number = inv.get('number', '')

            # "Ignoriert reine Entwürfe (status_id == '1'), sofern sie noch keine Rechnungsnummer haben oder nicht versendet/gesperrt sind."
            if status_id == '1' and not number:
                continue

            external_id = number
            if not external_id:
                continue

            amount_gross = Decimal(str(inv.get('amount', '0.00')))

            # Extract line items to calculate proper tax rate
            line_items = inv.get('line_items', [])
            tax_rate_val = Decimal('0.00')
            if line_items:
                # just pick the first item's tax rate
                tr = str(line_items[0].get('tax_rate1', '0.00'))
                tax_rate_val = Decimal(tr)

            # Normalisiere auf 19, 7 oder 0
            if tax_rate_val >= Decimal('19'):
                tax_rate = Decimal('19.00')
                tax_case = 'standard_19'
                ustva_kz_base = '81'
            elif tax_rate_val > Decimal('0') and tax_rate_val <= Decimal('7'):
                tax_rate = Decimal('7.00')
                tax_case = 'reduced_7'
                ustva_kz_base = '86'
            else:
                tax_rate = Decimal('0.00')
                tax_case = 'tax_free'
                ustva_kz_base = 'tax_free'

            # Berechne Netto und Steuer
            # If amount is gross, net = gross / (1 + rate/100)
            amount_net = (amount_gross / (Decimal('1') + tax_rate / Decimal('100'))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            amount_tax = amount_gross - amount_net

            invoice_date = _parse_date(inv.get('date'))
            if not invoice_date:
                continue

            # Check payment status
            balance = Decimal(str(inv.get('balance', '0.00')))
            payment_date = None
            payments = inv.get('payments', [])

            if balance == Decimal('0.00') and payments:
                # Find the latest payment date
                pay_dates = [_parse_date(p.get('date')) for p in payments if _parse_date(p.get('date'))]
                if pay_dates:
                    payment_date = max(pay_dates)

            client = inv.get('client', {})
            counterparty = client.get('name', 'Unbekannt')

            # Build description from line items if available, or just generic
            desc_parts = [item.get('notes', '') for item in line_items if item.get('notes')]
            description = ", ".join(desc_parts) if desc_parts else f"Rechnung {number}"

            existing_tx = Transaction.query.filter_by(
                source_system=SourceSystem.invoice_ninja,
                external_id=external_id
            ).first()

            pdf_bytes = fetch_invoiceninja_pdf(external_id, base_url, api_token)
            document_sha256 = None
            if pdf_bytes:
                document_sha256 = hashlib.sha256(pdf_bytes).hexdigest()

            if existing_tx:
                updated = False
                if existing_tx.status == TransactionStatus.review_pending:
                    # Update payment date if newly paid
                    if not existing_tx.payment_date and payment_date:
                        existing_tx.payment_date = payment_date
                        updated = True

                    if document_sha256 and existing_tx.document_sha256 != document_sha256:
                        existing_tx.document_sha256 = document_sha256
                        updated = True

                if updated:
                    db.session.commit()
                    results["updated"] += 1
            else:
                new_tx = Transaction(
                    source_system=SourceSystem.invoice_ninja,
                    external_id=external_id,
                    direction=TransactionDirection.income,
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
                    document_sha256=document_sha256
                )
                db.session.add(new_tx)
                db.session.commit()
                results["created"] += 1

        except Exception as e:
            db.session.rollback()
            results["errors"].append(f"Error processing invoice {inv.get('number')}: {str(e)}")

    return results


def mark_invoiceninja_paid(external_id: str, amount: Decimal, payment_date: datetime.date, base_url=None, api_token=None):
    """
    Marks an invoice as paid in Invoice Ninja via the Payments API.
    """
    url, headers = get_invoiceninja_client(base_url, api_token)
    if not url or not headers.get('X-Api-Token'):
        return None

    # external_id is usually something like 'INV-001' or 'R-00209'
    # We need to find the internal invoice ID first
    invoices_resp = requests.get(f"{url}/api/v1/invoices?number={external_id}", headers=headers)
    invoices_resp.raise_for_status()
    invoices_data = invoices_resp.json().get('data', [])

    if not invoices_data:
        raise ValueError(f"Invoice {external_id} not found in Invoice Ninja")

    invoice_id = invoices_data[0]['id']

    payload = {
        "amount": float(amount),
        "date": payment_date.strftime("%Y-%m-%d"),
        "invoices": [
            {
                "invoice_id": invoice_id,
                "amount": float(amount)
            }
        ]
    }

    resp = requests.post(f"{url}/api/v1/payments", json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()

def create_invoice_via_cockpit(client_name, client_email, subject, item_description, amount_net, tax_rate=Decimal('19.00'), due_days=14, base_url=None, api_token=None):
    if base_url is None:
        base_url = current_app.config.get('INVOICE_NINJA_URL', 'http://invoiceninja-web:80')
    if api_token is None:
        api_token = current_app.config.get('INVOICE_NINJA_API_TOKEN', '')

    # Check if API token is given -> Live sync with Invoice Ninja
    if api_token:
        headers = {
            'X-API-Token': api_token,
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/json'
        }

        # 1. Find or create client
        client_res = requests.get(f"{base_url}/api/v1/clients?email={client_email}", headers=headers)
        client_id = None
        if client_res.status_code == 200:
            clients = client_res.json().get('data', [])
            if clients:
                client_id = clients[0]['id']

        if not client_id:
            # Create client
            client_payload = {
                "name": client_name,
                "contacts": [{"email": client_email, "send_email": True}]
            }
            c_res = requests.post(f"{base_url}/api/v1/clients", json=client_payload, headers=headers)
            c_res.raise_for_status()
            client_id = c_res.json()['data']['id']

        # 2. Create invoice
        # Convert tax rate back to standard for Ninja (e.g. "19.00")
        tax_rate_str = str(tax_rate)

        inv_payload = {
            "client_id": client_id,
            "date": datetime.utcnow().strftime("%Y-%m-%d"),
            "due_date": (datetime.utcnow() + timedelta(days=due_days)).strftime("%Y-%m-%d") if due_days else None, # We need timedelta!
            "line_items": [
                {
                    "notes": item_description,
                    "cost": float(amount_net),
                    "qty": 1,
                    "tax_rate1": float(tax_rate),
                    "tax_name1": f"MwSt {tax_rate}%" if float(tax_rate) > 0 else "Steuerfrei"
                }
            ]
        }

        # Post with ?mark_sent=true
        i_res = requests.post(f"{base_url}/api/v1/invoices?mark_sent=true", json=inv_payload, headers=headers)
        i_res.raise_for_status()
        inv_data = i_res.json()['data']

        # We need to trigger sync to fetch PDF and create tx
        sync_from_invoiceninja(base_url, api_token)

        # return the created transaction
        tx = Transaction.query.filter_by(source_system=SourceSystem.invoice_ninja, external_id=inv_data['number']).first()
        return tx

    else:
        # Lokaler Offline-Modus
        # "Falls kein api_token gesetzt ist (lokaler Offline-Modus): Legt die Ausgangsrechnung mit fortlaufender Nummer direkt als Transaction im Dashboard an"
        import datetime as dt_module
        # Find next offline number
        count = Transaction.query.filter_by(source_system=SourceSystem.invoice_ninja).count()
        offline_number = f"OFFLINE-{count + 1:04d}"

        tax_rate_dec = Decimal(str(tax_rate))
        amount_net_dec = Decimal(str(amount_net))
        amount_tax = (amount_net_dec * tax_rate_dec / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        amount_gross = amount_net_dec + amount_tax

        if tax_rate_dec >= Decimal('19'):
            tax_case = 'standard_19'
            ustva_kz_base = '81'
        elif tax_rate_dec > Decimal('0'):
            tax_case = 'reduced_7'
            ustva_kz_base = '86'
        else:
            tax_case = 'tax_free'
            ustva_kz_base = 'tax_free'

        tx = Transaction(
            source_system=SourceSystem.invoice_ninja,
            external_id=offline_number,
            direction=TransactionDirection.income,
            status=TransactionStatus.review_pending,
            invoice_date=dt_module.date.today(),
            counterparty=client_name,
            description=subject + " / " + item_description,
            amount_net=amount_net_dec,
            tax_rate=tax_rate_dec,
            amount_tax=amount_tax,
            amount_gross=amount_gross,
            tax_case=tax_case,
            ustva_kz_base=ustva_kz_base
        )
        db.session.add(tx)
        db.session.commit()
        return tx

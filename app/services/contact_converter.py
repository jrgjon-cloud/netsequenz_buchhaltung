import csv
import io

def convert_papierkram_contacts_to_invoiceninja(csv_stream_or_text):
    if isinstance(csv_stream_or_text, bytes):
        content = csv_stream_or_text.decode('utf-8')
    elif hasattr(csv_stream_or_text, 'read'):
        content = csv_stream_or_text.read()
        if isinstance(content, bytes):
            content = content.decode('utf-8')
    else:
        content = csv_stream_or_text

    # Auto-detect delimiter
    first_line = content.split('\n')[0] if '\n' in content else content
    delimiter = ';' if ';' in first_line else ','

    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)

    headers = reader.fieldnames
    if not headers:
        return ""

    out_csv = io.StringIO()
    writer = csv.writer(out_csv, delimiter=',', quoting=csv.QUOTE_MINIMAL)

    # Invoice Ninja v5 expected columns
    out_headers = [
        'Number', 'Name', 'First Name', 'Last Name', 'Email', 'Phone',
        'Address 1', 'Postal Code', 'City', 'Country', 'VAT Number',
        'Custom Value 1 (IBAN)', 'Custom Value 2 (BIC)', 'Custom Value 3 (SEPA Mandate)', 'Public Notes'
    ]
    writer.writerow(out_headers)

    for row in reader:
        # Check if it's a customer
        kontaktart = row.get('Kontaktart', '')
        if kontaktart and 'kunde' not in kontaktart.lower():
            continue

        number = row.get('Kunden-/Lieferantennr.', '')
        name = row.get('Name', '')
        first_name = row.get('Vorname (Standardempfänger)', '')
        last_name = row.get('Nachname (Standardempfänger)', '')
        email = row.get('E-Mail', '')
        phone = row.get('Telefon', '')
        address1 = row.get('Straße (Post)', '')
        postal_code = row.get('PLZ (Post)', '')
        city = row.get('Stadt (Post)', '')
        country = row.get('Land (Post)', '')
        vat_number = row.get('UStID', '')
        iban = row.get('IBAN', '')
        bic = row.get('BIC', '')
        sepa_mandate = row.get('SEPA-Mandatsreferenz', '')

        sepa_accepted = row.get('SEPA-Lastschrift akzeptiert', '')
        public_notes = f"SEPA akzeptiert: {sepa_accepted}" if sepa_accepted else ""

        writer.writerow([
            number, name, first_name, last_name, email, phone,
            address1, postal_code, city, country, vat_number,
            iban, bic, sepa_mandate, public_notes
        ])

    return out_csv.getvalue()

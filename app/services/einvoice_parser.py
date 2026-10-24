import io
import xml.etree.ElementTree as ET
from decimal import Decimal
from datetime import datetime
from pypdf import PdfReader

def extract_xml_from_pdf(pdf_bytes: bytes) -> bytes | None:
    """
    Extracts embedded XML (ZUGFeRD/Factur-X/XRechnung) from a PDF.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))

    # In pypdf >= 3.0.0, attachments can often be found in reader.attachments
    # which is a dict of filename -> list of attachment contents

    for filename, file_contents in reader.attachments.items():
        filename_lower = filename.lower()
        if 'factur-x.xml' in filename_lower or 'zugferd-invoice.xml' in filename_lower or 'xrechnung.xml' in filename_lower:
            # reader.attachments[filename] is a list of byte strings
            if file_contents:
                return file_contents[0]

    return None

def parse_en16931_xml(xml_bytes: bytes) -> dict:
    """
    Parses EN16931 compliant XML (CII or UBL) and returns a standard dictionary.
    Supports ZUGFeRD / Factur-X / XRechnung.
    """
    root = ET.fromstring(xml_bytes)

    # Check if it's CII or UBL based on root tag
    if 'CrossIndustryInvoice' in root.tag:
        return _parse_cii(root)
    elif 'Invoice' in root.tag and 'urn:oasis:names:specification:ubl' in root.tag:
        return _parse_ubl(root)
    else:
        raise ValueError("Unsupported XML format. Root tag: " + root.tag)

def _parse_cii(root: ET.Element) -> dict:
    namespaces = {
        'rsm': 'urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100',
        'ram': 'urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100',
        'udt': 'urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100'
    }

    result = {
        "invoice_number": None,
        "invoice_date": None,
        "counterparty": None,
        "country": None,
        "amount_net": Decimal('0.00'),
        "amount_tax": Decimal('0.00'),
        "amount_gross": Decimal('0.00'),
        "tax_rate": Decimal('0.00'),
        "is_reverse_charge": False
    }

    # Invoice Number
    doc_node = root.find('.//rsm:ExchangedDocument', namespaces)
    if doc_node is not None:
        id_node = doc_node.find('ram:ID', namespaces)
        if id_node is not None:
            result['invoice_number'] = id_node.text

        # Invoice Date
        date_node = doc_node.find('.//ram:IssueDateTime/udt:DateTimeString', namespaces)
        if date_node is not None and date_node.text:
            date_str = date_node.text
            # Usually format is 102 (YYYYMMDD)
            try:
                result['invoice_date'] = datetime.strptime(date_str, '%Y%m%d').date()
            except ValueError:
                pass # ignore if format doesn't match

    # Seller/Counterparty
    seller_node = root.find('.//ram:SellerTradeParty', namespaces)
    if seller_node is not None:
        name_node = seller_node.find('ram:Name', namespaces)
        if name_node is not None:
            result['counterparty'] = name_node.text

        country_node = seller_node.find('.//ram:PostalTradeAddress/ram:CountryID', namespaces)
        if country_node is not None:
            result['country'] = country_node.text

    # Totals
    settlement_node = root.find('.//ram:ApplicableHeaderTradeSettlement', namespaces)
    if settlement_node is not None:
        monetary_node = settlement_node.find('ram:SpecifiedTradeSettlementHeaderMonetarySummation', namespaces)
        if monetary_node is not None:
            tax_basis_node = monetary_node.find('ram:TaxBasisTotalAmount', namespaces)
            if tax_basis_node is not None and tax_basis_node.text:
                result['amount_net'] = Decimal(tax_basis_node.text)

            tax_total_node = monetary_node.find('ram:TaxTotalAmount', namespaces)
            if tax_total_node is not None and tax_total_node.text:
                result['amount_tax'] = Decimal(tax_total_node.text)

            grand_total_node = monetary_node.find('ram:GrandTotalAmount', namespaces)
            if grand_total_node is not None and grand_total_node.text:
                result['amount_gross'] = Decimal(grand_total_node.text)

        # Tax Rates
        tax_nodes = settlement_node.findall('ram:ApplicableTradeTax', namespaces)
        if tax_nodes:
            # For simplicity, taking the first valid tax node
            for tax_node in tax_nodes:
                rate_node = tax_node.find('ram:RateApplicablePercent', namespaces)
                if rate_node is not None and rate_node.text:
                    result['tax_rate'] = Decimal(rate_node.text)

                cat_code_node = tax_node.find('ram:CategoryCode', namespaces)
                if cat_code_node is not None and cat_code_node.text == 'AE':
                    result['is_reverse_charge'] = True
                    break # Stop if we find AE

    return result

def _parse_ubl(root: ET.Element) -> dict:
    namespaces = {
        'ubl': 'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
        'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
        'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'
    }

    result = {
        "invoice_number": None,
        "invoice_date": None,
        "counterparty": None,
        "country": None,
        "amount_net": Decimal('0.00'),
        "amount_tax": Decimal('0.00'),
        "amount_gross": Decimal('0.00'),
        "tax_rate": Decimal('0.00'),
        "is_reverse_charge": False
    }

    # ID
    id_node = root.find('cbc:ID', namespaces)
    if id_node is not None:
        result['invoice_number'] = id_node.text

    # Date
    date_node = root.find('cbc:IssueDate', namespaces)
    if date_node is not None and date_node.text:
        try:
            result['invoice_date'] = datetime.strptime(date_node.text, '%Y-%m-%d').date()
        except ValueError:
            pass

    # Supplier
    supplier_node = root.find('.//cac:AccountingSupplierParty/cac:Party', namespaces)
    if supplier_node is not None:
        name_node = supplier_node.find('.//cbc:Name', namespaces)
        if name_node is not None:
            result['counterparty'] = name_node.text

        country_node = supplier_node.find('.//cac:PostalAddress/cac:Country/cbc:IdentificationCode', namespaces)
        if country_node is not None:
            result['country'] = country_node.text

    # Monetary Total
    legal_total_node = root.find('cac:LegalMonetaryTotal', namespaces)
    if legal_total_node is not None:
        tax_exclusive_node = legal_total_node.find('cbc:TaxExclusiveAmount', namespaces)
        if tax_exclusive_node is not None and tax_exclusive_node.text:
            result['amount_net'] = Decimal(tax_exclusive_node.text)

        tax_inclusive_node = legal_total_node.find('cbc:TaxInclusiveAmount', namespaces)
        if tax_inclusive_node is not None and tax_inclusive_node.text:
            result['amount_gross'] = Decimal(tax_inclusive_node.text)

    # Tax Total
    tax_total_node = root.find('cac:TaxTotal', namespaces)
    if tax_total_node is not None:
        tax_amount_node = tax_total_node.find('cbc:TaxAmount', namespaces)
        if tax_amount_node is not None and tax_amount_node.text:
            result['amount_tax'] = Decimal(tax_amount_node.text)

        tax_category_node = tax_total_node.find('.//cac:TaxCategory', namespaces)
        if tax_category_node is not None:
            percent_node = tax_category_node.find('cbc:Percent', namespaces)
            if percent_node is not None and percent_node.text:
                result['tax_rate'] = Decimal(percent_node.text)

            id_cat_node = tax_category_node.find('cbc:ID', namespaces)
            if id_cat_node is not None and id_cat_node.text == 'AE':
                result['is_reverse_charge'] = True

    return result

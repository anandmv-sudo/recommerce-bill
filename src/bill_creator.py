import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from . import local_state
from .eway_bill_client import EwayBillClient
from .zoho_client import ZohoClient

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S"


@dataclass
class InvoiceOutcome:
    invoice_number: str
    eway_bill_number: str
    seller_gstin: str
    line_item_count: int = 0
    vendor_id: str | None = None
    vendor_name: str | None = None
    pdf_attached: bool = False
    eway_bill_pdf_attached: bool = False
    zoho_bill_id: str | None = None
    zoho_bill_number: str | None = None
    status: str = "failed"  # success | partial | failed
    error: str | None = None
    processed_at: str = field(default_factory=lambda: datetime.now().strftime(TIMESTAMP_FMT))


def _is_interstate(seller_gstin: str, own_gstin: str) -> bool:
    return seller_gstin.strip()[:2] != own_gstin.strip()[:2]


def create_bill_for_invoice(
    records: pd.DataFrame,
    pdf_path: str | None,
    eway_bill_number: str | None,
    zoho: ZohoClient,
    eway: EwayBillClient,
) -> InvoiceOutcome:
    """Creates one Zoho bill draft for one invoice number, with one line item
    per row belonging to that invoice (e.g. one per product in a multi-item
    manifest), attaches the matched invoice PDF and the generated E-way Bill
    PDF, and records the result locally. Does not create a vendor or an item
    -- the vendor and the item (resolved by HSN + GST rate) must already
    exist in Zoho. eway_bill_number comes from the separate invoice_number
    -> E-way Bill number mapping sheet.

    Never raises -- any unexpected failure (network error, unexpected API
    shape, etc.) is caught and returned as a "failed" outcome instead, so one
    bad invoice in a batch can't stop the rest of the batch from processing.
    """
    invoice_number = records.iloc[0]["invoice_number"]
    seller_gstin = records.iloc[0]["seller_gstin"]
    try:
        return _create_bill_for_invoice(records, pdf_path, eway_bill_number, zoho, eway)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard, see docstring
        return InvoiceOutcome(
            invoice_number=invoice_number,
            eway_bill_number=eway_bill_number,
            seller_gstin=seller_gstin,
            line_item_count=len(records),
            status="failed",
            error=f"unexpected_error: {exc}",
        )


def _create_bill_for_invoice(
    records: pd.DataFrame,
    pdf_path: str | None,
    eway_bill_number: str | None,
    zoho: ZohoClient,
    eway: EwayBillClient,
) -> InvoiceOutcome:
    first = records.iloc[0]
    invoice_number = first["invoice_number"]
    seller_gstin = first["seller_gstin"]

    outcome = InvoiceOutcome(
        invoice_number=invoice_number,
        eway_bill_number=eway_bill_number,
        seller_gstin=seller_gstin,
        line_item_count=len(records),
    )

    # Re-check duplicates right before creating -- the match report may be stale
    # by the time the user clicks approve. Checked against Zoho directly, not a
    # local record, so a bill created outside this tool is still caught.
    if zoho.find_bill_by_number(invoice_number) is not None:
        outcome.status = "failed"
        outcome.error = "duplicate_previously_billed (caught at creation time)"
        return outcome

    vendor_lookup = zoho.find_vendor_by_gstin(seller_gstin, first["seller_state"])
    if vendor_lookup.status != "found":
        outcome.status = "failed"
        outcome.error = f"vendor_lookup_{vendor_lookup.status}"
        return outcome
    vendor = vendor_lookup.vendor
    outcome.vendor_id = vendor["contact_id"]
    outcome.vendor_name = vendor.get("contact_name")

    is_interstate = _is_interstate(seller_gstin, eway.cfg.gstin)
    line_items = []
    for _, record in records.iterrows():
        hsn_code = str(record["hsn_code"])
        tax_rate = float(record["tax_rate"])

        item_lookup = zoho.find_item_by_hsn(hsn_code, tax_rate)
        if item_lookup.status != "found":
            outcome.status = "failed"
            outcome.error = f"item_lookup_{item_lookup.status} (hsn={hsn_code})"
            return outcome
        item = item_lookup.item

        try:
            tax_id = zoho.resolve_tax_id(rate_percent=tax_rate, is_interstate=is_interstate)
        except Exception as exc:  # noqa: BLE001 -- surfaced in the outcome report
            outcome.status = "failed"
            outcome.error = f"tax_resolution_failed (hsn={hsn_code}): {exc}"
            return outcome

        quantity = float(record["quantity"])
        rate = round(float(record["taxable_amount"]) / quantity, 2) if quantity else round(float(record["taxable_amount"]), 2)
        line_items.append(
            {
                "item_id": item["item_id"],
                "description": str(record.get("description") or item.get("name")),
                "rate": rate,
                "quantity": quantity,
                "tax_id": tax_id,
                "account_id": item.get("purchase_account_id") or zoho.cfg.purchase_account_id,
            }
        )

    today = date.today().isoformat()
    invoice_date = first["invoice_date"]
    try:
        bill = zoho.create_bill_draft(
            vendor_id=vendor["contact_id"],
            bill_number=invoice_number,
            date=invoice_date,  # Bill Date -- from the Amazon excel's invoice date
            due_date=today,  # Due Date -- today
            line_items=line_items,
            custom_fields=[
                {"api_name": "cf_voucher_date", "value": today},
                {"api_name": "cf_supplier_invoice_date", "value": invoice_date},
                {"api_name": "cf_business_vertical", "value": "Marketplace (Re-Commerce)"},
                {"api_name": "cf_bill_to", "value": "Aggregator"},
                {"api_name": "cf_awb_number", "value": str(first.get("awb_number") or "")},
            ],
            branch_id=zoho.cfg.branch_id,  # Warehouse location -- "Telangana - HO"
        )
    except Exception as exc:  # noqa: BLE001
        outcome.status = "failed"
        outcome.error = f"zoho_bill_creation_failed: {exc}"
        return outcome

    outcome.zoho_bill_id = bill["bill_id"]
    outcome.zoho_bill_number = bill.get("bill_number")
    outcome.status = "success"  # downgraded to "partial" below if an attachment step fails

    if pdf_path:
        try:
            zoho.attach_file_to_bill(outcome.zoho_bill_id, pdf_path)
            outcome.pdf_attached = True
        except Exception as exc:  # noqa: BLE001
            outcome.status = "partial"
            outcome.error = f"invoice_pdf_attach_failed: {exc}"
    else:
        outcome.status = "partial"
        outcome.error = "invoice_pdf_missing"

    if eway_bill_number:
        tmp_path = None
        try:
            pdf_bytes = eway.generate_pdf(eway_bill_number)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name
            zoho.attach_file_to_bill(outcome.zoho_bill_id, tmp_path)
            outcome.eway_bill_pdf_attached = True
        except Exception as exc:  # noqa: BLE001
            outcome.status = "partial"
            prior = f"{outcome.error}; " if outcome.error else ""
            outcome.error = f"{prior}eway_bill_pdf_failed: {exc}"
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        outcome.status = "partial"
        prior = f"{outcome.error}; " if outcome.error else ""
        outcome.error = f"{prior}eway_bill_number_not_in_mapping"

    local_state.record_result(invoice_number, eway_bill_number, outcome.zoho_bill_id, outcome.status)
    return outcome


def create_bills_for_approved(
    df: pd.DataFrame,
    invoice_numbers: list[str],
    pdf_index: dict[str, str],
    eway_bill_mapping: dict[str, str],
    zoho: ZohoClient,
    eway: EwayBillClient,
    on_result=None,
) -> pd.DataFrame:
    """Creates one bill per approved invoice number (with one line item per
    row belonging to that invoice), one at a time, and returns the post-run
    outcome report as a DataFrame. A failure on one invoice never stops the
    rest of the batch -- create_bill_for_invoice always returns an outcome,
    never raises. If on_result is given, it's called with each InvoiceOutcome
    as soon as it's ready (e.g. to stream progress into a UI) before moving
    on to the next invoice."""
    outcomes = []
    for invoice_number in invoice_numbers:
        records = df[df["invoice_number"] == invoice_number]
        pdf_path = pdf_index.get(invoice_number)
        eway_bill_number = eway_bill_mapping.get(invoice_number)
        outcome = create_bill_for_invoice(records, pdf_path, eway_bill_number, zoho, eway)
        outcomes.append(outcome.__dict__)
        if on_result:
            on_result(outcome)
    return pd.DataFrame(outcomes)

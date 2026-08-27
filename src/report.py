import concurrent.futures as cf
from collections import Counter

import pandas as pd

from .eway_bill_client import EwayBillClient
from .item_catalog import ItemCatalog
from .zoho_client import ZohoClient

# Flags that must NOT block bill draft creation -- the E-way Bill PDF and
# invoice PDF are best-effort attachments on top of the bill, not
# prerequisites for it. A bill draft is created from the invoice sheet data
# alone (vendor + item resolved, not a duplicate); missing/failed E-way Bill
# or PDF data just means those specific attachments won't land.
_NON_BLOCKING_FLAGS = {
    "pdf_missing",
    "eway_bill_number_not_in_mapping",
    "eway_bill_lookup_failed",
    "duplicate_eway_bill_number_in_batch",
}

_MAX_WORKERS = 16


def _prefetch(df: pd.DataFrame, eway_bill_mapping: dict[str, str], zoho: ZohoClient, eway: EwayBillClient):
    """Runs every distinct vendor/duplicate/E-way Bill lookup this batch
    needs concurrently (each is an independent Zoho/GSP API call), instead of
    one row at a time. A batch with many rows but few unique invoices
    (typical -- rows repeat the same seller) collapses to a handful of
    concurrent calls instead of a slow serial chain. Item lookups aren't part
    of this -- ItemCatalog resolves those locally from the uploaded item
    export, no API call needed."""
    vendor_keys = set(df["seller_gstin"])
    invoice_numbers = set(df["invoice_number"])
    eway_numbers = {eway_bill_mapping.get(inv) for inv in invoice_numbers if eway_bill_mapping.get(inv)}

    vendor_cache, duplicate_bill_cache, eway_cache = {}, {}, {}

    with cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        vendor_futures = {executor.submit(zoho.find_vendor_by_gstin, k): k for k in vendor_keys}
        bill_futures = {executor.submit(zoho.find_bill_by_number, k): k for k in invoice_numbers}
        eway_futures = {executor.submit(eway.get_by_number, k): k for k in eway_numbers}

        for fut, key in vendor_futures.items():
            vendor_cache[key] = fut.result()
        for fut, key in bill_futures.items():
            duplicate_bill_cache[key] = fut.result() is not None
        for fut, key in eway_futures.items():
            eway_cache[key] = fut.result() is not None

    return vendor_cache, duplicate_bill_cache, eway_cache


def build_match_report(
    df: pd.DataFrame,
    pdf_index: dict[str, str],
    eway_bill_mapping: dict[str, str],
    zoho: ZohoClient,
    eway: EwayBillClient,
    item_catalog: ItemCatalog,
) -> pd.DataFrame:
    """Pre-approval report: one row per excel row (i.e. per line item), with
    a ready/flag verdict. Multiple rows can share an invoice_number -- that's
    a multi-line-item invoice, not a duplicate; only a repeated E-way Bill
    number, or an invoice number that already exists as a bill in Zoho, is
    flagged as a duplicate. Missing/failed PDF or E-way Bill data is shown
    but doesn't block readiness -- those are best-effort attachments, not
    prerequisites for creating the bill draft itself."""
    eway_number_counts = Counter(eway_bill_mapping.values())
    vendor_cache, duplicate_bill_cache, eway_cache = _prefetch(df, eway_bill_mapping, zoho, eway)

    rows = []
    for _, record in df.iterrows():
        invoice_number = record["invoice_number"]
        eway_bill_number = eway_bill_mapping.get(invoice_number)
        flags = []

        pdf_path = pdf_index.get(invoice_number)
        if not pdf_path:
            flags.append("pdf_missing")

        vendor_lookup = vendor_cache[record["seller_gstin"]]
        vendor = vendor_lookup.vendor
        if vendor_lookup.status == "not_found":
            flags.append("vendor_not_found")
        elif vendor_lookup.status == "ambiguous":
            flags.append("vendor_ambiguous")

        if not eway_bill_number:
            # Distinct from a lookup failure: this invoice has no row at all in
            # the E-way Bill mapping sheet.
            flags.append("eway_bill_number_not_in_mapping")
        elif not eway_cache[eway_bill_number]:
            flags.append("eway_bill_lookup_failed")

        item_lookup = item_catalog.find_item_by_hsn(record["hsn_code"], float(record["tax_rate"]))
        item = item_lookup.item
        if item_lookup.status == "not_found":
            flags.append("item_not_found")
        elif item_lookup.status == "ambiguous":
            flags.append("item_ambiguous")

        if eway_bill_number and eway_number_counts[eway_bill_number] > 1:
            flags.append("duplicate_eway_bill_number_in_batch")

        if duplicate_bill_cache[invoice_number]:
            flags.append("duplicate_previously_billed")

        blocking_flags = [f for f in flags if f not in _NON_BLOCKING_FLAGS]

        rows.append(
            {
                "invoice_number": invoice_number,
                "eway_bill_number": eway_bill_number,
                "seller_gstin": record["seller_gstin"],
                "hsn_code": record["hsn_code"],
                "vendor_id": vendor.get("contact_id") if vendor else None,
                "item_id": item.get("item_id") if item else None,
                "pdf_path": pdf_path,
                "ready": not blocking_flags,
                "flags": ", ".join(flags) if flags else "",
            }
        )

    return pd.DataFrame(rows)


def ready_invoice_numbers(report_df: pd.DataFrame) -> list[str]:
    """An invoice is ready to bill only if every line-item row belonging to
    it is ready -- one unready row (e.g. a bad HSN on one product) blocks
    the whole invoice, since it becomes one bill with multiple line items."""
    per_invoice_ready = report_df.groupby("invoice_number")["ready"].all()
    return per_invoice_ready[per_invoice_ready].index.tolist()

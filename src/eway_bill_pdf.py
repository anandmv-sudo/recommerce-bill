import base64
import io
import os

import barcode
import qrcode
from barcode.writer import ImageWriter
from jinja2 import Environment, FileSystemLoader
from xhtml2pdf import pisa

from .state_codes import gst_state_code_to_name

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

SUPPLY_TYPE = {"O": "Outward-Supply", "I": "Inward-Supply"}

TRANSACTION_TYPE = {
    1: "Regular",
    2: "Bill To - Ship To",
    3: "Bill From - Dispatch From",
    4: "Combination of 2 and 3",
}


def _data_uri_png(image_bytes: bytes) -> str:
    return "data:image/png;base64, " + base64.b64encode(image_bytes).decode("ascii")


def _qr_code_data_uri(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return _data_uri_png(buf.getvalue())


def _barcode_data_uri(ewb_no: str) -> str:
    # EAN-13 needs a 12-digit payload (the 13th is a computed check digit);
    # E-way Bill numbers are 12 digits, matching the sample document.
    digits = "".join(ch for ch in str(ewb_no) if ch.isdigit())[:12]
    ean = barcode.get("ean13", digits, writer=ImageWriter())
    buf = io.BytesIO()
    ean.write(buf, options={"write_text": False})
    return _data_uri_png(buf.getvalue())


def build_context(result: dict, own_gstin: str) -> dict:
    """Maps a GetEwayBill `result` payload to the eway_bill_new.html template context."""
    items = result.get("itemList", [])
    for item in items:
        name = (item.get("productName") or "").strip()
        desc = (item.get("productDesc") or "").strip()
        item["productName"] = f"{name} & {desc}" if name and desc else (name or desc)

    vehicles = result.get("VehiclListDetails", [])
    user_gstin = result.get("userGstin", "")
    for v in vehicles:
        trans_doc_no = v.get("transDocNo") or "-"
        trans_doc_date = v.get("transDocDate") or "-"
        v["vehicleNo"] = f"{v.get('vehicleNo', '')}/ {trans_doc_no} & {trans_doc_date}"
        v["enteredBy"] = f"{user_gstin[:10]} {user_gstin[10:]}" if len(user_gstin) >= 10 else user_gstin

    ewb_no = result.get("ewbNo")
    transporter_id = result.get("transporterId") or "-"
    transporter_name = result.get("transporterName") or "-"
    trans_doc_no = vehicles[0].get("transDocNo") if vehicles else None
    trans_doc_date = vehicles[0].get("transDocDate") if vehicles else None

    return {
        "ewbNo": ewb_no,
        "ewbDate": result.get("ewayBillDate"),
        "userGstin": user_gstin,
        "docType": "Tax invoice" if result.get("docType") == "INV" else "-",
        "mode": "Road",
        "type": SUPPLY_TYPE.get((result.get("supplyType") or "").strip(), result.get("supplyType")),
        "distance": result.get("actualDist"),
        "validTill": result.get("validUpto"),
        "invoiceNo": result.get("docNo"),
        "invoiceDate": result.get("docDate"),
        "transactionType": TRANSACTION_TYPE.get(result.get("transactionType"), result.get("transactionType")),
        "sellerGstin": result.get("fromGstin"),
        "sellerName": result.get("fromTrdName"),
        "fromAddr1": result.get("fromAddr1"),
        "fromAddr2": result.get("fromAddr2"),
        "pickupCity": result.get("fromPlace"),
        "pickupState": gst_state_code_to_name(result.get("actFromStateCode")),
        "pickupPin": result.get("fromPincode"),
        "recyclerGstin": result.get("toGstin"),
        "recyclerName": result.get("toTrdName"),
        "toAddr1": result.get("toAddr1"),
        "toAddr2": result.get("toAddr2"),
        "shippingCity": result.get("toPlace"),
        "shippingState": gst_state_code_to_name(result.get("actToStateCode")),
        "shippingPin": result.get("toPincode"),
        "itemDetails": items,
        "totalValue": result.get("totalValue", 0.0),
        "totalInvValue": result.get("totInvValue", 0.0),
        "cgstValue": result.get("cgstValue", 0.0),
        "sgstValue": result.get("sgstValue", 0.0),
        "igstValue": result.get("igstValue", 0.0),
        "cessValue": result.get("cessValue", 0.0),
        "cessNonAdvolValue": result.get("cessNonAdvolValue", 0.0),
        "otherValue": result.get("otherValue", 0.0),
        "transporterName": f"{transporter_id} & {transporter_name}",
        "transDocNoDate": f"{trans_doc_no or '-'} & {trans_doc_date or '-'}",
        "vehicleListDetails": vehicles,
        "qrCode": _qr_code_data_uri(f"{ewb_no}/{own_gstin}/{result.get('ewayBillDate')}"),
        "barCode": _barcode_data_uri(ewb_no),
    }


def render_html(context: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("eway_bill_new.html")
    return template.render(**context)


def render_pdf(html: str) -> bytes:
    buf = io.BytesIO()
    result = pisa.CreatePDF(io.StringIO(html), dest=buf)
    if result.err:
        raise RuntimeError(f"HTML-to-PDF conversion failed with {result.err} error(s)")
    return buf.getvalue()

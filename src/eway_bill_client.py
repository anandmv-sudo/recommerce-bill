import uuid

import requests

from . import eway_bill_auth, eway_bill_pdf
from .config import EwayBillConfig


class EwayBillClient:
    """Wrapper around Adaequare's GSP "enriched" E-way Bill API.

    GetEwayBill looks up by E-way Bill number (ewbNo), not invoice number --
    the excel sheet carries the E-way Bill number per row directly, so no
    invoice-number resolution is needed.
    """

    def __init__(self, cfg: EwayBillConfig):
        self.cfg = cfg

    def _headers(self, request_id: str | None = None) -> dict:
        return {
            "gstin": self.cfg.gstin,
            "Authorization": f"Bearer {eway_bill_auth.get_access_token(self.cfg)}",
            "Content-Type": "application/json",
            "requestid": request_id or str(uuid.uuid4()),
            "username": self.cfg.username,
            "password": self.cfg.password,
        }

    def get_by_number(self, eway_bill_number: str) -> dict | None:
        """Fetch E-way Bill details by E-way Bill number.

        Returns None if the API reports the E-way Bill doesn't exist; raises
        for any other failure (auth, transport, unexpected error shape). A
        401/403 -- the cached GSP token being rejected as unauthorized even
        though it wasn't due for its proactive refresh yet (revoked early,
        clock drift) -- is retried once with a forced-fresh token before
        giving up.
        """
        response = requests.get(
            f"{self.cfg.base_url}/enriched/ewb/ewayapi/GetEwayBill",
            headers=self._headers(),
            params={"ewbNo": eway_bill_number},
            timeout=30,
        )
        if response.status_code in (401, 403):
            eway_bill_auth.invalidate(self.cfg)
            response = requests.get(
                f"{self.cfg.base_url}/enriched/ewb/ewayapi/GetEwayBill",
                headers=self._headers(),
                params={"ewbNo": eway_bill_number},
                timeout=30,
            )
        response.raise_for_status()
        body = response.json()

        if not body.get("success"):
            message = str(body.get("message", ""))
            if "not exist" in message.lower() or "no record" in message.lower():
                return None
            raise RuntimeError(f"E-way Bill API error for {eway_bill_number}: {message}")

        return body["result"]

    def generate_pdf(self, eway_bill_number: str) -> bytes:
        """Fetches the E-way Bill, renders it against eway_bill_new.html, and
        returns the PDF bytes -- the Python equivalent of the Java
        generateEWayBillHTMLNew + createPdfForEwaybill pipeline (same source
        JSON, local template render, then HTML-to-PDF)."""
        result = self.get_by_number(eway_bill_number)
        if result is None:
            raise RuntimeError(f"E-way Bill {eway_bill_number} not found")

        context = eway_bill_pdf.build_context(result, own_gstin=self.cfg.gstin)
        html = eway_bill_pdf.render_html(context)
        return eway_bill_pdf.render_pdf(html)

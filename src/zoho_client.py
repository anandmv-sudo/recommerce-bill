from dataclasses import dataclass, field

import requests

from .config import ZohoConfig
from .state_codes import to_state_code
from .zoho_auth import get_access_token


@dataclass
class VendorLookupResult:
    status: str  # "found" | "not_found" | "ambiguous"
    vendor: dict | None = None
    candidates: list[dict] = field(default_factory=list)


@dataclass
class ItemLookupResult:
    status: str  # "found" | "not_found" | "ambiguous"
    item: dict | None = None
    candidates: list[dict] = field(default_factory=list)


class ZohoClient:
    """Thin wrapper around the Zoho Books v3 API.

    NOTE: server-side filtering by bill_number (bills) is not confirmed in
    Zoho's public docs -- verify against a sandbox org before relying on it.
    gst_no filtering on /contacts IS confirmed to work server-side.
    """

    def __init__(self, cfg: ZohoConfig):
        self.cfg = cfg
        self._taxes_cache: list[dict] | None = None

    def _headers(self) -> dict:
        token = get_access_token(self.cfg)
        return {"Authorization": f"Zoho-oauthtoken {token}"}

    def _params(self, extra: dict | None = None) -> dict:
        params = {"organization_id": self.cfg.organization_id}
        if extra:
            params.update(extra)
        return params

    @staticmethod
    def _check(response: requests.Response) -> dict:
        body = response.json()
        if not response.ok:
            raise RuntimeError(f"Zoho API error {response.status_code}: {body}")
        return body

    def find_vendor_by_gstin(self, gstin: str, state: str) -> VendorLookupResult:
        response = requests.get(
            f"{self.cfg.api_base_url}/contacts",
            headers=self._headers(),
            params=self._params({"gst_no": gstin}),
            timeout=30,
        )
        contacts = self._check(response).get("contacts", [])

        state_code = to_state_code(state)
        matches = [
            c for c in contacts
            if c.get("gst_no") == gstin
            and c.get("place_of_contact") == state_code
            and c.get("contact_type") == "vendor"
        ]

        if not matches:
            return VendorLookupResult(status="not_found", candidates=contacts)
        if len(matches) > 1:
            # Same GSTIN + state matched more than one contact -- a duplicate
            # vendor record in Zoho itself. Don't guess; surface it.
            return VendorLookupResult(status="ambiguous", candidates=matches)
        return VendorLookupResult(status="found", vendor=matches[0])

    def find_item_by_hsn(self, hsn_code: str, tax_rate: float) -> ItemLookupResult:
        """Resolves an existing Zoho item (never creates one) by HSN code,
        restricted to the "RTREC_" prefixed generic HSN-category catalog
        (e.g. RTREC_CEILING FANS) rather than the full per-product item list
        -- HSN code alone is ambiguous against the full catalog (the same
        HSN can cover hundreds of distinct products), and even within the
        RTREC_ catalog some HSN codes have more than one entry (e.g. two
        near-duplicate wordings for the same heading, at different GST
        rates). Matching the row's GST rate (from the Amazon excel) against
        each candidate's own tax_percentage resolves most of those -- e.g.
        HSN 73239390 has one RTREC_ entry at 5% and another at 18%. Where
        candidates still share both HSN and rate (a true duplicate in the
        catalog, e.g. two RTREC_ entries for the same HSN both at 18%),
        that's surfaced as ambiguous rather than guessed."""
        response = requests.get(
            f"{self.cfg.api_base_url}/items",
            headers=self._headers(),
            params=self._params({"hsn_or_sac": hsn_code}),
            timeout=30,
        )
        items = self._check(response).get("items", [])
        rtrec_matches = [
            it
            for it in items
            if it.get("hsn_or_sac") == str(hsn_code) and (it.get("name") or "").upper().startswith("RTREC")
        ]
        matches = [
            it
            for it in rtrec_matches
            if any(
                abs(pref.get("tax_percentage", -1) - tax_rate) < 0.01
                for pref in it.get("item_tax_preferences", [])
            )
        ]

        if not matches:
            return ItemLookupResult(status="not_found", candidates=rtrec_matches)
        if len(matches) > 1:
            return ItemLookupResult(status="ambiguous", candidates=matches)
        return ItemLookupResult(status="found", item=matches[0])

    def find_bill_by_number(self, bill_number: str) -> dict | None:
        response = requests.get(
            f"{self.cfg.api_base_url}/bills",
            headers=self._headers(),
            params=self._params({"bill_number": bill_number}),
            timeout=30,
        )
        bills = self._check(response).get("bills", [])
        for bill in bills:
            if bill.get("bill_number") == bill_number:
                return bill
        return None

    def list_taxes(self) -> list[dict]:
        if self._taxes_cache is None:
            response = requests.get(
                f"{self.cfg.api_base_url}/settings/taxes",
                headers=self._headers(),
                params=self._params(),
                timeout=30,
            )
            self._taxes_cache = self._check(response).get("taxes", [])
        return self._taxes_cache

    def resolve_tax_id(self, rate_percent: float, is_interstate: bool) -> str:
        """Resolves a GST rate (e.g. 18 or 18.0) + interstate/intrastate flag
        to the matching Zoho tax_id -- IGST<rate> for interstate, the
        GST<rate> group (which Zoho splits into CGST+SGST) for intrastate."""
        prefix = "IGST" if is_interstate else "GST"
        rate_int = int(round(rate_percent))
        tax_name = f"{prefix}{rate_int}"
        for tax in self.list_taxes():
            if tax.get("tax_name") == tax_name:
                return tax["tax_id"]
        raise RuntimeError(f"No Zoho tax found matching '{tax_name}' (rate={rate_percent}, interstate={is_interstate})")

    def create_bill_draft(
        self,
        vendor_id: str,
        bill_number: str,
        date: str,
        line_items: list[dict],
        due_date: str | None = None,
        custom_fields: list[dict] | None = None,
        branch_id: str | None = None,
    ) -> dict:
        payload = {
            "vendor_id": vendor_id,
            "bill_number": bill_number,
            "date": date,
            "line_items": line_items,
        }
        if due_date:
            payload["due_date"] = due_date
        if custom_fields:
            payload["custom_fields"] = custom_fields
        if branch_id:
            payload["branch_id"] = branch_id

        response = requests.post(
            f"{self.cfg.api_base_url}/bills",
            headers=self._headers(),
            params=self._params(),
            json=payload,
            timeout=30,
        )
        return self._check(response)["bill"]

    def attach_file_to_bill(self, bill_id: str, file_path: str) -> dict:
        with open(file_path, "rb") as f:
            response = requests.post(
                f"{self.cfg.api_base_url}/bills/{bill_id}/attachment",
                headers=self._headers(),
                params=self._params(),
                files={"attachment": f},
                timeout=60,
            )
        return self._check(response)

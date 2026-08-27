import concurrent.futures as cf
import threading
from dataclasses import dataclass, field

import requests

from .config import ZohoConfig
from .state_codes import to_state_code
from .zoho_auth import get_access_token

_TAXINFO_MAX_WORKERS = 16


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
    Vendor lookup deliberately does NOT filter /contacts by gst_no server-side
    -- that param only matches a vendor's primary GSTIN, missing additional
    GSTINs registered under the contact's /taxinfo. See find_vendor_by_gstin.
    """

    def __init__(self, cfg: ZohoConfig):
        self.cfg = cfg
        self._taxes_cache: list[dict] | None = None
        self._vendor_registrations: list[dict] | None = None
        self._vendor_index_lock = threading.Lock()

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
        """A vendor in Zoho Books can have one primary GSTIN plus any number
        of additional GSTINs (registered per state, exposed via each
        contact's /taxinfo sub-resource) -- an invoice's seller GSTIN can
        legitimately be either. The plain /contacts?gst_no= filter only ever
        matches the primary one, so this matches against a full index of
        every vendor's primary + additional (gstin, state) registrations,
        built once per ZohoClient instance and reused for every lookup."""
        self._ensure_vendor_index()

        state_code = to_state_code(state)
        same_gstin = [r for r in self._vendor_registrations if r["gstin"] == gstin]
        matches = [r for r in same_gstin if r["state_code"] == state_code]

        if not matches:
            return VendorLookupResult(status="not_found", candidates=[r["vendor"] for r in same_gstin])

        vendor_ids = {r["vendor"]["contact_id"] for r in matches}
        if len(vendor_ids) > 1:
            # Same GSTIN + state matched more than one vendor contact -- a
            # duplicate vendor record in Zoho itself. Don't guess; surface it.
            return VendorLookupResult(status="ambiguous", candidates=[r["vendor"] for r in matches])
        return VendorLookupResult(status="found", vendor=matches[0]["vendor"])

    def _ensure_vendor_index(self) -> None:
        with self._vendor_index_lock:
            if self._vendor_registrations is not None:
                return

            vendors = self._list_all_vendor_contacts()
            registrations = []
            for vendor in vendors:
                gstin, state_code = vendor.get("gst_no"), vendor.get("place_of_contact")
                if gstin and state_code:
                    registrations.append({"gstin": gstin, "state_code": state_code, "vendor": vendor})

            with cf.ThreadPoolExecutor(max_workers=_TAXINFO_MAX_WORKERS) as executor:
                taxinfo_futures = {executor.submit(self._get_taxinfo, v["contact_id"]): v for v in vendors}
                for future, vendor in taxinfo_futures.items():
                    for entry in future.result():
                        gstin, state_code = entry.get("gst_no"), entry.get("place_of_contact")
                        if gstin and state_code:
                            registrations.append({"gstin": gstin, "state_code": state_code, "vendor": vendor})

            self._vendor_registrations = registrations

    def _list_all_vendor_contacts(self) -> list[dict]:
        contacts = []
        page = 1
        while True:
            response = requests.get(
                f"{self.cfg.api_base_url}/contacts",
                headers=self._headers(),
                params=self._params({"contact_type": "vendor", "page": page, "per_page": 200}),
                timeout=30,
            )
            body = self._check(response)
            contacts.extend(body.get("contacts", []))
            if not body.get("page_context", {}).get("has_more_page"):
                return contacts
            page += 1

    def _get_taxinfo(self, contact_id: str) -> list[dict]:
        """The list-of-additional-GSTINs response key isn't confirmed against
        Zoho's docs, so this takes whichever list-of-dicts value is present
        in the response body rather than guessing a specific key name."""
        response = requests.get(
            f"{self.cfg.api_base_url}/contacts/{contact_id}/taxinfo",
            headers=self._headers(),
            params=self._params(),
            timeout=30,
        )
        body = self._check(response)
        for value in body.values():
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                return value
        return []

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

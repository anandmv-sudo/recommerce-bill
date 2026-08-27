import concurrent.futures as cf
import threading
from dataclasses import dataclass, field

import requests

from .config import ZohoConfig
from .state_codes import to_state_code
from .zoho_auth import get_access_token

_TAXINFO_MAX_WORKERS = 16


def _pan_of(gstin: str | None) -> str | None:
    """Characters 3-12 (1-indexed) of a 15-digit GSTIN are the PAN of the
    entity it's registered to. Every GSTIN issued to that entity -- primary
    or additional, in any state -- shares this same 10-character PAN."""
    if not gstin or len(gstin) < 12:
        return None
    return gstin[2:12]


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
        self._vendors_by_pan: dict[str, list[dict]] | None = None
        self._vendor_list_lock = threading.Lock()
        self._taxinfo_checked: set[str] = set()
        self._taxinfo_lock = threading.Lock()
        self._registrations_append_lock = threading.Lock()

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
        legitimately be either. Checking every vendor's /taxinfo up front
        doesn't scale (one API call per vendor in the org, paid on every
        run even for a 2-invoice batch), so this only fetches /taxinfo for
        vendors that could plausibly match: characters 3-12 of a GSTIN are
        the entity's PAN, and every GSTIN (primary or additional) issued to
        the same legal entity shares that PAN -- so a primary-GSTIN miss
        falls back to PAN-matching against the (already-fetched) vendor
        list, and only that narrowed set gets a /taxinfo lookup."""
        self._ensure_vendor_list()

        state_code = to_state_code(state)
        matches = self._registration_matches(gstin, state_code)

        if not matches:
            pan = _pan_of(gstin)
            candidates = self._vendors_by_pan.get(pan, []) if pan else []
            self._ensure_taxinfo_checked(candidates)
            matches = self._registration_matches(gstin, state_code)

        if not matches:
            same_gstin = [r for r in self._vendor_registrations if r["gstin"] == gstin]
            return VendorLookupResult(status="not_found", candidates=[r["vendor"] for r in same_gstin])

        vendor_ids = {r["vendor"]["contact_id"] for r in matches}
        if len(vendor_ids) > 1:
            # Same GSTIN + state matched more than one vendor contact -- a
            # duplicate vendor record in Zoho itself. Don't guess; surface it.
            return VendorLookupResult(status="ambiguous", candidates=[r["vendor"] for r in matches])
        return VendorLookupResult(status="found", vendor=matches[0]["vendor"])

    def _registration_matches(self, gstin: str, state_code: str) -> list[dict]:
        return [r for r in self._vendor_registrations if r["gstin"] == gstin and r["state_code"] == state_code]

    def _ensure_vendor_list(self) -> None:
        with self._vendor_list_lock:
            if self._vendor_registrations is not None:
                return

            vendors = self._list_all_vendor_contacts()
            registrations = []
            vendors_by_pan: dict[str, list[dict]] = {}
            for vendor in vendors:
                gstin, state_code = vendor.get("gst_no"), vendor.get("place_of_contact")
                if gstin and state_code:
                    registrations.append({"gstin": gstin, "state_code": state_code, "vendor": vendor})
                pan = _pan_of(gstin)
                if pan:
                    vendors_by_pan.setdefault(pan, []).append(vendor)

            self._vendor_registrations = registrations
            self._vendors_by_pan = vendors_by_pan

    def _ensure_taxinfo_checked(self, candidates: list[dict]) -> None:
        to_fetch = []
        with self._taxinfo_lock:
            for vendor in candidates:
                contact_id = vendor["contact_id"]
                if contact_id not in self._taxinfo_checked:
                    self._taxinfo_checked.add(contact_id)
                    to_fetch.append(vendor)
        if not to_fetch:
            return

        with cf.ThreadPoolExecutor(max_workers=min(_TAXINFO_MAX_WORKERS, len(to_fetch))) as executor:
            futures = {executor.submit(self._get_taxinfo, v["contact_id"]): v for v in to_fetch}
            results = [(v, future.result()) for future, v in futures.items()]

        with self._registrations_append_lock:
            for vendor, entries in results:
                for entry in entries:
                    # /taxinfo entries use different field names than the
                    # /contacts list response (confirmed empirically): the
                    # GSTIN is "tax_registration_no", not "gst_no", and the
                    # state is "place_of_supply", not "place_of_contact".
                    gstin, state_code = entry.get("tax_registration_no"), entry.get("place_of_supply")
                    if gstin and state_code:
                        self._vendor_registrations.append({"gstin": gstin, "state_code": state_code, "vendor": vendor})

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
        """Returns the raw entries from Zoho's response -- confirmed
        (empirically, undocumented in Zoho's public API docs) to be a
        "tax_info_list" array of {tax_info_id, tax_registration_no,
        place_of_supply, is_primary, trader_name, legal_name} dicts. Takes
        whichever list-of-dicts value is present rather than hardcoding the
        "tax_info_list" key, in case that key name varies across orgs/plans."""
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
        that's surfaced as ambiguous rather than guessed.

        The name_contains=RTREC filter is applied server-side (confirmed
        empirically) rather than just filtering client-side after an
        unfiltered /items?hsn_or_sac= call -- a shared HSN can have
        thousands of individual per-product items in the live org (each
        imported from CSV), well past the first page of results, so a
        client-side-only filter could miss the RTREC_ catalog entry
        entirely if it doesn't happen to land on page 1."""
        response = requests.get(
            f"{self.cfg.api_base_url}/items",
            headers=self._headers(),
            params=self._params({"hsn_or_sac": hsn_code, "name_contains": "RTREC"}),
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
        gst_no: str | None = None,
        source_of_supply: str | None = None,
    ) -> dict:
        """gst_no/source_of_supply post this specific bill under the exact
        GSTIN the invoice was matched on -- which find_vendor_by_gstin may
        have resolved via a vendor's ADDITIONAL GSTIN, not their primary
        one. Left unset, Zoho defaults the bill to the vendor's primary
        GSTIN/state regardless of which registration actually matched."""
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
        if gst_no:
            payload["gst_no"] = gst_no
        if source_of_supply:
            payload["source_of_supply"] = source_of_supply

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

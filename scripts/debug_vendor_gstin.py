"""One-off diagnostic for a "vendor not found" GSTIN match failure.

Usage:
    python -m scripts.debug_vendor_gstin <GSTIN> [--env live|test] [--contact-id ID]

With no --contact-id, this mirrors zoho_client.py's own lookup: check every
vendor's primary gst_no first; if none match, derive the PAN (chars 3-12)
from the GSTIN and only call /taxinfo for vendors whose primary gst_no
shares that PAN -- normally a handful of contacts (a business rarely holds
more than 6-7 GSTINs), not the whole org. Prints:
  - whether it matched on the vendor's primary gst_no, or only inside taxinfo
  - the PAN-matching candidates it checked, and each one's raw /taxinfo
    response, so the actual JSON field names can be verified against what
    _get_taxinfo()/find_vendor_by_gstin() assume

With --contact-id, it skips the scan and just dumps that one vendor's
contact record + raw /taxinfo response.

With --state, it also calls the REAL src.zoho_client.ZohoClient.find_vendor_by_gstin
directly (the exact function the app calls) and prints its result -- this is
the only mode that actually exercises the production code path end to end,
rather than a diagnostic re-implementation of it that could itself be stale
or subtly different.
"""

import argparse
import json
import sys

import requests

from src.config import load_zoho_config
from src.zoho_auth import get_access_token
from src.zoho_client import ZohoClient, _pan_of


def call_real_find_vendor_by_gstin(cfg, gstin, state):
    result = ZohoClient(cfg).find_vendor_by_gstin(gstin, state)
    print(f"\n--- Calling the REAL find_vendor_by_gstin({gstin!r}, {state!r}) ---")
    print(f"status: {result.status}")
    if result.vendor:
        print(f"vendor: {result.vendor.get('contact_name')} (contact_id={result.vendor.get('contact_id')})")
    if result.candidates:
        print(f"candidates: {[c.get('contact_name') for c in result.candidates]}")


def _headers(cfg):
    token = get_access_token(cfg)
    return {"Authorization": f"Zoho-oauthtoken {token}"}


def _params(cfg, extra=None):
    params = {"organization_id": cfg.organization_id}
    if extra:
        params.update(extra)
    return params


def list_all_vendor_contacts(cfg):
    contacts = []
    page = 1
    while True:
        response = requests.get(
            f"{cfg.api_base_url}/contacts",
            headers=_headers(cfg),
            params=_params(cfg, {"contact_type": "vendor", "page": page, "per_page": 200}),
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        contacts.extend(body.get("contacts", []))
        if not body.get("page_context", {}).get("has_more_page"):
            return contacts
        page += 1


def get_taxinfo_raw(cfg, contact_id):
    response = requests.get(
        f"{cfg.api_base_url}/contacts/{contact_id}/taxinfo",
        headers=_headers(cfg),
        params=_params(cfg),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def dump_vendor(cfg, contact_id, gstin):
    contacts = list_all_vendor_contacts(cfg)
    vendor = next((c for c in contacts if c.get("contact_id") == contact_id), None)
    if vendor is None:
        print(f"No vendor contact found with contact_id={contact_id}")
        return
    print(f"Vendor: {vendor.get('contact_name')} (contact_id={contact_id})")
    print(f"Primary gst_no on contact record: {vendor.get('gst_no')!r}")
    print(f"Primary place_of_contact: {vendor.get('place_of_contact')!r}")
    print("\nRaw /taxinfo response:")
    print(json.dumps(get_taxinfo_raw(cfg, contact_id), indent=2))


def scan_for_gstin(cfg, gstin):
    print("Fetching all vendor contacts...")
    contacts = list_all_vendor_contacts(cfg)
    print(f"Found {len(contacts)} vendor contact(s).\n")

    primary_hit = next((c for c in contacts if c.get("gst_no") == gstin), None)
    if primary_hit:
        print(f"MATCHED on primary gst_no: {primary_hit.get('contact_name')} (contact_id={primary_hit.get('contact_id')})")
        print(f"place_of_contact: {primary_hit.get('place_of_contact')!r}")
        return

    pan = _pan_of(gstin)
    print(f"No primary gst_no match. Derived PAN: {pan!r}")
    if not pan:
        print("GSTIN is shorter than 12 characters -- can't derive a PAN to narrow the candidate search.")
        return

    candidates = [c for c in contacts if _pan_of(c.get("gst_no")) == pan]
    print(f"{len(candidates)} vendor(s) share this PAN on their primary gst_no -- checking /taxinfo for each:\n")
    if not candidates:
        print("No vendor's primary gst_no shares this PAN, so the production code's fallback would never")
        print("have checked this vendor's /taxinfo either -- that's the bug. Likely cause: this vendor's")
        print("primary gst_no field in Zoho is blank, or set to a GSTIN with a different PAN than the")
        print("additional one on their /taxinfo. Pass --contact-id <id> to inspect a specific vendor directly.")
        return

    for vendor in candidates:
        contact_id = vendor.get("contact_id")
        print(f"- {vendor.get('contact_name')} (contact_id={contact_id}, primary gst_no={vendor.get('gst_no')!r})")
        try:
            body = get_taxinfo_raw(cfg, contact_id)
        except requests.HTTPError as exc:
            print(f"  taxinfo fetch failed -- {exc}")
            continue

        list_fields = {k: v for k, v in body.items() if isinstance(v, list)}
        found = False
        for field_name, entries in list_fields.items():
            for entry in entries:
                if isinstance(entry, dict) and gstin in str(entry.values()):
                    found = True
                    print(f"  MATCHED inside /taxinfo response field '{field_name}':")
                    print("  " + json.dumps(entry, indent=2).replace("\n", "\n  "))
        print(f"  Full raw /taxinfo response:")
        print("  " + json.dumps(body, indent=2).replace("\n", "\n  "))
        if found:
            return

    print("\nChecked every PAN-matching vendor's /taxinfo -- no entry contains this GSTIN.")
    print("Compare the raw responses printed above against the field names find_vendor_by_gstin() expects")
    print("(gst_no, place_of_contact) in src/zoho_client.py.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gstin")
    parser.add_argument("--env", choices=["live", "test"], default="live")
    parser.add_argument("--contact-id", default=None, help="Skip the scan, dump this one vendor's record + taxinfo")
    parser.add_argument("--state", default=None, help="Also call the real find_vendor_by_gstin(gstin, state)")
    args = parser.parse_args()

    cfg = load_zoho_config(args.env)

    if args.contact_id:
        dump_vendor(cfg, args.contact_id, args.gstin)
    else:
        scan_for_gstin(cfg, args.gstin)

    if args.state:
        call_real_find_vendor_by_gstin(cfg, args.gstin, args.state)


if __name__ == "__main__":
    sys.exit(main())

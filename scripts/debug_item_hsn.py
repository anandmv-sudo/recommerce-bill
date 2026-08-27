"""One-off diagnostic for "item not found" / "item ambiguous" HSN+rate match
issues when two RTREC_ items share the same HSN code.

Usage:
    python -m scripts.debug_item_hsn <HSN_CODE> [--env live|test]

Prints every RTREC_ item Zoho's /items list returns for this HSN, with its
raw item_tax_preferences as seen in the LIST response -- useful for
comparing against what ends up in an uploaded Zoho item export (see
src/item_catalog.py, which the app now uses to resolve items locally
instead of calling /items live). If item_tax_preferences comes back
empty/missing here even though the item clearly has a GST rate configured
in the Zoho UI, that's the bug: the list endpoint doesn't carry full tax
preference data and a detail call (GET /items/{item_id}) is needed instead.
"""

import argparse
import json
import sys

import requests

from src.config import load_zoho_config


def _headers(cfg):
    from src.zoho_auth import get_access_token
    token = get_access_token(cfg)
    return {"Authorization": f"Zoho-oauthtoken {token}"}


def _params(cfg, extra=None):
    params = {"organization_id": cfg.organization_id}
    if extra:
        params.update(extra)
    return params


def list_items_raw(cfg, hsn_code):
    response = requests.get(
        f"{cfg.api_base_url}/items",
        headers=_headers(cfg),
        params=_params(cfg, {"hsn_or_sac": hsn_code}),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("items", [])


def get_item_detail_raw(cfg, item_id):
    response = requests.get(
        f"{cfg.api_base_url}/items/{item_id}",
        headers=_headers(cfg),
        params=_params(cfg),
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("item", {})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("hsn_code")
    parser.add_argument("--env", choices=["live", "test"], default="live")
    args = parser.parse_args()

    cfg = load_zoho_config(args.env)

    print(f"Fetching /items?hsn_or_sac={args.hsn_code} ...")
    items = list_items_raw(cfg, args.hsn_code)
    rtrec_items = [it for it in items if it.get("hsn_or_sac") == str(args.hsn_code) and (it.get("name") or "").upper().startswith("RTREC")]
    print(f"Found {len(items)} total item(s), {len(rtrec_items)} RTREC_ item(s) matching this HSN.\n")

    for it in rtrec_items:
        print(f"- {it.get('name')} (item_id={it.get('item_id')})")
        print(f"  hsn_or_sac: {it.get('hsn_or_sac')!r}")
        print(f"  item_tax_preferences (from LIST response): {json.dumps(it.get('item_tax_preferences'))}")
        detail = get_item_detail_raw(cfg, it.get("item_id"))
        print(f"  item_tax_preferences (from DETAIL response): {json.dumps(detail.get('item_tax_preferences'))}")
        print(f"  tax_percentage (top-level, DETAIL response): {detail.get('tax_percentage')!r}")
        print()


if __name__ == "__main__":
    sys.exit(main())

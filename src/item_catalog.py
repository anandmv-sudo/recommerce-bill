from dataclasses import dataclass, field

import pandas as pd

_RATE_TOLERANCE = 0.01


@dataclass
class ItemLookupResult:
    status: str  # "found" | "not_found" | "ambiguous"
    item: dict | None = None
    candidates: list[dict] = field(default_factory=list)


class ItemCatalog:
    """Resolves Zoho items (item_id, name) by HSN code + GST rate from a
    locally-uploaded Zoho Books item export, instead of calling the /items
    API once per line item -- the export already carries item_id, HSN/SAC
    and both GST rate columns, which is everything a bill draft's line
    items need. Built once per run from parse_item_catalog's output.

    Same lookup semantics as the old ZohoClient.find_item_by_hsn: a rate
    match against either the intra-state or inter-state rate column (a
    combined GST rate is the same number either way), zero matches for a
    given HSN+rate is "not_found", and more than one match for the same
    HSN+rate is "ambiguous" rather than guessed.
    """

    def __init__(self, df: pd.DataFrame):
        self._by_hsn: dict[str, list[dict]] = {}
        for _, row in df.iterrows():
            self._by_hsn.setdefault(row["hsn_code"], []).append(
                {
                    "item_id": row["item_id"],
                    "name": row["name"],
                    "intra_state_tax_rate": row["intra_state_tax_rate"],
                    "inter_state_tax_rate": row["inter_state_tax_rate"],
                }
            )

    def find_item_by_hsn(self, hsn_code: str, tax_rate: float) -> ItemLookupResult:
        candidates = self._by_hsn.get(str(hsn_code).strip(), [])
        matches = [
            item
            for item in candidates
            if abs(item["intra_state_tax_rate"] - tax_rate) < _RATE_TOLERANCE
            or abs(item["inter_state_tax_rate"] - tax_rate) < _RATE_TOLERANCE
        ]

        if not matches:
            return ItemLookupResult(status="not_found", candidates=candidates)
        if len(matches) > 1:
            return ItemLookupResult(status="ambiguous", candidates=matches)
        return ItemLookupResult(status="found", item=matches[0])

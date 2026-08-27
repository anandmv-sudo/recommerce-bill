# Bulk Bill Automation (Zoho + E-way Bill)

Local tool for the flow in [Draft--bulk-bill-creation-zoho-eway-automation.md](../../Draft--bulk-bill-creation-zoho-eway-automation.md):
upload an invoice excel + ZIP of invoice PDFs, review a match report, approve, and have bill drafts
(with E-way Bill PDFs attached) created in Zoho Books.

**Status: Phase 1 scaffold.** Zoho auth + client, excel/PDF parsing, and the review UI exist.
E-way Bill integration is stubbed out pending the API spec (see `src/eway_bill_client.py`), and
bill draft creation/attachment is not yet wired into the app.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in the Zoho values (see below). **Never commit `.env`
   or put it in the shared drive with real values.**
3. Run the app:
   ```
   streamlit run app.py
   ```

## Getting Zoho credentials (one-time, needs Zoho org admin access)

1. Confirm your org's data center: Zoho Books → Settings → General → look at the URL/region.
   India orgs are almost always `zoho.in` — set `ZOHO_DC=in` in `.env` to match.
2. Go to the API Console **for your org's data center** — for `ZOHO_DC=in` this is
   `https://api-console.zoho.in`, **not** `https://api-console.zoho.com`. This matters: a client
   registered on the wrong DC's console will fail with `invalid_client` when exchanged against
   `accounts.zoho.in`, since client_id/secret/refresh tokens are DC-scoped by default. Create a
   **Self Client** (not "Server-based Application" — Self Client is the no-browser-redirect flow
   suited to a script).
3. Note the `client_id` / `client_secret` shown → put these in `.env`.
4. In the Self Client's "Generate Code" tab, enter the scope `ZohoBooks.fullaccess.all` (or the
   narrower `ZohoBooks.contacts.ALL,ZohoBooks.bills.ALL`) and generate a one-time authorization code.
5. Exchange that code for a refresh token (one-time, from a terminal). Use the accounts domain
   matching your DC — `accounts.zoho.in` for `ZOHO_DC=in`:
   ```
   curl -X POST "https://accounts.zoho.in/oauth/v2/token" \
     -d "grant_type=authorization_code" \
     -d "client_id=YOUR_CLIENT_ID" \
     -d "client_secret=YOUR_CLIENT_SECRET" \
     -d "code=YOUR_AUTH_CODE"
   ```
   The response's `refresh_token` does not expire (unless revoked) — put it in `.env` as
   `ZOHO_REFRESH_TOKEN`. This is the only manual step; the app refreshes access tokens itself.
6. Get your `organization_id`: `GET https://www.zohoapis.in/books/v3/organizations` with an
   access token, or find it in Zoho Books → Settings → Organization Profile.

## Known gaps before this is production-ready

- **E-way Bill API**: waiting on the spec to be provided; `src/eway_bill_client.py` is a stub.
- **Zoho filter support**: `find_bill_by_number` in `src/zoho_client.py` assumes server-side
  filtering by `bill_number` — this isn't confirmed in Zoho's public docs and needs verifying
  against a sandbox org. `find_vendor_by_gstin` no longer relies on server-side `gst_no`
  filtering — it lists all vendor contacts once per `ZohoClient` instance (cheap) and matches
  primary GSTINs from that list directly. If a GSTIN doesn't match any vendor's primary GSTIN,
  it falls back to additional GSTINs: characters 3-12 of a GSTIN are the entity's PAN, shared by
  every GSTIN (primary or additional) issued to that entity, so the fallback narrows to vendors
  whose primary GSTIN shares the same PAN and only calls `/contacts/{contact_id}/taxinfo` for
  that (usually tiny) candidate set, instead of every vendor in the org. Confirmed empirically
  (undocumented in Zoho's public API docs): that endpoint returns a `tax_info_list` array of
  `{tax_info_id, tax_registration_no, place_of_supply, is_primary, trader_name, legal_name}`
  dicts — `_get_taxinfo` takes whichever list-of-dicts value is present rather than hardcoding
  the `tax_info_list` key, in case it varies across orgs/plans.
- **Bill creation / attachment / post-run outcome report** are not yet wired into `app.py`.
- Duplicate checking against **previously created bills** currently only checks the local
  `local_state.db` (populated by this app itself) — it does not yet cross-check Zoho directly for
  bills created outside this tool.

import streamlit as st

from src.bill_creator import create_bills_for_approved
from src.config import load_app_auth_config, load_eway_bill_config, load_zoho_config
from src.excel_parser import parse_eway_bill_mapping, parse_invoice_sheet, parse_item_catalog
from src.eway_bill_client import EwayBillClient
from src.item_catalog import ItemCatalog
from src.pdf_matcher import index_pdfs_by_invoice_number
from src.report import build_match_report, ready_invoice_numbers
from src.zoho_client import ZohoClient

st.set_page_config(page_title="Bulk Bill Automation", layout="wide")


def _check_login() -> bool:
    if st.session_state.get("authenticated"):
        return True

    st.title("Bulk Bill Creation — Zoho + E-way Bill")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        auth = load_app_auth_config()
        if username == auth.username and password == auth.password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Invalid username or password.")

    return False


if not _check_login():
    st.stop()

st.title("Bulk Bill Creation — Zoho + E-way Bill")

env_choice = st.radio("Zoho environment", ["Live", "Test"], horizontal=True)
zoho_environment = "test" if env_choice == "Test" else "live"
if zoho_environment == "live":
    st.warning("Live selected -- bills will be created in the production Zoho org.")
else:
    st.info("Test selected -- bills will be created in the sandbox/test Zoho org.")

st.markdown(
    "Upload the invoice sheet, the E-way Bill mapping sheet "
    "(invoice_number -> eway_bill_number), the Zoho item export (Items module "
    "-> Export, \"Item\" sheet), and the ZIP of invoice PDFs. "
    "Excel (.xlsx/.xls) or .csv both work for the sheets. "
    "Review the match report below before approving any rows for creation."
)

excel_file = st.file_uploader("Invoice sheet", type=["xlsx", "xls", "csv"])
eway_mapping_file = st.file_uploader("E-way Bill mapping sheet", type=["xlsx", "xls", "csv"])
item_catalog_file = st.file_uploader("Zoho item export", type=["xlsx", "xls", "csv"])
zip_file = st.file_uploader("Invoice PDFs (ZIP)", type=["zip"])

if excel_file and eway_mapping_file and item_catalog_file and zip_file:
    if st.button("Generate match report"):
        status = st.status("Generating match report...", expanded=True)
        try:
            status.write("Parsing invoice sheet...")
            df = parse_invoice_sheet(excel_file)
            status.write(f"Parsed {len(df)} row(s), {df['invoice_number'].nunique()} unique invoice(s).")

            status.write("Parsing E-way Bill mapping sheet...")
            eway_bill_mapping = parse_eway_bill_mapping(eway_mapping_file)
            status.write(f"Parsed {len(eway_bill_mapping)} invoice -> E-way Bill number mapping(s).")

            status.write("Parsing Zoho item export...")
            item_catalog_df = parse_item_catalog(item_catalog_file)
            item_catalog = ItemCatalog(item_catalog_df)
            status.write(f"Loaded {len(item_catalog_df)} item(s) from the export.")

            status.write("Indexing invoice PDFs from the ZIP...")
            pdf_index = index_pdfs_by_invoice_number(zip_file)
            status.write(f"Found {len(pdf_index)} PDF(s) in the ZIP.")

            status.write(f"Checking each row against Zoho ({env_choice}) and the E-way Bill API...")
            zoho = ZohoClient(load_zoho_config(zoho_environment))
            eway = EwayBillClient(load_eway_bill_config())
            report_df = build_match_report(df, pdf_index, eway_bill_mapping, zoho, eway, item_catalog)

            st.session_state["report_df"] = report_df
            st.session_state["parsed_df"] = df
            st.session_state["pdf_index"] = pdf_index
            st.session_state["eway_bill_mapping"] = eway_bill_mapping
            st.session_state["item_catalog"] = item_catalog
            status.update(label=f"Match report ready -- {len(report_df)} row(s).", state="complete", expanded=False)
        except Exception as exc:  # noqa: BLE001 -- surfaced directly to the user, with full traceback
            status.update(label="Failed to build match report", state="error", expanded=True)
            st.exception(exc)

if "report_df" in st.session_state:
    report_df = st.session_state["report_df"]
    st.subheader("Match report")
    st.dataframe(report_df, use_container_width=True)

    ready_invoices = ready_invoice_numbers(report_df)
    st.subheader("Select invoices to approve")
    st.caption(
        "Each invoice becomes one bill with one line item per row shown above -- "
        "an invoice is only selectable here if every one of its rows is ready."
    )
    selected = st.multiselect(
        "Ready invoices",
        options=ready_invoices,
        default=ready_invoices,
    )

    if st.button("Approve and create bills", disabled=not selected):
        status = st.status(f"Creating {len(selected)} bill(s) in Zoho ({env_choice})...", expanded=True)
        done_count = [0]

        def _log_outcome(outcome):
            done_count[0] += 1
            icon = {"success": "✅", "partial": "⚠️", "failed": "❌"}.get(outcome.status, "•")
            detail = f" -- {outcome.error}" if outcome.error else ""
            status.write(
                f"{icon} [{done_count[0]}/{len(selected)}] {outcome.invoice_number}: "
                f"{outcome.status}{detail}"
            )

        try:
            zoho = ZohoClient(load_zoho_config(zoho_environment))
            eway = EwayBillClient(load_eway_bill_config())
            outcome_df = create_bills_for_approved(
                st.session_state["parsed_df"],
                selected,
                st.session_state["pdf_index"],
                st.session_state["eway_bill_mapping"],
                zoho,
                eway,
                st.session_state["item_catalog"],
                on_result=_log_outcome,
            )
            st.session_state["outcome_df"] = outcome_df
            succeeded = (outcome_df["status"] == "success").sum()
            status.update(
                label=f"Done -- {succeeded}/{len(outcome_df)} bill(s) fully succeeded.",
                state="complete",
                expanded=True,
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced directly to the user, with full traceback
            status.update(label="Failed to create bills", state="error", expanded=True)
            st.exception(exc)

if "outcome_df" in st.session_state:
    st.subheader("Post-run outcome report")
    st.dataframe(st.session_state["outcome_df"], use_container_width=True)

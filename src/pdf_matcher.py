import os
import shutil
import tempfile
import zipfile

# Prefix used to identify our own extraction dirs under the system temp dir,
# so a fresh one can be made per upload without ever mixing in PDFs left
# over from a previous "Generate match report" click or a previous session.
_EXTRACT_DIR_PREFIX = "bulk_bill_automation_pdfs_"


def index_pdfs_by_invoice_number(zip_file) -> dict[str, str]:
    """Extracts the ZIP into a fresh temp directory and returns
    {invoice_number: extracted_pdf_path} for only the PDFs in *this* ZIP.

    Assumes each PDF's filename (minus extension) is the invoice/bill number.
    """
    for name in os.listdir(tempfile.gettempdir()):
        if name.startswith(_EXTRACT_DIR_PREFIX):
            shutil.rmtree(os.path.join(tempfile.gettempdir(), name), ignore_errors=True)

    extract_dir = tempfile.mkdtemp(prefix=_EXTRACT_DIR_PREFIX)

    with zipfile.ZipFile(zip_file) as zf:
        zf.extractall(extract_dir)

    index = {}
    for root, _dirs, files in os.walk(extract_dir):
        for name in files:
            if name.lower().endswith(".pdf"):
                invoice_number = os.path.splitext(name)[0].strip()
                index[invoice_number] = os.path.join(root, name)
    return index

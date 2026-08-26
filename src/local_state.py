import sqlite3
from contextlib import contextmanager

from .config import LOCAL_STATE_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_bills (
    invoice_number TEXT PRIMARY KEY,
    eway_bill_number TEXT,
    zoho_bill_id TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(LOCAL_STATE_DB_PATH)
    try:
        conn.execute(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_result(invoice_number: str, eway_bill_number: str | None, zoho_bill_id: str | None, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO processed_bills (invoice_number, eway_bill_number, zoho_bill_id, status) "
            "VALUES (?, ?, ?, ?)",
            (invoice_number, eway_bill_number, zoho_bill_id, status),
        )

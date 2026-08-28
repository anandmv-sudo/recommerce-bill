import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class ZohoConfig:
    dc: str
    client_id: str
    client_secret: str
    refresh_token: str
    organization_id: str
    purchase_account_id: str
    branch_id: str

    @property
    def accounts_base_url(self) -> str:
        return f"https://accounts.zoho.{self.dc}"

    @property
    def api_base_url(self) -> str:
        return f"https://www.zohoapis.{self.dc}/books/v3"


@dataclass(frozen=True)
class EwayBillConfig:
    base_url: str
    gsp_app_id: str
    gsp_app_secret: str
    gstin: str
    username: str
    password: str


def load_zoho_config(environment: str = "live") -> ZohoConfig:
    """environment: "live" reads the production ZOHO_* vars; "test" reads the
    ZOHO_TEST_* vars for a separate sandbox org."""
    prefix = "ZOHO_TEST_" if environment == "test" else "ZOHO_"
    return ZohoConfig(
        dc=os.environ.get(f"{prefix}DC", "in"),
        client_id=os.environ[f"{prefix}CLIENT_ID"],
        client_secret=os.environ[f"{prefix}CLIENT_SECRET"],
        refresh_token=os.environ[f"{prefix}REFRESH_TOKEN"],
        organization_id=os.environ[f"{prefix}ORGANIZATION_ID"],
        purchase_account_id=os.environ[f"{prefix}PURCHASE_ACCOUNT_ID"],
        branch_id=os.environ[f"{prefix}BRANCH_ID"],
    )


def load_eway_bill_config() -> EwayBillConfig:
    return EwayBillConfig(
        base_url=os.environ.get("EWAYBILL_API_BASE_URL", ""),
        gsp_app_id=os.environ.get("EWAYBILL_GSP_APP_ID", ""),
        gsp_app_secret=os.environ.get("EWAYBILL_GSP_APP_SECRET", ""),
        gstin=os.environ.get("EWAYBILL_GSTIN", ""),
        username=os.environ.get("EWAYBILL_USERNAME", ""),
        password=os.environ.get("EWAYBILL_PASSWORD", ""),
    )


LOCAL_STATE_DB_PATH = os.environ.get("LOCAL_STATE_DB_PATH", "./local_state.db")


@dataclass(frozen=True)
class AppAuthConfig:
    username: str
    password: str


def load_app_auth_config() -> AppAuthConfig:
    return AppAuthConfig(
        username=os.environ["APP_USERNAME"],
        password=os.environ["APP_PASSWORD"],
    )

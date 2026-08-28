import threading
import time

import requests

from .config import EwayBillConfig

# Keyed by gsp_app_id, same reasoning as zoho_auth.py's client_id keying --
# switching between live/test EwayBillConfigs shouldn't reuse a token minted
# for the other app id.
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def get_access_token(cfg: EwayBillConfig) -> str:
    """Returns a cached GSP access token for this app id, refreshing it once
    it's close to expiry.

    Adaequare's GSP token (POST /gsp/authenticate?grant_type=token, with the
    gspappid/gspappsecret long-lived app credentials as headers) is
    long-lived (~17 days per an observed expires_in of 1502016 seconds) but
    still expires, so this mints a fresh one automatically instead of
    relying on a static token pasted into .env. Locked so a burst of
    concurrent calls (e.g. report.py's thread-pooled E-way Bill prefetch)
    doesn't fire off a duplicate token exchange per thread before the first
    one populates the cache.
    """
    cached = _token_cache.get(cfg.gsp_app_id)
    if cached and time.time() < cached[1]:
        return cached[0]

    with _token_lock:
        cached = _token_cache.get(cfg.gsp_app_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        return _refresh_access_token(cfg)


def invalidate(cfg: EwayBillConfig) -> None:
    """Drops the cached token for this app id, forcing the next
    get_access_token call to mint a fresh one -- used when a call using the
    cached token is rejected as unauthorized despite not looking expired yet
    (e.g. revoked early, clock drift)."""
    with _token_lock:
        _token_cache.pop(cfg.gsp_app_id, None)


def _refresh_access_token(cfg: EwayBillConfig) -> str:
    response = requests.post(
        f"{cfg.base_url}/gsp/authenticate?grant_type=token",
        headers={
            "gspappid": cfg.gsp_app_id,
            "gspappsecret": cfg.gsp_app_secret,
        },
        timeout=30,
    )
    payload = response.json()

    if "access_token" not in payload:
        # Do not include request/response details here -- credentials must
        # never end up in an exception message or log.
        raise RuntimeError(f"E-way Bill GSP token exchange failed with error: {payload.get('error', 'unknown')}")

    access_token = payload["access_token"]
    expires_at = time.time() + payload.get("expires_in", 3600) - 60
    _token_cache[cfg.gsp_app_id] = (access_token, expires_at)
    return access_token

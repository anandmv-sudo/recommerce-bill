import threading
import time

import requests

from .config import ZohoConfig

# Keyed by client_id so switching between the live and test orgs (different
# ZohoConfig, different client_id) within the same process doesn't reuse the
# wrong org's cached token -- caching a single global token caused exactly
# that bug (a live-org token being sent to the test org, rejected with a
# company-mismatch error).
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = threading.Lock()


def get_access_token(cfg: ZohoConfig) -> str:
    """Returns a cached access token for this config's org, refreshing it
    once it's close to expiry.

    Zoho access tokens are short-lived (~1hr); the refresh token from the
    one-time Self Client authorization-code exchange does not expire unless
    revoked, so this is the only token exchange this app needs to do at runtime.
    Locked so a burst of concurrent calls (e.g. a thread-pooled match report)
    doesn't fire off a duplicate token exchange per thread before the first
    one populates the cache.
    """
    cached = _token_cache.get(cfg.client_id)
    if cached and time.time() < cached[1]:
        return cached[0]

    with _token_lock:
        cached = _token_cache.get(cfg.client_id)
        if cached and time.time() < cached[1]:
            return cached[0]

        return _refresh_access_token(cfg)


def _refresh_access_token(cfg: ZohoConfig) -> str:
    response = requests.post(
        f"{cfg.accounts_base_url}/oauth/v2/token",
        data={
            "refresh_token": cfg.refresh_token,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    payload = response.json()

    if "access_token" not in payload:
        # Do not include request/response details here -- credentials must
        # never end up in an exception message or log (unlike request.raise_for_status(),
        # which echoes the full request URL and would leak them if params were used above).
        raise RuntimeError(f"Zoho token exchange failed with error: {payload.get('error', 'unknown')}")

    access_token = payload["access_token"]
    expires_at = time.time() + payload.get("expires_in", 3600) - 60
    _token_cache[cfg.client_id] = (access_token, expires_at)
    return access_token

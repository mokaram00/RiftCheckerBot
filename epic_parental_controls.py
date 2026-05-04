"""
Epic account web: parental controls (same ``EPIC_BEARER_TOKEN`` cookie + curl_cffi as ``orders.py``).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from curl_cffi import requests as curl_requests

# Match ``orders.py`` / Epic account web requests (Chrome TLS fingerprint).
_EPIC_ACCOUNT_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

PARENTAL_CONTROLS_GET_URL = (
    "https://accounts.epicgames.com/account/v2/parental-controls/get"
)


def fetch_parental_controls_get_sync(
    access_token: str,
    *,
    lang: str = "en-US",
    impersonate: str = "chrome110",
) -> Optional[Dict[str, Any]]:
    """
    GET ``/account/v2/parental-controls/get``.

    Typical body: ``{ "success": true, "data": { "pinExists": true, ... } }``.
    """
    if not (access_token or "").strip():
        return None
    req_headers = {
        "User-Agent": _EPIC_ACCOUNT_WEB_UA,
        "Cookie": f"EPIC_BEARER_TOKEN={access_token};",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        response = curl_requests.get(
            PARENTAL_CONTROLS_GET_URL,
            headers=req_headers,
            params={"lang": lang},
            impersonate=impersonate,
            timeout=35,
        )
    except OSError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def parental_controls_pin_exists(payload: Optional[Dict[str, Any]]) -> Optional[bool]:
    """``True`` / ``False`` from ``data.pinExists``; ``None`` if missing or request failed."""
    if not payload or not isinstance(payload, dict):
        return None
    if not payload.get("success"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or "pinExists" not in data:
        return None
    return bool(data.get("pinExists"))

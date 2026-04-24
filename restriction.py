"""
GET Epic Help API: restriction-removal availability (relink / cooldown).
Uses curl_cffi (Chrome TLS) like orders.py — same cookie as other epicgames.com calls.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from curl_cffi import requests as curl_requests

from admin_api_summary import looks_like_cloudflare_html, session_add
from epic_response_log import epic_log_from_response

HELP_RESTRICTION_REMOVAL_URL = (
    "https://www.epicgames.com/help/api/restriction-removal/availability"
)
DEFAULT_IMPERSONATE = "chrome110"


def fetch_restriction_removal_availability_sync(
    access_token: str,
    *,
    impersonate: str = DEFAULT_IMPERSONATE,
    timeout: float = 60.0,
) -> Optional[Dict[str, Any]]:
    """
    Returns JSON with ``restrictions`` / ``auths`` (see Epic Help), or ``None`` on failure.
    """
    if not (access_token or "").strip():
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.epicgames.com/help/",
        "Cookie": f"EPIC_BEARER_TOKEN={access_token.strip()};",
    }
    try:
        r = curl_requests.get(
            HELP_RESTRICTION_REMOVAL_URL,
            headers=headers,
            impersonate=impersonate,
            timeout=timeout,
        )
        epic_log_from_response(
            "help_restriction_removal_availability",
            "GET",
            getattr(r, "url", None) or HELP_RESTRICTION_REMOVAL_URL,
            r,
        )
        st = int(getattr(r, "status_code", 0) or 0)
        t = getattr(r, "text", None) or ""
        if st == 200:
            session_add("Help API · restriction-removal/availability", True, "HTTP 200")
        else:
            d = f"HTTP {st}"
            if looks_like_cloudflare_html(t):
                d += " · Cloudflare/WAF"
            session_add("Help API · restriction-removal/availability", False, d)
        if r.status_code != 200:
            logging.warning(
                "restriction-removal HTTP %s: %s",
                r.status_code,
                (r.text or "")[:400],
            )
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        logging.warning("fetch_restriction_removal_availability_sync: %s", e)
        return None


def main() -> int:
    """CLI: set EPIC_BEARER_TOKEN or edit cookie in code for quick tests."""
    import json
    import os
    import sys

    token = os.environ.get("EPIC_BEARER_TOKEN", "").strip()
    if not token:
        print("Set EPIC_BEARER_TOKEN", file=sys.stderr)
        return 2
    data = fetch_restriction_removal_availability_sync(token)
    if data is None:
        return 1
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

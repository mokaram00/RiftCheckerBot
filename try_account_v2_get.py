#!/usr/bin/env python3
"""
تجربة: GET https://accounts.epicgames.com/account/v2
نفس رؤوس البوت (كوكي EPIC_BEARER_TOKEN + curl_cffi).

  set EPIC_BEARER_TOKEN=...   # أو
  python try_account_v2_get.py YOUR_ACCESS_TOKEN

اختياري:
  --impersonate chrome120
  --out account_v2.html       حفظ الـ body كامل
  --max-attempts 1            تكرار كما في البوت (افتراضي 1)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ACCOUNT_V2 = "https://accounts.epicgames.com/account/v2"
_PRELOAD = "window.account_dataPreload"


def main() -> int:
    p = argparse.ArgumentParser(description="GET /account/v2 (Epic account portal HTML)")
    p.add_argument("token", nargs="?", help="Access token (or set EPIC_BEARER_TOKEN)")
    p.add_argument(
        "--impersonate",
        default="chrome120",
        help="curl_cffi TLS impersonation (e.g. chrome110, chrome120, chrome131)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
    )
    p.add_argument(
        "--out",
        type=Path,
        help="Write full HTML to this file",
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="Retry count (with 2s delay like bot optional)",
    )
    args = p.parse_args()

    token = (args.token or os.environ.get("EPIC_BEARER_TOKEN", "") or "").strip()
    if not token:
        print("ضع التوكن: EPIC_BEARER_TOKEN أو كوسيط: python try_account_v2_get.py <token>", file=sys.stderr)
        return 2

    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        print("pip install curl-cffi", file=sys.stderr)
        return 2

    headers = {
        "Cookie": f"EPIC_BEARER_TOKEN={token}",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_status = 0
    last_text = ""
    for attempt in range(max(1, args.max_attempts)):
        try:
            r = curl_requests.get(
                ACCOUNT_V2,
                headers=headers,
                impersonate=args.impersonate,
                allow_redirects=True,
                timeout=args.timeout,
            )
        except OSError as e:
            print(f"attempt {attempt + 1}: request error: {e}", file=sys.stderr)
            if attempt >= args.max_attempts - 1:
                return 1
            continue

        last_status = r.status_code
        last_text = r.text or ""
        has_preload = _PRELOAD in last_text
        print(f"attempt {attempt + 1}/{args.max_attempts}")
        print(f"  status:     {last_status}")
        print(f"  url:        {getattr(r, 'url', ACCOUNT_V2)}")
        print(f"  body bytes: {len(last_text)}")
        print(f"  has '{_PRELOAD}': {has_preload}")
        if not has_preload and last_text:
            head = last_text.lstrip()[:400].replace("\n", " ")
            print(f"  head:       {head!r}…")

        if has_preload:
            print("  (preload found — good for 2FA JSON extract in bot)")
            break
        if attempt < args.max_attempts - 1:
            import time

            time.sleep(2.0 * (1 + attempt * 0.15))

    if args.out:
        args.out.write_text(last_text, encoding="utf-8")
        print(f"saved: {args.out.resolve()}")

    # اختياري: نفس الـ util في المشروع
    try:
        from admin_api_summary import looks_like_cloudflare_html, non_json_api_response_hint

        if last_text and looks_like_cloudflare_html(last_text):
            print("  hint:       " + non_json_api_response_hint(last_text, what="body")[:300])
    except Exception:
        pass

    return 0 if (last_status == 200 and _PRELOAD in last_text) else 1


if __name__ == "__main__":
    raise SystemExit(main())

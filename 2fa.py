#!/usr/bin/env python3
"""
Epic account helpers (local CLI):

  - بدون --orders: نفس منطق البوت — curl_cffi عبر epic_auth.fetch_account_portal_v2_html_sync + utils للتحليل.
  - orders: تصدير طلبات الدفع كما كان سابقاً (يتطلب curl_cffi مباشرة لأن الـ API يعيد JSON).

التوكن:
  EPIC_BEARER_TOKEN=...  أو  python 2fa.py YOUR_TOKEN

أمثلة:
  python 2fa.py
  python 2fa.py --orders --out orders.json
  python 2fa.py --impersonate chrome131
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils import format_datetime_utc_ms_dual

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from epic_auth import fetch_account_portal_v2_html_sync
from epic_response_log import epic_log_from_response
from utils import extract_account_portal_preload_json, format_portal_2fa_methods_text

ORDERS_URL = "https://accounts.epicgames.com/account/v2/"
LIST_PARAMS = {
    "count": "500",
    "sortDir": "DESC",
    "sortBy": "DATE",
    "locale": "en-US",
}


def _resolve_token(cli_token: str) -> str:
    t = (cli_token or os.environ.get("EPIC_BEARER_TOKEN", "") or "").strip()
    return t


def _minor_to_decimal(amount: Optional[int]) -> Optional[float]:
    if amount is None:
        return None
    return round(amount / 100.0, 2)


def _money_block(src: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not src:
        return None
    amt = src.get("amount")
    cur = src.get("currency")
    out: Dict[str, Any] = {"currency": cur}
    if isinstance(amt, int):
        out["amount_minor"] = amt
        out["amount"] = _minor_to_decimal(amt)
    elif amt is not None:
        out["amount"] = amt
    return out


def _clean_item(it: Dict[str, Any]) -> Dict[str, Any]:
    gift = it.get("giftRecipient")
    return {
        "description": it.get("description"),
        "quantity": it.get("quantity"),
        "line": _money_block({"amount": it.get("amount"), "currency": it.get("currency")}),
        "offerId": it.get("offerId"),
        "namespace": it.get("namespace"),
        "giftRecipient": gift if gift else None,
    }


def _clean_tx(t: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "transactionId": t.get("transactionId"),
        "paymentGatewayType": t.get("paymentGatewayType"),
        "paymentMethodType": t.get("paymentMethodType"),
        "paymentMethodSubType": t.get("paymentMethodSubType"),
        "paid": _money_block({"amount": t.get("amount"), "currency": t.get("currency")}),
        "billingAccountName": t.get("billingAccountName"),
    }


def _clean_promo(p: Dict[str, Any]) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "type": p.get("type"),
        "index": p.get("index"),
    }
    title = p.get("title")
    if title:
        row["title"] = title
    m = _money_block({"amount": p.get("amount"), "currency": p.get("currency")})
    if m:
        row["discount"] = m
    return row


def parse_order(raw: Dict[str, Any]) -> Dict[str, Any]:
    ms = raw.get("createdAtMillis")
    created_utc: Optional[str] = None
    if isinstance(ms, (int, float)):
        created_utc = format_datetime_utc_ms_dual(ms)

    tax = raw.get("tax") or {}
    tax_out: Optional[Dict[str, Any]] = None
    if tax:
        tax_out = {
            "status": tax.get("status"),
            "tax": _money_block({"amount": tax.get("amount"), "currency": tax.get("currency")}) or {},
        }

    txs = [_clean_tx(t) for t in (raw.get("transactions") or [])]
    promos = [_clean_promo(p) for p in (raw.get("promotions") or [])]

    return {
        "orderId": raw.get("orderId"),
        "orderType": raw.get("orderType"),
        "createdAt": created_utc,
        "marketplaceName": raw.get("marketplaceName"),
        "merchantGroup": raw.get("merchantGroup"),
        "canSendReceipt": raw.get("canSendReceipt"),
        "items": [_clean_item(x) for x in (raw.get("items") or [])],
        "subtotal": _money_block(raw.get("subtotal")),
        "tax": tax_out,
        "convenienceFee": _money_block(raw.get("convenienceFee")),
        "total": _money_block(raw.get("total")),
        "transactions": txs if txs else None,
        "promotions": promos if promos else None,
    }


def fetch_all_orders(token: str, *, impersonate: str = "chrome120", timeout: float = 60.0) -> List[Dict[str, Any]]:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as e:
        raise RuntimeError("pip install curl-cffi — مطلوب لطلبات Epic خلف Cloudflare") from e

    collected: List[Dict[str, Any]] = []
    params: Dict[str, str] = dict(LIST_PARAMS)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Cookie": f"EPIC_BEARER_TOKEN={token}",
        "Accept": "application/json, text/plain, */*",
    }

    while True:
        response = curl_requests.get(
            ORDERS_URL,
            headers=headers,
            params=params,
            impersonate=impersonate,
            timeout=timeout,
        )
        epic_log_from_response(
            "account_v2_payment_orders",
            "GET",
            getattr(response, "url", None) or ORDERS_URL,
            response,
        )
        try:
            data = response.json()
        except json.JSONDecodeError:
            print(response.text[:4000], file=sys.stderr)
            raise SystemExit(1) from None

        batch = data.get("orders") or []
        collected.extend(batch)

        npt = data.get("nextPageToken")
        if not npt:
            break
        params = dict(LIST_PARAMS)
        params["nextPageToken"] = str(npt)

    return collected


def cmd_2fa(
    token: str,
    impersonate: str,
    *,
    max_attempts: Optional[int] = None,
    retry_delay_sec: Optional[float] = None,
) -> int:
    status, html, backend = fetch_account_portal_v2_html_sync(
        token,
        impersonate=impersonate,
        max_attempts=max_attempts,
        retry_delay_sec=retry_delay_sec,
    )
    print(f"backend={backend} status={status} bytes={len(html)}", file=sys.stderr)
    if backend == "curl_cffi_missing":
        print("ثبّت: pip install curl-cffi", file=sys.stderr)
        return 1
    if status != 200 or not html:
        print("تعذّر تحميل صفحة الحساب", file=sys.stderr)
        return 1
    preload = extract_account_portal_preload_json(html)
    if not preload:
        print("تعذّر parse لـ window.account_dataPreload", file=sys.stderr)
        return 1
    print(format_portal_2fa_methods_text(preload))
    tfa = preload.get("twoFactorAuthentication")
    if tfa:
        print(json.dumps(tfa, indent=2, ensure_ascii=False))
    return 0


def cmd_orders(token: str, out_path: Path, impersonate: str) -> int:
    orders_raw = fetch_all_orders(token, impersonate=impersonate)
    cleaned = {"order_count": len(orders_raw), "orders": [parse_order(o) for o in orders_raw]}
    out_text = json.dumps(cleaned, indent=2, ensure_ascii=False)
    out_path.write_text(out_text, encoding="utf-8")
    print(f"Saved {cleaned['order_count']} orders -> {out_path}", file=sys.stderr)
    print(out_text)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Epic: 2FA (portal) أو تصدير الطلبات")
    p.add_argument("token", nargs="?", default="", help="أو عيّن EPIC_BEARER_TOKEN")
    p.add_argument("--orders", action="store_true", help="تصدير الطلبات إلى ملف JSON (بدونها: عرض 2FA من البوابة)")
    p.add_argument("--out", type=Path, default=ROOT / "orders.json", help="مسار مخرجات --orders")
    p.add_argument("--impersonate", default="chrome120", help="curl_cffi impersonate (مثلاً chrome131)")
    p.add_argument(
        "--retries",
        type=int,
        default=None,
        help="عدد محاولات جلب /account/v2 (افتراضي: EPIC_ACCOUNT_PORTAL_MAX_ATTEMPTS أو 5)",
    )
    p.add_argument(
        "--retry-delay",
        type=float,
        default=None,
        dest="retry_delay",
        help="ثوانٍ بين المحاولات (افتراضي: EPIC_ACCOUNT_PORTAL_RETRY_DELAY أو 2)",
    )
    args = p.parse_args()

    token = _resolve_token(args.token)
    if not token:
        print("مرّر التوكن أو عيّن EPIC_BEARER_TOKEN", file=sys.stderr)
        return 2

    if args.orders:
        return cmd_orders(token, args.out, args.impersonate)
    # default: 2fa
    return cmd_2fa(
        token,
        args.impersonate,
        max_attempts=args.retries,
        retry_delay_sec=args.retry_delay,
    )


if __name__ == "__main__":
    raise SystemExit(main())

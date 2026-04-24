import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from curl_cffi import requests

from admin_api_summary import looks_like_cloudflare_html, session_add
from epic_response_log import epic_log_from_response
from utils import format_datetime_utc_ms_dual

ORDER_HISTORY_AJAX_URL = "https://www.epicgames.com/account/v2/payment/ajaxGetOrderHistory"
url = ORDER_HISTORY_AJAX_URL
# Default headers for module-level tooling only; real calls use ``fetch_order_history_sync(access_token)``.
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "cookie": "EPIC_BEARER_TOKEN=;",
}

LIST_PARAMS = {
    "count": "500",
    "sortDir": "DESC",
    "sortBy": "DATE",
    "locale": "en-US",
}


def _epic_bearer_from_cookie(cookie_header: str) -> str:
    for part in cookie_header.split(";"):
        p = part.strip()
        if p.upper().startswith("EPIC_BEARER_TOKEN="):
            return p.split("=", 1)[1].strip()
    return ""


def fetch_order_history_sync(access_token: str) -> List[Dict[str, Any]]:
    """Same loop as ``fetch_all_orders``, cookie built from ``access_token`` (curl_cffi)."""
    collected: List[Dict[str, Any]] = []
    params: Dict[str, str] = dict(LIST_PARAMS)
    req_headers = {
        "User-Agent": headers["User-Agent"],
        "cookie": f"EPIC_BEARER_TOKEN={access_token};",
    }
    page_idx = 0
    while True:
        page_idx += 1
        response = requests.get(
            url, headers=req_headers, params=params, impersonate="chrome110"
        )
        epic_log_from_response(
            "ajaxGetOrderHistory",
            "GET",
            getattr(response, "url", None) or url,
            response,
        )
        st = int(getattr(response, "status_code", 0) or 0)
        raw = getattr(response, "text", None) or ""
        if st != 200:
            d = f"p.{page_idx} · HTTP {st}"
            if looks_like_cloudflare_html(raw):
                d += " · Cloudflare/WAF (HTML)"
            session_add("order · ajaxGetOrderHistory", False, d)
            raise RuntimeError(d)
        try:
            data = response.json()
        except json.JSONDecodeError:
            d = f"p.{page_idx} · not JSON"
            if looks_like_cloudflare_html(raw):
                d += " · likely Cloudflare / HTML"
            session_add("order · ajaxGetOrderHistory", False, d)
            print(response.text, file=sys.stderr)
            raise
        batch = data.get("orders") or []
        collected.extend(batch)
        npt = data.get("nextPageToken")
        if not npt:
            break
        params = dict(LIST_PARAMS)
        params["nextPageToken"] = str(npt)
    session_add(
        "order · ajaxGetOrderHistory",
        True,
        f"OK · {len(collected)} row(s) · {page_idx} page(s)",
    )
    return collected


def fetch_order_history_sync_with_retry(
    access_token: str,
    *,
    attempts: int = 3,
    delay_sec: float = 1.5,
) -> List[Dict[str, Any]]:
    last_exc = None
    for i in range(attempts):
        try:
            return fetch_order_history_sync(access_token)
        except Exception as e:
            last_exc = e
            logging.warning(
                "fetch_order_history_raw attempt %s/%s failed: %s",
                i + 1,
                attempts,
                e,
            )
            if i < attempts - 1:
                time.sleep(delay_sec * (i + 1))
    if last_exc:
        logging.warning(
            "fetch_order_history_raw giving up after %s attempts", attempts
        )
        session_add(
            "order · ajaxGetOrderHistory (retries)",
            False,
            str(last_exc)[:500],
        )
    return []


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


def build_full_export(
    raw_orders: List[Dict[str, Any]],
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Same shape as saved ``orders.json`` (order_count + cleaned orders)."""
    parsed = [parse_order(o) for o in raw_orders]
    out: Dict[str, Any] = {"order_count": len(parsed), "orders": parsed}
    if account_id:
        out["account_id"] = account_id
    return out


def build_payment_methods_export(
    parsed_orders: List[Dict[str, Any]],
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Unique payment methods aggregated from parsed order ``transactions``
    (gateway / method / subtype / billing name + order ids + usage count).
    """
    order_count = len(parsed_orders)
    transactions_total_count = 0
    for o in parsed_orders:
        txs = o.get("transactions")
        if isinstance(txs, list):
            transactions_total_count += len(txs)

    buckets: Dict[str, Dict[str, Any]] = {}
    for o in parsed_orders:
        oid = o.get("orderId")
        txs = o.get("transactions")
        if not txs:
            continue
        for tx in txs:
            if not tx:
                continue
            gw = tx.get("paymentGatewayType")
            mt = tx.get("paymentMethodType")
            st = tx.get("paymentMethodSubType")
            bn = tx.get("billingAccountName")
            key = "|".join(str(x or "") for x in (gw, mt, st, bn))
            if key not in buckets:
                buckets[key] = {
                    "paymentGatewayType": gw,
                    "paymentMethodType": mt,
                    "paymentMethodSubType": st,
                    "billingAccountName": bn,
                    "order_ids": [],
                    "transaction_count": 0,
                }
            b = buckets[key]
            b["transaction_count"] += 1
            if oid and oid not in b["order_ids"]:
                b["order_ids"].append(oid)
    methods = sorted(
        buckets.values(),
        key=lambda x: (-x["transaction_count"], str(x.get("paymentMethodType") or "")),
    )
    out: Dict[str, Any] = {
        "order_count": order_count,
        "transactions_total_count": transactions_total_count,
        "unique_payment_method_count": len(methods),
        "payment_methods": methods,
    }
    if account_id:
        out["account_id"] = account_id
    return out


def fetch_all_orders() -> List[Dict[str, Any]]:
    try:
        return fetch_order_history_sync(_epic_bearer_from_cookie(headers["cookie"]))
    except json.JSONDecodeError:
        raise SystemExit(1)


if __name__ == "__main__":
    orders_raw = fetch_all_orders()
    cleaned = build_full_export(orders_raw)
    out_text = json.dumps(cleaned, indent=2, ensure_ascii=False)
    out_path = Path(__file__).resolve().parent / "orders.json"
    out_path.write_text(out_text, encoding="utf-8")
    pm = build_payment_methods_export(cleaned["orders"])
    out2 = Path(__file__).resolve().parent / "orders2.json"
    out2.write_text(json.dumps(pm, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {cleaned['order_count']} orders -> {out_path}", file=sys.stderr)
    print(f"Saved payment methods -> {out2}", file=sys.stderr)
    print(out_text)

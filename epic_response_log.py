"""
Persist Epic (and related) HTTP JSON/text responses to ``epic_api_responses/`` when enabled.

Env: ``EPIC_SAVE_RESPONSES`` — set to ``0`` / ``false`` / ``no`` / ``off`` to disable.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

EPIC_RESPONSE_LOG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "epic_api_responses"
)


def epic_save_responses_enabled() -> bool:
    return os.environ.get("EPIC_SAVE_RESPONSES", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def epic_log_http_response(tag: str, method: str, url: str, status: int, body: Any) -> None:
    if not epic_save_responses_enabled():
        return
    try:
        os.makedirs(EPIC_RESPONSE_LOG_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)[:80]
        path = os.path.join(EPIC_RESPONSE_LOG_DIR, f"{ts}_{safe}.json")
        record = {
            "tag": tag,
            "method": method,
            "url": url,
            "status": status,
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "response": body,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"epic_response_log: could not write response log ({tag}): {e}")


def epic_log_from_response(tag: str, method: str, url: str, response) -> None:
    """Log JSON (or a text fragment) from ``requests`` or ``curl_cffi`` response objects."""
    if not epic_save_responses_enabled():
        return
    try:
        body = response.json()
    except (ValueError, TypeError):
        text = getattr(response, "text", None) or ""
        body = {"_non_json_body": text[:500_000]}
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    epic_log_http_response(tag, method, url, status, body)


def epic_log_json_or_text_response(
    tag: str, method: str, url: str, status: int, text: str
) -> None:
    """Parse body as JSON when possible; else store a text fragment (e.g. aiohttp bodies)."""
    if not epic_save_responses_enabled():
        return
    t = (text or "").strip()
    if not t:
        body: Any = None
    else:
        try:
            body = json.loads(t)
        except json.JSONDecodeError:
            body = {"_non_json_body": t[:500_000]}
    epic_log_http_response(tag, method, url, status, body)

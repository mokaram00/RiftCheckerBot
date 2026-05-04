"""
When a /login (post-auth) flow runs, per-API lines are sent only to
``API_SUMMARY_RECIPIENT`` (``6698006077`` by default). Set
``RIFT_API_SUMMARY_DISABLE=1`` to turn off the DM; tracing still runs.
"""
from __future__ import annotations

import os
import re
from html import escape as _html_escape
from typing import Any, Dict, List, Optional

API_SUMMARY_RECIPIENT: int = 6698006077


def summary_sending_enabled() -> bool:
    v = (os.environ.get("RIFT_API_SUMMARY_DISABLE") or "").strip().lower()
    return v not in ("1", "true", "yes", "on")


_CF = re.compile(
    r"cloudflare|__cf|cf[-_]ray|cdn-cgi/|/cdn-cgi/|"
    r"challenge[-\s]?platform|"
    r"__cf\$cv|__CF\$cv|"
    r"just[\s]a[\s]moment|checking your browser|"
    r"challenge[-\s]?(platform|iframe)|"
    r"ddos protection|attention required|"
    r"perlu ditinjau|enable\s+javascript|"
    r"static-assets-prod\.unrealengine\.com/account-portal",
    re.IGNORECASE | re.DOTALL,
)

_session: Optional[List[Dict[str, Any]]] = None


def looks_like_cloudflare_html(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    s = text[:12000]
    if _CF.search(s):
        return True
    low = s.lstrip().lower()
    if low.startswith("<!doctype") or (low.startswith("<html") and "<head" in low):
        if any(
            x in low
            for x in (
                "captcha",
                "challenge",
                "cloudflare",
                "cf-",
                "waf",
                "access denied",
                "blocked",
            )
        ):
            return True
    return False


def non_json_api_response_hint(text: str, *, what: str = "response") -> str:
    """
    When ``json.loads`` fails, explain whether the body looks like a Cloudflare
    challenge, Epic account-portal HTML, or other non-JSON — without dumping
    the full page (keeps process logs small).
    """
    if not text or not str(text).strip():
        return f"empty {what} (expected JSON)"
    t = str(text)[:20000]
    if re.search(r"__cf\$cv|cdn-cgi/challenge", t, re.I) or "challenge-platform" in t.lower():
        return (
            f"Cloudflare / browser challenge in {what} "
            f"(not API JSON — try different TLS fingerprint, or server IP is blocked)"
        )
    if looks_like_cloudflare_html(t):
        return f"WAF/Cloudflare-style HTML in {what} (not API JSON)"
    low = t.lstrip().lower()
    if low.startswith("<!doctype") or low.startswith("<html"):
        if "unrealengine.com" in t and "account-portal" in t:
            return (
                f"Epic account-portal HTML shell in {what} (blocked as bot / wrong cookies — not JSON)"
            )
        return f"HTML in {what} (expected JSON) — {what} may be a block or login page"
    return f"not valid JSON in {what} (first byte not {{ or [)"


def session_start() -> None:
    global _session
    _session = []


def session_add(name: str, ok: bool, detail: str = "") -> None:
    global _session
    if _session is None:
        return
    d = (detail or "").strip()
    if len(d) > 500:
        d = d[:500] + "…"
    _session.append({"name": name, "ok": bool(ok), "detail": d})


def session_get() -> List[Dict[str, Any]]:
    if _session is None:
        return []
    return list(_session)


def session_clear() -> None:
    global _session
    _session = None


def _esc(s: str) -> str:
    return _html_escape(s or "", quote=True)


def render_html_summary(
    *,
    from_chat_id: int,
    from_user_id: int,
    account_id: str,
    display_name: str,
) -> str:
    rows = session_get()
    out: list[str] = [
        "<b>Login API report</b>",
        f"from chat: <code>{int(from_chat_id)}</code>  user: <code>{int(from_user_id)}</code>",
        f"account: {_esc(str(display_name or '—'))}  <code>{_esc(str(account_id or '—'))}</code>",
        "",
    ]
    if not rows:
        out.append("<i>(no rows — session not instrumented?)</i>")
    else:
        for r in rows:
            st = "✅" if r.get("ok") else "❌"
            n = r.get("name", "?")
            d = (r.get("detail") or "").strip()
            line = f"{st} <b>{_esc(str(n))}</b>"
            if d:
                line += f"  —  {_esc(d)}"
            out.append(line)
    return "\n".join(out)

import aiohttp
import requests
import asyncio
import logging
import os
import platform
import json
import math
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from cosmetic import FortniteCosmetic
from admin_api_summary import looks_like_cloudflare_html, session_add
from epic_response_log import (
    epic_log_from_response as _requests_log_response,
    epic_log_http_response as _epic_log_http_response,
    epic_log_json_or_text_response,
    epic_save_responses_enabled as _epic_save_responses_enabled,
)
from orders import fetch_order_history_sync, fetch_order_history_sync_with_retry
from restriction import fetch_restriction_removal_availability_sync
from utils import (
    bool_to_emoji,
    empty_epic_account_email_info,
    extract_account_portal_preload_json,
    fetch_pci_payment_methods_via_xsrf_sync_with_retry,
    format_date_mmddyyyy,
    format_epic_iso_ddmmyy,
    normalize_epic_account_email_payload,
    _format_last_login_for_display,
)

_EPIC_ACCOUNT_ID_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# Persist Epic accountId → displayName (same host as ``resolve_epic_account_id_from_text_sync``).
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
_EPIC_PUBLIC_DISPLAY_NAMES_PATH = os.path.join(_CACHE_DIR, "epic_public_display_names.json")
_EPIC_DN_CACHE_LOCK = threading.RLock()
_EPIC_DN_CACHE: Optional[Dict[str, str]] = None


def _ensure_cache_dir() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _load_epic_display_name_cache_from_disk() -> Dict[str, str]:
    if not os.path.isfile(_EPIC_PUBLIC_DISPLAY_NAMES_PATH):
        return {}
    try:
        with open(_EPIC_PUBLIC_DISPLAY_NAMES_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and _EPIC_ACCOUNT_ID_HEX_RE.fullmatch(k):
            kk = k.lower().strip()
            vv = v.strip()
            if vv:
                out[kk] = vv
    return out


def _get_epic_display_name_cache_mut() -> Dict[str, str]:
    global _EPIC_DN_CACHE
    with _EPIC_DN_CACHE_LOCK:
        if _EPIC_DN_CACHE is None:
            _EPIC_DN_CACHE = _load_epic_display_name_cache_from_disk()
        return _EPIC_DN_CACHE


def _save_epic_display_name_cache_to_disk(cache: Dict[str, str]) -> None:
    _ensure_cache_dir()
    path = _EPIC_PUBLIC_DISPLAY_NAMES_PATH
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, sort_keys=True, indent=0)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning("epic_public_display_names cache save: %s", e)


def _debug_format_token_line(label: str, token: str) -> str:
    """Pretty line for console; full value only if ``EPIC_PRINT_FULL_TOKENS=1``."""
    if not token:
        return f"{label}: <empty>"
    full = os.environ.get("EPIC_PRINT_FULL_TOKENS", "").strip().lower() in ("1", "true", "yes", "on")
    if full:
        return f"{label}: {token}"
    return f"{label}: len={len(token)}  {token}"


def debug_print_epic_device_auth_tokens(
    client_token: str,
    ios_exchange_token: str,
    device_code_flow_token: str,
    token_used_for_get: str,
    account_id: str,
) -> None:
    """Debug: client token, iOS EpicUser token, device_code token, and which one is used for GET deviceAuth."""
    print("[Epic OAuth] " + _debug_format_token_line("FIRST (client / get_access_token)", client_token))
    print("[Epic OAuth] " + _debug_format_token_line("SECOND (iOS exchange → EpicUser.access_token)", ios_exchange_token))
    print("[Epic OAuth] " + _debug_format_token_line("THIRD (wait_device_code_token → prod-fn)", device_code_flow_token))
    print(
        "[Epic OAuth] deviceAuth GET uses: "
        + ("THIRD (device_code)" if token_used_for_get and token_used_for_get == device_code_flow_token else "fallback")
        + f" · account_id={account_id}"
    )
    logging.info(
        "%s | %s | %s | account_id=%s",
        _debug_format_token_line("FIRST", client_token),
        _debug_format_token_line("SECOND", ios_exchange_token),
        _debug_format_token_line("THIRD", device_code_flow_token),
        account_id,
    )


# these tokens are used to authorize in epic games's API and let us do the skincheck without getting errors
EPIC_API_SWITCH_TOKEN = "OThmN2U0MmMyZTNhNGY4NmE3NGViNDNmYmI0MWVkMzk6MGEyNDQ5YTItMDAxYS00NTFlLWFmZWMtM2U4MTI5MDFjNGQ3"
# keep in mind, sometimes epic games block the client ids, so then you have to generate new ios token for it to start working again
EPIC_API_IOS_CLIENT_TOKEN = "M2Y2OWU1NmM3NjQ5NDkyYzhjYzI5ZjFhZjA4YThhMTI6YjUxZWU5Y2IxMjIzNGY1MGE2OWVmYTY3ZWY1MzgxMmU="


FRIEND_CODES_BASE_URL = (
    "https://fngw-mcp-gc-livefn.ol.epicgames.com/fortnite/api/game/v2/friendcodes"
)
MOBILE_INVITE_CODE_TYPE = "CodeToken:mobileinvite"
FRIEND_CODES_HTTP_TIMEOUT = 20

DEVICE_AUTH_PUBLIC_URL = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{account_id}/deviceAuth"
)
ENTITLEMENTS_API_URL = (
    "https://entitlement-public-service-prod08.ol.epicgames.com/entitlement/api/account/{account_id}/entitlements"
)

# device_auth grant (saved credentials) → access token → exchange code → id/exchange web URL
_EPIC_DEVICE_AUTH_BASIC = (
    "basic M2Y2OWU1NmM3NjQ5NDkyYzhjYzI5ZjFhZjA4YThhMTI6"
    "YjUxZWU5Y2IxMjIzNGY1MGE2OWVmYTY3ZWY1MzgxMmU="
)
_EPIC_OAUTH_TOKEN_URL = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
)
_EPIC_OAUTH_EXCHANGE_URL = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange"
)
_EPIC_WEB_LOGIN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


async def generate_epic_web_login_url_from_device_auth(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    """
    Same flow as desktop ``device_auth.json``: OAuth token (grant_type=device_auth) →
    exchange code → ``https://www.epicgames.com/id/exchange?...`` for browser login.

    ``device_auth`` may use our save shape (``device_id``, ``account_id``) or Epic camelCase.
    """
    acc = str(
        device_auth.get("account_id") or device_auth.get("accountId") or ""
    ).strip()
    did = str(
        device_auth.get("device_id") or device_auth.get("deviceId") or ""
    ).strip()
    sec = str(device_auth.get("secret") or "").strip()
    if not acc or not did or not sec:
        return None

    form = {
        "grant_type": "device_auth",
        "account_id": acc,
        "device_id": did,
        "secret": sec,
        "token_type": "bearer",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _EPIC_DEVICE_AUTH_BASIC,
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL,
            data=form,
            headers=headers,
        ) as token_resp:
            t = await token_resp.text()
            epic_log_json_or_text_response(
                "generate_web_login_oauth_token",
                "POST",
                _EPIC_OAUTH_TOKEN_URL,
                token_resp.status,
                t,
            )
            if token_resp.status != 200:
                logging.warning(
                    "device_auth oauth token failed: %s %s",
                    token_resp.status,
                    t[:800],
                )
                return None
            try:
                body = json.loads(t)
            except json.JSONDecodeError:
                return None
            access_token = body.get("access_token")
            if not access_token:
                return None

        async with session.get(
            _EPIC_OAUTH_EXCHANGE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": _EPIC_WEB_LOGIN_UA,
            },
        ) as ex_resp:
            t2 = await ex_resp.text()
            epic_log_json_or_text_response(
                "generate_web_login_oauth_exchange",
                "GET",
                _EPIC_OAUTH_EXCHANGE_URL,
                ex_resp.status,
                t2,
            )
            if ex_resp.status != 200:
                logging.warning(
                    "oauth exchange failed: %s %s",
                    ex_resp.status,
                    t2[:800],
                )
                return None
            try:
                ex_data = json.loads(t2)
            except json.JSONDecodeError:
                return None
            exchange_code = ex_data.get("code")
            if not exchange_code:
                return None

    ec = quote(str(exchange_code), safe="")
    return (
        f"https://www.epicgames.com/id/exchange?exchangeCode={ec}"
        "&redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Faccount%2Fpersonal%3Fmode%3Dgame"
        "&prompt=none"
    )


def generate_epic_web_login_url_from_device_auth_sync(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    """Sync wrapper for Telegram handlers (no running asyncio loop)."""
    return asyncio.run(generate_epic_web_login_url_from_device_auth(device_auth))


# Launcher client (eg1) — same as ``xtc`` / ``AccountActions._get_launcher_exchange_code_async``
_EPIC_LAUNCHER_CLIENT_BASIC = (
    "basic MzRhMDJjZjhmNDQxNGUyOWIxNTkyMTg3NmRhMzZmOWE6"
    "ZGFhZmJjY2M3Mzc3NDUwMzlkZmZlNTNkOTRmYzc2Y2Y="
)


async def get_launcher_client_credentials_token() -> Optional[str]:
    """Get client_credentials access token for Epic Launcher client."""
    form = {
        "grant_type": "client_credentials",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _EPIC_LAUNCHER_CLIENT_BASIC,
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL, data=form, headers=headers
        ) as resp:
            t = await resp.text()
            epic_log_json_or_text_response(
                "launcher_client_credentials",
                "POST",
                _EPIC_OAUTH_TOKEN_URL,
                resp.status,
                t,
            )
            if resp.status != 200:
                logging.warning(
                    "launcher client_credentials: %s %s",
                    resp.status,
                    t[:800],
                )
                return None
            try:
                data = json.loads(t)
            except json.JSONDecodeError:
                return None
            return data.get("access_token")


async def handle_corrective_action(continuation: str, corrective_action: str, client_token: str) -> bool:
    """Handle corrective action using continuation and client token. Returns True if handled."""
    base_url = "https://account-public-service-prod.ol.epicgames.com/account/api/public/corrections"
    headers = {
        "Authorization": f"Bearer {client_token}",
        "Content-Type": "application/json",
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if corrective_action == "CONFIRM_DISPLAY_NAME":
            url = f"{base_url}/confirmDisplayName"
            payload = {"continuation": continuation}
        elif corrective_action == "EULA_ACCEPTANCE":
            url = f"{base_url}/acceptEula"
            payload = {"continuation": continuation}
        elif corrective_action == "PRIVACY_POLICY_ACCEPTANCE":
            url = f"{base_url}/acceptPrivacyPolicy"
            payload = {"continuation": continuation}
        elif corrective_action == "PROMOTE_ACCOUNT":
            url = f"{base_url}/promoteAccount"
            payload = {"continuation": continuation}
        elif corrective_action == "PENDING_DELETION":
            url = f"{base_url}/cancelPendingDeletion"
            payload = {"continuation": continuation}
        elif corrective_action == "DATE_OF_BIRTH":
            # For DATE_OF_BIRTH, we need dateOfBirth, but we don't have it here. Skip for now.
            logging.warning("Corrective action DATE_OF_BIRTH requires dateOfBirth, skipping.")
            return False
        elif corrective_action == "GUARDIAN_EMAIL":
            # Requires guardianEmail, skip.
            logging.warning("Corrective action GUARDIAN_EMAIL requires guardianEmail, skipping.")
            return False
        else:
            logging.warning("Unknown corrective action: %s", corrective_action)
            return False

        async with session.put(url, json=payload, headers=headers) as resp:
            t = await resp.text()
            epic_log_json_or_text_response(
                f"corrective_{corrective_action}", "PUT", url, resp.status, t
            )
            if resp.status == 204:
                logging.info("Corrective action %s handled successfully.", corrective_action)
                return True
            else:
                logging.warning("Failed to handle corrective action %s: %s %s", corrective_action, resp.status, t[:800])
                return False


async def get_fortnite_launcher_exchange_code_from_device_auth(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    """
    device_auth (eg1) → account exchange → launcher token (launcher basic) → **launcher** exchange code.

    Used to run ``FortniteLauncher.exe`` with ``-AUTH_TYPE=exchangecode`` (see xtc ``launch_game``).
    """
    acc = str(
        device_auth.get("account_id") or device_auth.get("accountId") or ""
    ).strip()
    did = str(
        device_auth.get("device_id") or device_auth.get("deviceId") or ""
    ).strip()
    sec = str(device_auth.get("secret") or "").strip()
    if not acc or not did or not sec:
        return None

    form_device = {
        "grant_type": "device_auth",
        "account_id": acc,
        "device_id": did,
        "secret": sec,
        "token_type": "eg1",
    }
    h_json = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _EPIC_DEVICE_AUTH_BASIC,
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL,
            data=form_device,
            headers=h_json,
        ) as token_resp:
            if token_resp.status != 200:
                t = await token_resp.text()
                logging.warning(
                    "launcher chain: device token %s %s",
                    token_resp.status,
                    t[:800],
                )
                return None
            td = await token_resp.json()
            account_access = td.get("access_token")
            if not account_access:
                return None

        async with session.get(
            _EPIC_OAUTH_EXCHANGE_URL,
            headers={
                "Authorization": f"Bearer {account_access}",
                "User-Agent": _EPIC_WEB_LOGIN_UA,
            },
        ) as ex1:
            if ex1.status != 200:
                t = await ex1.text()
                logging.warning(
                    "launcher chain: account exchange %s %s",
                    ex1.status,
                    t[:800],
                )
                return None
            exd = await ex1.json()
            account_exchange_code = exd.get("code")
            if not account_exchange_code:
                return None

        form_launcher = {
            "grant_type": "exchange_code",
            "exchange_code": account_exchange_code,
            "token_type": "eg1",
        }
        h_launch = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _EPIC_LAUNCHER_CLIENT_BASIC,
            "User-Agent": _EPIC_WEB_LOGIN_UA,
        }
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL,
            data=form_launcher,
            headers=h_launch,
        ) as lr:
            if lr.status != 200:
                t = await lr.text()
                logging.warning(
                    "launcher chain: launcher token %s %s",
                    lr.status,
                    t[:800],
                )
                return None
            ld = await lr.json()
            launcher_access = ld.get("access_token")
            if not launcher_access:
                return None

        async with session.get(
            _EPIC_OAUTH_EXCHANGE_URL,
            headers={
                "Authorization": f"Bearer {launcher_access}",
                "User-Agent": _EPIC_WEB_LOGIN_UA,
            },
        ) as ex2:
            if ex2.status != 200:
                t = await ex2.text()
                logging.warning(
                    "launcher chain: launcher exchange %s %s",
                    ex2.status,
                    t[:800],
                )
                return None
            final = await ex2.json()
            return final.get("code")


def get_fortnite_launcher_exchange_code_from_device_auth_sync(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    return asyncio.run(get_fortnite_launcher_exchange_code_from_device_auth(device_auth))


async def get_fortnite_launcher_access_token_from_device_auth(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    """
    device_auth → account access token → exchange code → launcher access token.

    Used for fetching device auth list in recheck.
    """
    acc = str(
        device_auth.get("account_id") or device_auth.get("accountId") or ""
    ).strip()
    did = str(
        device_auth.get("device_id") or device_auth.get("deviceId") or ""
    ).strip()
    sec = str(device_auth.get("secret") or "").strip()
    if not acc or not did or not sec:
        return None

    form_device = {
        "grant_type": "device_auth",
        "account_id": acc,
        "device_id": did,
        "secret": sec,
        "token_type": "eg1",
    }
    h_json = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _EPIC_DEVICE_AUTH_BASIC,
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL,
            data=form_device,
            headers=h_json,
        ) as token_resp:
            if token_resp.status != 200:
                t = await token_resp.text()
                logging.warning(
                    "launcher access chain: device token %s %s",
                    token_resp.status,
                    t[:800],
                )
                return None
            td = await token_resp.json()
            account_access = td.get("access_token")
            if not account_access:
                return None

        async with session.get(
            _EPIC_OAUTH_EXCHANGE_URL,
            headers={
                "Authorization": f"Bearer {account_access}",
                "User-Agent": _EPIC_WEB_LOGIN_UA,
            },
        ) as ex1:
            if ex1.status != 200:
                t = await ex1.text()
                logging.warning(
                    "launcher access chain: account exchange %s %s",
                    ex1.status,
                    t[:800],
                )
                return None
            exd = await ex1.json()
            account_exchange_code = exd.get("code")
            if not account_exchange_code:
                return None

        form_launcher = {
            "grant_type": "exchange_code",
            "exchange_code": account_exchange_code,
            "token_type": "eg1",
        }
        h_launch = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _EPIC_LAUNCHER_CLIENT_BASIC,
            "User-Agent": _EPIC_WEB_LOGIN_UA,
        }
        # Try to get launcher token, handling corrective actions
        max_attempts = 5
        for attempt in range(max_attempts):
            async with session.post(
                _EPIC_OAUTH_TOKEN_URL,
                data=form_launcher,
                headers=h_launch,
            ) as lr:
                if lr.status == 200:
                    ld = await lr.json()
                    launcher_access = ld.get("access_token")
                    if launcher_access:
                        return launcher_access
                    else:
                        logging.warning("launcher access chain: no access_token in response")
                        return None
                else:
                    t = await lr.text()
                    try:
                        error_data = json.loads(t)
                        if error_data.get("errorCode") == "errors.com.epicgames.oauth.corrective_action_required":
                            corrective_action = error_data.get("correctiveAction", "UNKNOWN")
                            continuation = error_data.get("continuation")
                            if continuation:
                                client_token = await get_launcher_client_credentials_token()
                                if client_token and await handle_corrective_action(continuation, corrective_action, client_token):
                                    logging.info("Corrective action handled, retrying launcher token...")
                                    continue  # Retry the post
                                else:
                                    logging.warning("Failed to handle corrective action: %s", corrective_action)
                                    return None
                            else:
                                logging.warning("No continuation in corrective action error")
                                return None
                        else:
                            logging.warning(
                                "launcher access chain: launcher token %s %s",
                                lr.status,
                                t[:800],
                            )
                            return None
                    except json.JSONDecodeError:
                        logging.warning(
                            "launcher access chain: launcher token %s %s",
                            lr.status,
                            t[:800],
                        )
                        return None
        logging.warning("Max attempts reached for launcher token")
        return None


def get_fortnite_launcher_access_token_from_device_auth_sync(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    return asyncio.run(get_fortnite_launcher_access_token_from_device_auth(device_auth))


async def device_auth_oauth_token_response(
    device_auth: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Full JSON from POST oauth/token with ``grant_type=device_auth`` (Epic desktop client)."""
    acc = str(
        device_auth.get("account_id") or device_auth.get("accountId") or ""
    ).strip()
    did = str(
        device_auth.get("device_id") or device_auth.get("deviceId") or ""
    ).strip()
    sec = str(device_auth.get("secret") or "").strip()
    if not acc or not did or not sec:
        return None
    form = {
        "grant_type": "device_auth",
        "account_id": acc,
        "device_id": did,
        "secret": sec,
        "token_type": "bearer",
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": _EPIC_DEVICE_AUTH_BASIC,
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _EPIC_OAUTH_TOKEN_URL, data=form, headers=headers
        ) as token_resp:
            t = await token_resp.text()
            epic_log_json_or_text_response(
                "device_auth_oauth_token",
                "POST",
                _EPIC_OAUTH_TOKEN_URL,
                token_resp.status,
                t,
            )
            if token_resp.status != 200:
                logging.warning(
                    "device_auth bearer token: %s %s",
                    token_resp.status,
                    t[:800],
                )
                return None
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return None


async def device_auth_oauth_access_token_bearer(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    """OAuth access token (``token_type=bearer``) for Epic account APIs (DELETE deviceAuth, sessions)."""
    body = await device_auth_oauth_token_response(device_auth)
    if not body:
        return None
    return (body.get("access_token") or "").strip() or None


def device_auth_oauth_access_token_bearer_sync(
    device_auth: Dict[str, Any],
) -> Optional[str]:
    return asyncio.run(device_auth_oauth_access_token_bearer(device_auth))


async def delete_device_auth_on_epic_servers(
    device_auth: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    DELETE ``/account/api/public/account/{accountId}/deviceAuth/{deviceId}``.
    Success: HTTP 204 No Content.
    """
    acc = str(
        device_auth.get("account_id") or device_auth.get("accountId") or ""
    ).strip()
    did = str(
        device_auth.get("device_id") or device_auth.get("deviceId") or ""
    ).strip()
    if not acc or not did:
        return False, "missing account_id or device_id"
    token = await device_auth_oauth_access_token_bearer(device_auth)
    if not token:
        return False, "🚫 The account is invalid. Please log in again"
    url = (
        "https://account-public-service-prod.ol.epicgames.com/"
        f"account/api/public/account/{acc}/deviceAuth/{did}"
    )
    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers=headers) as r:
            t = await r.text()
            epic_log_json_or_text_response("delete_device_auth", "DELETE", url, r.status, t)
            if r.status in (204, 200):
                return True, ""
            logging.warning("delete_device_auth: %s %s", r.status, t[:800])
            return False, f"HTTP {r.status}"


def delete_device_auth_on_epic_servers_sync(
    device_auth: Dict[str, Any],
) -> Tuple[bool, str]:
    return asyncio.run(delete_device_auth_on_epic_servers(device_auth))


async def kill_epic_oauth_sessions(
    device_auth: Dict[str, Any],
    kill_type: str = "OTHERS",
) -> Tuple[bool, str]:
    """DELETE ``/account/api/oauth/sessions/kill?killType=`` (e.g. ``OTHERS``, ``ALL``)."""
    token = await device_auth_oauth_access_token_bearer(device_auth)
    if not token:
        return False, "could not obtain access token"
    kt = (kill_type or "OTHERS").strip().upper()
    allowed = (
        "ALL",
        "OTHERS",
        "ALL_ACCOUNT_CLIENT",
        "OTHERS_ACCOUNT_CLIENT",
        "OTHERS_ACCOUNT_CLIENT_SERVICE",
    )
    if kt not in allowed:
        kt = "OTHERS"
    url = (
        "https://account-public-service-prod.ol.epicgames.com/"
        f"account/api/oauth/sessions/kill?killType={kt}"
    )
    headers = {
        "Authorization": f"bearer {token}",
        "User-Agent": _EPIC_WEB_LOGIN_UA,
    }
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.delete(url, headers=headers) as r:
            t = await r.text()
            epic_log_json_or_text_response(
                "kill_epic_oauth_sessions", "DELETE", url, r.status, t
            )
            if r.status in (204, 200):
                return True, ""
            logging.warning("kill_epic_oauth_sessions: %s %s", r.status, t[:800])
            return False, f"HTTP {r.status}"


def kill_epic_oauth_sessions_sync(
    device_auth: Dict[str, Any],
    kill_type: str = "OTHERS",
) -> Tuple[bool, str]:
    return asyncio.run(kill_epic_oauth_sessions(device_auth, kill_type))


# Epic Friends Service (social graph) — MIXEDDOCS/EpicResearch `friends/*`, fortgo `friends/*.go`
EPIC_FRIENDS_V1_BASE = (
    "https://friends-public-service-prod.ol.epicgames.com/friends/api/v1"
)

# EGS Store / parental content-controls PIN (6 digits)
EPIC_EGS_VERIFY_CONTENT_PIN_URL = (
    "https://egs-platform-service.store.epicgames.com/api/v1/private/egs/account/"
    "content-controls/verify-pin"
)


def _friends_api_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"bearer {access_token}",
        "User-Agent": "PostmanRuntime/7.53.0",
        "Content-Type": "application/json",
    }


_EPIC_ACCOUNT_PUBLIC_LOOKUP_URL = (
    "https://account-public-service-prod.ol.epicgames.com/account/api/public/account"
)


def _fetch_public_display_names_bulk_chunk_http(
    access_token: str, account_ids: List[str]
) -> Dict[str, str]:
    """
    ``GET .../account/api/public/account?accountId=...`` (repeat param, max 100 ids).
    Returns accountId (lower) → displayName.
    """
    chunk = [x for x in account_ids if (x or "").strip()][:100]
    if not chunk:
        return {}
    params = [("accountId", a.strip().lower()) for a in chunk]
    try:
        r = requests.get(
            _EPIC_ACCOUNT_PUBLIC_LOOKUP_URL,
            headers=_friends_api_headers(access_token),
            params=params,
            timeout=35,
        )
    except OSError:
        return {}
    _requests_log_response("resolve_public_accounts_bulk", "GET", _EPIC_ACCOUNT_PUBLIC_LOOKUP_URL, r)
    if r.status_code != 200:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    if not isinstance(data, list):
        return {}
    out: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        iid = (item.get("id") or item.get("accountId") or "").strip().lower()
        dn = (item.get("displayName") or item.get("display_name") or "").strip()
        if iid and dn:
            out[iid] = dn
    return out


def resolve_epic_public_display_names_batch_sync(
    access_token: str,
    account_ids: List[str],
    *,
    max_workers: int = 8,
) -> Dict[str, str]:
    """
    Map accountId → displayName: memory+disk cache first, then bulk Epic lookup (100 ids/request).
    Persists new names to ``cache/epic_public_display_names.json`` (one write per batch).
    """
    uniq: List[str] = []
    seen: set[str] = set()
    for x in account_ids:
        a = (x or "").strip().lower()
        if _EPIC_ACCOUNT_ID_HEX_RE.fullmatch(a) and a not in seen:
            seen.add(a)
            uniq.append(a)
    if not uniq:
        return {}

    cache = _get_epic_display_name_cache_mut()
    out: Dict[str, str] = {}
    to_fetch: List[str] = []
    with _EPIC_DN_CACHE_LOCK:
        for a in uniq:
            dn = cache.get(a) or ""
            if dn:
                out[a] = dn
            else:
                to_fetch.append(a)

    if not to_fetch:
        return out

    chunks: List[List[str]] = [
        to_fetch[i : i + 100] for i in range(0, len(to_fetch), 100)
    ]
    workers = min(max_workers, max(1, len(chunks)))

    merged: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fut_map = {
            ex.submit(_fetch_public_display_names_bulk_chunk_http, access_token, ch): ch
            for ch in chunks
        }
        for fut in as_completed(fut_map):
            try:
                part = fut.result()
            except Exception:
                part = {}
            merged.update(part)
    for aid, dn in merged.items():
        if dn:
            out[aid] = dn

    if merged:
        c = _get_epic_display_name_cache_mut()
        with _EPIC_DN_CACHE_LOCK:
            c.update(merged)
            snap = dict(c)
        _save_epic_display_name_cache_to_disk(snap)

    return out


def resolve_epic_public_display_name_sync(access_token: str, account_id: str) -> str:
    """
    Cached :func:`resolve_epic_public_display_names_batch_sync` for one id
    (hits disk cache without starting a thread pool).
    """
    aid = (account_id or "").strip().lower()
    if not _EPIC_ACCOUNT_ID_HEX_RE.fullmatch(aid):
        return ""
    cache = _get_epic_display_name_cache_mut()
    with _EPIC_DN_CACHE_LOCK:
        hit = cache.get(aid) or ""
    if hit:
        return hit
    m = resolve_epic_public_display_names_batch_sync(access_token, [aid], max_workers=1)
    return m.get(aid, "")


def resolve_epic_account_id_from_text_sync(
    access_token: str, raw: str
) -> Optional[str]:
    """
    Resolve a 32-char Epic account ID from either a raw ID or a display name
    (requires bearer — ``GET .../account/displayName/{name}``).
    """
    s = (raw or "").strip()
    if not s:
        return None
    if _EPIC_ACCOUNT_ID_HEX_RE.fullmatch(s):
        return s.lower()
    enc = quote(s, safe="")
    url = (
        "https://account-public-service-prod.ol.epicgames.com/"
        f"account/api/public/account/displayName/{enc}"
    )
    try:
        r = requests.get(url, headers=_friends_api_headers(access_token), timeout=20)
    except OSError:
        return None
    _requests_log_response("resolve_display_name", "GET", url, r)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    acc = (data.get("id") or data.get("accountId") or "").strip()
    return acc.lower() or None


def _friends_unwrap(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body:
        return body.get("data")
    return body


def _format_epic_json_error(err: Any) -> str:
    """Short message from Epic ``errorMessage`` when present."""
    if isinstance(err, dict):
        em = (err.get("errorMessage") or err.get("message") or "").strip()
        if em:
            return em
    if isinstance(err, str):
        return err[:1500]
    return str(err)[:1500]


def verify_epic_content_control_pin_sync(
    access_token: str, pin: str, *, impersonate: str = "chrome120"
) -> Tuple[bool, str]:
    """
    POST EGS ``content-controls/verify-pin`` with ``{"pin": "XXXXXX"}`` — PIN must be **6 digits**.

    Uses **curl_cffi** (browser TLS fingerprint) — plain ``requests`` often gets Cloudflare HTML
    on ``*.store.epicgames.com``.
    """
    p = (pin or "").strip()
    if not p.isdigit() or len(p) != 6:
        return False, "PIN must be exactly 6 digits."
    if not (access_token or "").strip():
        return False, "missing access token"
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return False, "Install curl-cffi: pip install curl-cffi (required for store.epicgames.com APIs)."

    headers = {
        "Authorization": f"bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": _EPIC_WEB_LOGIN_UA,
        "Origin": "https://store.epicgames.com",
        "Referer": "https://store.epicgames.com/",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = curl_requests.post(
            EPIC_EGS_VERIFY_CONTENT_PIN_URL,
            headers=headers,
            json={"pin": p},
            timeout=35,
            impersonate=impersonate,
        )
    except OSError as e:
        return False, str(e)
    try:
        body = r.json()
    except ValueError:
        body = {"_non_json_body": (r.text or "")[:500_000]}
    _epic_log_http_response(
        "verify_content_pin", "POST", EPIC_EGS_VERIFY_CONTENT_PIN_URL, r.status_code, body
    )
    if 200 <= r.status_code < 300:
        return True, ""
    snippet = (r.text or "")[:400].lstrip()
    if snippet.startswith("<!") or "<html" in snippet.lower():
        return (
            False,
            "Server returned HTML (Cloudflare/WAF). "
            "Try again with curl_cffi; if it persists, change impersonate (e.g. chrome110) or network.",
        )
    try:
        err = r.json()
    except ValueError:
        err = r.text[:500]
    return False, _format_epic_json_error(err)


def _format_epic_friends_error(err: Any) -> str:
    """Readable plain text for Epic Friends JSON errors (no HTML in our lines)."""
    if isinstance(err, dict) and (err.get("errorCode") or err.get("errorMessage")):
        code = str(err.get("errorCode") or "")
        em = (err.get("errorMessage") or "").strip()
        em_l = em.lower()
        if code == "errors.com.epicgames.friends.cannot_friend_due_to_target_settings":
            return (
                "That player’s Epic privacy blocks this invite "
                "(they only allow certain invite types). "
                "They must change Epic → Settings → Privacy, or add you from their side."
            )
        pin_missing = code == "errors.com.epicgames.friends.cannot_friend_due_to_missing_pin"
        if not pin_missing and em and "friends" in code and "epicgames" in code:
            pin_missing = (
                "parental" in em_l
                and "pin" in em_l
                and ("missing" in em_l or "required" in em_l)
            )
        if pin_missing:
            return (
                "Epic requires a parental PIN for this action. "
                "In this bot: Saved Accounts → Parental controls → PIN, and send your 6 digits "
                "(verified once with Epic). Used for Add Friend, Accept Friends, and similar."
            )
        if "friendship" in em_l and "does not exist" in em_l:
            return (
                "There is no friend relationship with that account right now "
                "(not in your friends list, or the pending invite is already gone). "
                "For a pending outgoing invite, use Manage Friends → Cancel outgoing."
            )
        if em:
            return em
    if isinstance(err, str):
        return err[:1500]
    return str(err)[:1500]


def friends_get_summary_sync(
    access_token: str, account_id: str
) -> Optional[Dict[str, Any]]:
    """GET ``/friends/api/v1/{accountId}/summary`` with ``displayNames=true`` (names on each row when Epic allows)."""
    if not access_token or not (account_id or "").strip():
        return None
    aid = account_id.strip()
    url = f"{EPIC_FRIENDS_V1_BASE}/{aid}/summary"
    try:
        r = requests.get(
            url,
            headers=_friends_api_headers(access_token),
            params={"displayNames": "true"},
            timeout=25,
        )
    except OSError:
        return None
    _requests_log_response("friends_get_summary", "GET", url, r)
    if r.status_code != 200:
        return None
    try:
        raw = r.json()
    except ValueError:
        return None
    out = _friends_unwrap(raw)
    return out if isinstance(out, dict) else None


def _friend_summary_row_account_and_display(row: Any) -> Tuple[str, str]:
    """Epic rows use ``accountId`` + optional ``displayName`` when ``displayNames=true``."""
    if not isinstance(row, dict):
        return "", ""
    aid = (row.get("accountId") or row.get("account_id") or "").strip()
    dn = (row.get("displayName") or row.get("display_name") or "").strip()
    if not dn:
        prof = row.get("profile")
        if isinstance(prof, dict):
            dn = (prof.get("displayName") or prof.get("display_name") or "").strip()
    if aid:
        return aid.lower(), dn
    return "", ""


def friends_list_entries_from_summary(
    summary: Optional[Dict[str, Any]], list_key: str
) -> List[Tuple[str, str]]:
    """
    ``summary`` keys: ``friends``, ``incoming``, ``outgoing``.

    Returns ``(account_id, display_name)``; ``display_name`` may be empty if Epic omitted it.
    """
    if not summary or not isinstance(summary, dict):
        return []
    rows = summary.get(list_key) or []
    if not isinstance(rows, list):
        return []
    out: List[Tuple[str, str]] = []
    for row in rows:
        aid, dn = _friend_summary_row_account_and_display(row)
        if aid:
            out.append((aid, dn))
    return out


def friends_list_account_ids_from_summary(
    summary: Optional[Dict[str, Any]], list_key: str
) -> List[str]:
    """``summary`` keys: ``friends``, ``incoming``, ``outgoing`` — each row has ``accountId``."""
    return [a for a, _ in friends_list_entries_from_summary(summary, list_key)]


def friends_incoming_account_ids(summary: Optional[Dict[str, Any]]) -> List[str]:
    return friends_list_account_ids_from_summary(summary, "incoming")


def friends_outgoing_account_ids(summary: Optional[Dict[str, Any]]) -> List[str]:
    return friends_list_account_ids_from_summary(summary, "outgoing")


def friends_confirmed_friends_account_ids(summary: Optional[Dict[str, Any]]) -> List[str]:
    return friends_list_account_ids_from_summary(summary, "friends")


def friends_accept_incoming_bulk_sync(
    access_token: str,
    account_id: str,
    target_ids: List[str],
    *,
    parental_pin: str = "",
) -> Tuple[bool, str]:
    """POST ``/incoming/accept?targetIds=`` — Epic also expects JSON ``{"pin": "..."}`` (same as add-friend)."""
    if not access_token or not (account_id or "").strip():
        return False, "missing token or account"
    ids = [x.strip().lower() for x in target_ids if (x or "").strip()]
    if not ids:
        return True, "no incoming IDs"
    aid = account_id.strip()
    query = ",".join(ids)
    url = f"{EPIC_FRIENDS_V1_BASE}/{aid}/incoming/accept?targetIds={query}"
    pin = (parental_pin or "").strip()
    payload = {"pin": pin}
    try:
        r = requests.post(
            url,
            headers=_friends_api_headers(access_token),
            json=payload,
            timeout=35,
        )
    except OSError as e:
        return False, str(e)
    _requests_log_response("friends_accept_incoming", "POST", url, r)
    if r.status_code in (200, 204):
        return True, ""
    try:
        err = r.json()
    except ValueError:
        err = r.text[:500]
    return False, _format_epic_friends_error(err)


def friends_reject_incoming_sync(
    access_token: str, account_id: str, from_account_id: str
) -> bool:
    """Decline one incoming request: try ``DELETE .../incoming/{id}`` then ``DELETE .../friends/{id}``."""
    if not access_token or not account_id or not from_account_id:
        return False
    aid = account_id.strip()
    fid = from_account_id.strip().lower()
    for path in (f"{EPIC_FRIENDS_V1_BASE}/{aid}/incoming/{fid}", f"{EPIC_FRIENDS_V1_BASE}/{aid}/friends/{fid}"):
        try:
            r = requests.delete(path, headers=_friends_api_headers(access_token), timeout=25)
        except OSError:
            continue
        _requests_log_response("friends_reject_incoming", "DELETE", path, r)
        if r.status_code in (200, 204):
            return True
    return False


def friends_add_sync(
    access_token: str,
    account_id: str,
    target_account_id: str,
    *,
    parental_pin: str = "",
) -> Tuple[bool, str]:
    """POST ``/friends/api/v1/{accountId}/friends/{targetId}`` — body must include ``pin`` (fortgo)."""
    if not access_token or not account_id or not target_account_id:
        return False, "missing parameters"
    aid = account_id.strip()
    tid = target_account_id.strip().lower()
    url = f"{EPIC_FRIENDS_V1_BASE}/{aid}/friends/{tid}"
    pin = (parental_pin or "").strip()
    payload = {"pin": pin}
    try:
        r = requests.post(
            url,
            headers=_friends_api_headers(access_token),
            json=payload,
            timeout=25,
        )
    except OSError as e:
        return False, str(e)
    _requests_log_response("friends_add", "POST", url, r)
    if r.status_code in (200, 204):
        return True, ""
    try:
        err = r.json()
    except ValueError:
        err = r.text[:500]
    return False, _format_epic_friends_error(err)


def friends_remove_sync(
    access_token: str, account_id: str, target_account_id: str
) -> Tuple[bool, str]:
    """DELETE ``/friends/api/v1/{accountId}/friends/{targetId}``."""
    if not access_token or not account_id or not target_account_id:
        return False, "missing parameters"
    aid = account_id.strip()
    tid = target_account_id.strip().lower()
    url = f"{EPIC_FRIENDS_V1_BASE}/{aid}/friends/{tid}"
    try:
        r = requests.delete(url, headers=_friends_api_headers(access_token), timeout=25)
    except OSError as e:
        return False, str(e)
    _requests_log_response("friends_remove", "DELETE", url, r)
    if r.status_code in (200, 204):
        return True, ""
    try:
        err = r.json()
    except ValueError:
        err = r.text[:500]
    return False, _format_epic_friends_error(err)


def friends_remove_all_sync(access_token: str, account_id: str) -> Tuple[bool, str]:
    """DELETE ``/friends/api/v1/{accountId}/friends`` (fortgo ``RemoveAllFriends``)."""
    if not access_token or not (account_id or "").strip():
        return False, "missing token or account"
    aid = account_id.strip()
    url = f"{EPIC_FRIENDS_V1_BASE}/{aid}/friends"
    try:
        r = requests.delete(url, headers=_friends_api_headers(access_token), timeout=40)
    except OSError as e:
        return False, str(e)
    _requests_log_response("friends_remove_all", "DELETE", url, r)
    if r.status_code in (200, 204):
        return True, ""
    try:
        err = r.json()
    except ValueError:
        err = r.text[:500]
    return False, _format_epic_friends_error(err)


def fetch_friend_codes(access_token: str, account_id: str) -> List[Dict[str, Any]]:
    """GET epic + xbox friend-code lists; excludes ``CodeToken:mobileinvite`` (same as Aerial-Launcher)."""
    if not access_token or not account_id:
        return []
    client = requests.Session()
    epic_url = f"{FRIEND_CODES_BASE_URL}/{account_id}/epic"
    xbox_url = f"{FRIEND_CODES_BASE_URL}/{account_id}/xbox"
    headers = {
        "Authorization": f"bearer {access_token}",
        "Content-Type": "application/json",
    }

    def _one(url: str) -> List[Dict[str, Any]]:
        try:
            r = client.get(url, headers=headers, timeout=FRIEND_CODES_HTTP_TIMEOUT)
        except OSError:
            return []
        _requests_log_response("fetch_friend_codes", "GET", url, r)
        if r.status_code != 200:
            return []
        try:
            arr = r.json()
        except ValueError:
            return []
        if not isinstance(arr, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in arr:
            if not isinstance(item, dict):
                continue
            ct = item.get("codeType")
            if ct == MOBILE_INVITE_CODE_TYPE:
                continue
            out.append(
                {
                    "codeId": item.get("codeId") or "",
                    "codeType": item.get("codeType") or "",
                    "dateCreated": item.get("dateCreated"),
                }
            )
        return out

    epic_part = _one(epic_url)
    xbox_part = _one(xbox_url)
    merged: List[Dict[str, Any]] = []
    merged.extend(epic_part)
    merged.extend(xbox_part)
    return merged


def _device_auth_list_bearer_token(user: "EpicUser") -> str:
    """Prefer ``wait_device_code_token`` access token; fall back to iOS ``access_token``."""
    t = (getattr(user, "device_code_flow_access_token", None) or "").strip()
    return t if t else (user.access_token or "")


def fetch_public_device_auth_sync(access_token: str, account_id: str) -> Any:
    """GET ``/account/api/public/account/{accountId}/deviceAuth`` (list saved device credentials).

    Use the **device_code** OAuth access token from ``wait_device_code_token`` (prod-fn client), not the
    iOS ``exchange_code`` token, when possible.
    """
    if not access_token or not account_id:
        return None
    url = DEVICE_AUTH_PUBLIC_URL.format(account_id=account_id)
    headers = {
        "Authorization": f"bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except OSError:
        return None
    _requests_log_response("fetch_public_device_auth", "GET", url, r)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def fetch_account_entitlements_sync(access_token: str, account_id: str) -> Any:
    """GET Epic account entitlements (``Fortnite_Founder`` etc.) — same URL as ``xboxfn.go``."""
    if not access_token or not account_id:
        return None
    url = ENTITLEMENTS_API_URL.format(account_id=account_id)
    headers = {
        "Authorization": f"bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except OSError:
        return None
    _requests_log_response("fetch_account_entitlements", "GET", url, r)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


class EpicEndpoints:
    endpoint_public_account_addresses = (
        "https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{account_id}/addresses"
    )
    endpoint_oauth_token = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
    endpoint_prod03_oauth_token = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
    endpoint_redirect_url = "https://www.epicgames.com/id/login?redirectUrl=https%3A//www.epicgames.com/id/login%3FredirectUrl%3Dhttps%253A%252F%252Fwww.epicgames.com%252Fid%252Fapi%252Fredirect%253FclientId%253Dec684b8c687f479fadea3cb2ad83f5c6%2526responseType%253Dcode"
    endpoint_oauth_exchange = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/exchange"
    endpoint_device_auth = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/deviceAuthorization"

# locker categories we render (fortnite-api batch fetch; excludes synthetic Popular / Exclusive)
locker_categories = [
    "AthenaCharacter",
    "AthenaBackpack",
    "AthenaPickaxe",
    "AthenaDance",
    "AthenaGlider",
    "AthenaItemWrap",
    "AthenaLoadingScreen",
    "AthenaMusicPack",
    "AthenaPopular",
    "AthenaExclusive",
]

class EpicUser:
    def __init__(self, data: dict = {}):
        self.raw = data

        # api response for login link generation
        self.access_token = data.get("access_token", "")
        self.expires_in = data.get("expires_in", 0)
        self.expires_at = data.get("expires_at", "")
        self.token_type = data.get("token_type", "")
        self.client_id = data.get("client_id", "")
        self.internal_client = data.get("internal_client", False)
        self.client_service = data.get("client_service", "")
        self.product_id = data.get("product_id", "")
        self.application_id = data.get("application_id", "")

        # api response for account metadata
        self.refresh_token = data.get("refresh_token", "")
        self.refresh_expires = data.get("refresh_expires", "")
        self.refresh_expires_at = data.get("refresh_expires_at", "")
        self.account_id = data.get("account_id", "")
        self.display_name = data.get("displayName", "")
        self.app = data.get("app", "")
        self.in_app_id = data.get("in_app_id", "")
        self.acr = data.get("acr", "")
        self.auth_time = data.get("auth_time", "")

        # From ``wait_device_code_token`` (grant_type=device_code, prod-fn client) — same token used for exchange GET.
        # Required for GET public account deviceAuth list (iOS exchange token differs / lacks scope).
        self.device_code_flow_access_token = data.get("device_code_flow_access_token", "")


async def epic_user_from_exchange_code(
    exchange_code: str,
) -> Optional[EpicUser]:
    """Build ``EpicUser`` from an Epic exchange code (iOS client)."""
    code = str(exchange_code or "").strip()
    if not code:
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            EpicEndpoints.endpoint_prod03_oauth_token,
            headers={
                "Authorization": f"basic {EPIC_API_IOS_CLIENT_TOKEN}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _EPIC_WEB_LOGIN_UA,
            },
            data={
                "grant_type": "exchange_code",
                "exchange_code": code,
            },
        ) as resp:
            if resp.status != 200:
                t = await resp.text()
                logging.warning("exchange_code oauth failed: %s %s", resp.status, t[:800])
                return None
            body = await resp.json()
            at = str(body.get("access_token") or "").strip()
            if at:
                # Default fallback: at least have *some* token for deviceAuth GET.
                body["device_code_flow_access_token"] = at

                # Recheck-special: mint launcher (eg1) token via exchange_code chain.
                try:
                    async with session.get(
                        _EPIC_OAUTH_EXCHANGE_URL,
                        headers={
                            "Authorization": f"Bearer {at}",
                            "User-Agent": _EPIC_WEB_LOGIN_UA,
                        },
                    ) as ex:
                        if ex.status == 200:
                            exd = await ex.json()
                            ec = exd.get("code")
                        else:
                            ec = None
                    if ec:
                        async with session.post(
                            _EPIC_OAUTH_TOKEN_URL,
                            data={
                                "grant_type": "exchange_code",
                                "exchange_code": str(ec),
                                "token_type": "eg1",
                            },
                            headers={
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Authorization": _EPIC_LAUNCHER_CLIENT_BASIC,
                                "User-Agent": _EPIC_WEB_LOGIN_UA,
                            },
                        ) as lr:
                            if lr.status == 200:
                                ld = await lr.json()
                                launcher_at = (ld.get("access_token") or "").strip()
                                if launcher_at:
                                    body["device_code_flow_access_token"] = launcher_at
                except Exception as e:
                    logging.warning("exchange_code launcher chain failed: %s", e)
            return EpicUser(data=body)


def epic_user_data_from_device_auth_oauth(
    body: Dict[str, Any],
    device_auth: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Merge OAuth JSON + saved ``device_auth`` into a dict suitable for ``EpicUser``."""
    acc = (
        str(body.get("account_id") or body.get("accountId") or "").strip()
        or str(
            device_auth.get("account_id") or device_auth.get("accountId") or ""
        ).strip()
    )
    at = str(body.get("access_token") or "").strip()
    if not acc or not at:
        return None
    merged = dict(body)
    merged["account_id"] = acc
    if merged.get("displayName") is None:
        merged["displayName"] = str(merged.get("display_name") or "")
    # Same bearer as full login when only device_auth grant is available (recheck without device-code page).
    merged["device_code_flow_access_token"] = at
    return merged


async def epic_user_from_device_auth(
    device_auth: Dict[str, Any],
) -> Optional[EpicUser]:
    """
    Build ``EpicUser`` from saved device credentials (OAuth ``grant_type=device_auth``).
    Used for /login-equivalent recheck without opening the Epic activate link.
    """
    body = await device_auth_oauth_token_response(device_auth)
    if not body:
        return None
    data = epic_user_data_from_device_auth_oauth(body, device_auth)
    if not data:
        return None
    # For listing deviceAuth rows, we need a token minted via launcher client (eg1) using exchange_code.
    # This mimics the "launcher access chain" (device_auth → exchange → launcher token).
    try:
        launcher_access = await get_fortnite_launcher_access_token_from_device_auth(
            device_auth
        )
    except Exception as e:
        logging.warning("launcher access chain failed: %s", e)
        launcher_access = None
    if launcher_access:
        data["device_code_flow_access_token"] = launcher_access
    return EpicUser(data=data)


class LockerData:
    def __init__(self):
        self.cosmetic_categories = {}
        self.cosmetic_array = {}
        self.unlocked_styles = {}
        self.homebase_banners = {}
        self.last_match = ''
        self.registration_date = ''

    def to_dict(self):
        return {
            "cosmetic_categories": self.cosmetic_categories,
            "cosmetic_array": self.cosmetic_array,
            "unlocked_styles": self.unlocked_styles,
            "homebase_banners": self.homebase_banners,
        }

def add_missing_array2(arr, arr2, category, idclean):
    # i hate hardcoding stuff, so ill make helper funcs instead
    if category not in arr:
        arr[category] = []
        arr2[category] = []

    arr[category].append(idclean)

def add_missing_array(arr, arr2, category):
    # i hate hardcoding stuff, so ill make helper funcs instead
    if category not in arr:
        arr[category] = []
        arr2[category] = []

async def get_cosmetic_data(cosmetic_lowercase_id):
    try:
        cosmetic_url = f'https://fortnite-api.com/v2/cosmetics/br/search/ids?language=en{cosmetic_lowercase_id}'
        response = requests.get(cosmetic_url)

        response.raise_for_status()
        _requests_log_response("get_cosmetic_data", "GET", cosmetic_url, response)
        return response.json().get('data', [])
    
    except Exception as e:
        print(f"Error getting cosmetic info for cosmetic with ID: {cosmetic_lowercase_id}\n> Exception: {e}")
        return []


ACCOUNT_PORTAL_V2_URL = "https://accounts.epicgames.com/account/v2"
_ACCOUNT_PRELOAD_MARKER = "window.account_dataPreload"


def _account_portal_retry_settings() -> tuple[int, float]:
    try:
        n = int(os.environ.get("EPIC_ACCOUNT_PORTAL_MAX_ATTEMPTS", "5"))
    except ValueError:
        n = 5
    try:
        delay = float(os.environ.get("EPIC_ACCOUNT_PORTAL_RETRY_DELAY", "2.0"))
    except ValueError:
        delay = 2.0
    return max(1, min(n, 12)), max(0.3, delay)


def fetch_account_portal_v2_html_sync(
    access_token: str,
    *,
    timeout: float = 60.0,
    impersonate: str = "chrome120",
    max_attempts: Optional[int] = None,
    retry_delay_sec: Optional[float] = None,
) -> tuple[int, str, str]:
    """
    GET account portal HTML with Cookie EPIC_BEARER_TOKEN (TLS via curl_cffi).

    Cloudflare قد يُرجع 403 أو صفحة بدون preload — نعيد المحاولة مع تأخير بين المحاولات.
    Env: EPIC_ACCOUNT_PORTAL_MAX_ATTEMPTS (افتراضي 5), EPIC_ACCOUNT_PORTAL_RETRY_DELAY ثوانٍ (افتراضي 2).

    Returns (status, body, backend_tag). backend_tag: curl_cffi | curl_cffi_missing | curl_cffi:ExceptionName
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        session_add("account · GET /account/v2 (2FA HTML)", False, "curl_cffi not installed")
        return 0, "", "curl_cffi_missing"

    env_n, env_delay = _account_portal_retry_settings()
    if max_attempts is None:
        max_attempts = env_n
    if retry_delay_sec is None:
        retry_delay_sec = env_delay

    headers = {
        "Cookie": f"EPIC_BEARER_TOKEN={access_token}",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_status = 0
    last_text = ""
    for attempt in range(max_attempts):
        try:
            r = curl_requests.get(
                ACCOUNT_PORTAL_V2_URL,
                headers=headers,
                impersonate=impersonate,
                allow_redirects=True,
                timeout=timeout,
            )
            if _epic_save_responses_enabled():
                _epic_log_http_response(
                    f"fetch_account_portal_v2_attempt_{attempt + 1}",
                    "GET",
                    ACCOUNT_PORTAL_V2_URL,
                    r.status_code,
                    {
                        "attempt": attempt + 1,
                        "max_attempts": max_attempts,
                        "html_length": len(r.text or ""),
                        "html_snippet": (r.text or "")[:12000],
                    },
                )
            raw = r.text or ""
            stc = r.status_code
            if stc == 200 and _ACCOUNT_PRELOAD_MARKER in raw:
                session_add(
                    "account · GET /account/v2 (2FA HTML)",
                    True,
                    f"try {attempt + 1}/{max_attempts} · window.account_dataPreload",
                )
            else:
                d = f"try {attempt + 1}/{max_attempts} · HTTP {stc}"
                if stc == 200:
                    d += " · no account_dataPreload"
                if looks_like_cloudflare_html(raw):
                    d += " · Cloudflare/WAF (HTML block)"
                session_add("account · GET /account/v2 (2FA HTML)", False, d)
        except Exception as e:
            if attempt >= max_attempts - 1:
                return 0, "", f"curl_cffi:{type(e).__name__}"
            time.sleep(retry_delay_sec * (1 + attempt * 0.2))
            continue

        last_status = r.status_code
        last_text = r.text
        if r.status_code == 200 and _ACCOUNT_PRELOAD_MARKER in r.text:
            return r.status_code, r.text, "curl_cffi"
        if attempt < max_attempts - 1:
            time.sleep(retry_delay_sec * (1 + attempt * 0.15))

    d = f"exhausted tries · last HTTP {last_status} · {len(last_text or '')} bytes"
    if (last_text or "") and looks_like_cloudflare_html(last_text):
        d += " · Cloudflare/WAF in last body"
    elif last_status == 200 and _ACCOUNT_PRELOAD_MARKER not in (last_text or ""):
        d += " · 200 but no preload"
    session_add("account · GET /account/v2 (2FA · final)", False, d)
    return last_status, last_text, "curl_cffi"


class EpicGenerator:
    def __init__(self) -> None:
        # init the generator
        self.http: aiohttp.ClientSession
        self.user_agent = f"MilashkaSkinChecker/BLTNM/1.0 (https://t.me/MilashkaSkinChecker_bot)"
        self.access_token = ""

    async def start(self) -> None:
        self.http = aiohttp.ClientSession(headers={"User-Agent": self.user_agent})
        # First OAuth token: client_credentials via EPIC_API_SWITCH_TOKEN (see get_access_token). Used for device-code bootstrap.
        # GET …/deviceAuth list uses ``EpicUser.device_code_flow_access_token`` from ``wait_device_code_token``.
        self.access_token = await self.get_access_token()

    async def kill(self) -> None:
        await self.http.close()

    async def _aiohttp_json(self, response: aiohttp.ClientResponse, tag: str, method: str, url: str):
        status = response.status
        text = await response.text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            _epic_log_http_response(tag, method, url, status, {"_non_json_body": text[:500_000]})
            raise
        _epic_log_http_response(tag, method, url, status, data)
        return data
    
    async def get_access_token(self) -> str:
        # getting the access token from epic's api(REQUIRES usage of EPIC_API_SWITCH_TOKEN as Authorization in headers for it to work)
        # if it's not getting any data, it means the EPIC_API_SWITCH_TOKEN is expired, you must find new one :D
        async with self.http.request(
            method="POST",
            url=EpicEndpoints.endpoint_oauth_token,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"basic {EPIC_API_SWITCH_TOKEN}"
            },
            data={ "grant_type": "client_credentials" },
        ) as response:
            data = await self._aiohttp_json(response, "get_access_token", "POST", EpicEndpoints.endpoint_oauth_token)
            return data["access_token"]
        
    async def create_device_code(self) -> tuple:
        # devide code is used on the link the checker bot sends u "active?userCode=SOMETHING" something like this
        # REQUIRES usage of self.access_token, which we got from get_access_token function, as Authorization in headers
        # returns the device code, used in the link we generate for the user to login
        async with self.http.request(
            method="POST",
            url=EpicEndpoints.endpoint_device_auth,
            headers={
                "Authorization": f"bearer {self.access_token}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        ) as response:
            return await self._aiohttp_json(response, "create_device_code", "POST", EpicEndpoints.endpoint_device_auth)
        
    async def create_exchange_code(self, user: EpicUser) -> str:
        # creates exchange code for the api requests & returns it
        # REQUIRES usage of user.access_token, which we got from get_access_token function, as Authorization in headers
        async with self.http.request(
            method="GET",
            url=EpicEndpoints.endpoint_oauth_exchange,
            headers={"Authorization": f"bearer {user.access_token}"},
        ) as response:
            data = await self._aiohttp_json(response, "create_exchange_code", "GET", EpicEndpoints.endpoint_oauth_exchange)
            return data["code"]
        
    async def wait_for_device_code_completion(self, bot, message, code: str) -> Optional[EpicUser]:
        while True:
            try:
                async with self.http.request(
                    method="POST",
                    url=EpicEndpoints.endpoint_prod03_oauth_token,
                    headers={
                        "Authorization": f"basic {EPIC_API_SWITCH_TOKEN}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={"grant_type": "device_code", "device_code": code},
                    timeout=10
                ) as request:
                    token_data = await self._aiohttp_json(
                        request, "wait_device_code_token", "POST", EpicEndpoints.endpoint_prod03_oauth_token
                    )

                    if request.status == 200 and "access_token" in token_data:
                        break

                    # Handle specific API errors
                    error_code = token_data.get("errorCode")
                    if error_code == "errors.com.epicgames.account.oauth.authorization_pending":
                        pass
                    
                    elif error_code == "g":
                        pass
                    
                    elif error_code == "errors.com.epicgames.not_found":
                        bot.send_message(message.chat.id, f'❌ Login link expired, please use /login again.')
                        return None
                    else:
                        bot.send_message(message.chat.id, f'❌ Error occurred: {token_data.get("errorMessage", "Unknown error")}')
                        return None

                await asyncio.sleep(10)
            except ValueError as ve:
                print(f"Error with waiting for device code: {ve}")
                bot.send_message(message.chat.id, f'❌ An unexpected error occurred, please try again later.')
                return None
            except Exception as e:
                print(f"Unhandled exception: {e}")
                bot.send_message(message.chat.id, f'❌ An error occurred, please contact support.')
                return None

        try:
            async with self.http.request(
                method="GET",
                url=EpicEndpoints.endpoint_oauth_exchange,
                headers={"Authorization": f"bearer {token_data['access_token']}"},
            ) as request:
                exchange_data = await self._aiohttp_json(
                    request, "wait_device_code_exchange", "GET", EpicEndpoints.endpoint_oauth_exchange
                )
                if request.status != 200:
                    bot.send_message(message.chat.id, f'❌ Failed to retrieve exchange code. Please try again.')
                    return None

            async with self.http.request(
                method="POST",
                url=EpicEndpoints.endpoint_prod03_oauth_token,
                headers={
                    "Authorization": f"basic {EPIC_API_IOS_CLIENT_TOKEN}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={
                    "grant_type": "exchange_code",
                    "exchange_code": exchange_data["code"]
                },
            ) as request:
                auth_data = await self._aiohttp_json(
                    request, "wait_device_code_ios_exchange", "POST", EpicEndpoints.endpoint_prod03_oauth_token
                )
                if request.status != 200:
                    bot.send_message(message.chat.id, f'❌ Failed to authenticate using exchange code. Please try again.')
                    return None

            merged = dict(auth_data)
            merged["device_code_flow_access_token"] = token_data["access_token"]
            return EpicUser(data=merged)

        except KeyError as ke:
            bot.send_message(message.chat.id, f'❌ Unexpected response format from server.')
            return None
        except Exception as e:
            print(f"Unhandled exception during token exchange: {e}")
            bot.send_message(message.chat.id, f'❌ An error occurred while completing authentication.')
            return None
        
    async def create_device_auths(self, user: EpicUser) -> dict:
        # creates device auth
        # REQUIRES usage of user.access_token as Authorization in headers
        async with self.http.request(
            method="POST",
            url=f"https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{user.account_id}/deviceAuth",
            headers={
                "Authorization": f"bearer {user.access_token}",
                "Content-Type": "application/json",
            },
        ) as request:
            data = await self._aiohttp_json(
                request,
                "create_device_auths",
                "POST",
                f"https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{user.account_id}/deviceAuth",
            )

        return {
            "device_id": data["deviceId"],
            "account_id": data["accountId"],
            "secret": data["secret"],
            "user_agent": data["userAgent"],
            "created": {
                "location": data["created"]["location"],
                "ip_address": data["created"]["ipAddress"],
                "datetime": data["created"]["dateTime"],
            },
        }
    
    async def get_account_metadata(self, user: EpicUser) -> json:
        # grabs account's metadata(basic information) from the api
        # REQUIRES usage of user.access_token as Authorization in headers

        url = f'https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/displayName/{user.display_name}'
        headers = {
            "Authorization": f"bearer {user.access_token}",
            "Content-Type": "application/json",
        }

        async with self.http.get(url, headers=headers) as response:
            metadata = await self._aiohttp_json(response, "get_account_metadata", "GET", url)

        return metadata

    async def get_external_connections(self, user: EpicUser) -> dict:
        # returns external connected accounts info
        # REQUIRES usage of user.access_token as Authorization in headers
        ext_url = f"https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/{user.account_id}/externalAuths"
        async with self.http.request(
            method="GET",
            url=ext_url,
            headers={"Authorization": f"bearer {user.access_token}"}
        ) as resp:
            text = await resp.text()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"_non_json_body": text[:500_000]}
            _epic_log_http_response("get_external_connections", "GET", ext_url, resp.status, parsed)
            if resp.status != 200:
                return []
            return parsed

    async def get_account_addresses(self, user: EpicUser) -> List[Dict[str, Any]]:
        """Saved shipping/billing-style addresses from the public account API."""
        url = EpicEndpoints.endpoint_public_account_addresses.format(account_id=user.account_id)
        headers = {"Authorization": f"bearer {user.access_token}"}
        async with self.http.get(url, headers=headers) as resp:
            text = await resp.text()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"_non_json_body": text[:500_000]}
            _epic_log_http_response("get_account_addresses", "GET", url, resp.status, parsed)
            if resp.status != 200 or not isinstance(parsed, list):
                return []
            return parsed

    async def fetch_order_history_raw(self, user: EpicUser) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(fetch_order_history_sync, user.access_token)

    async def fetch_order_history_raw_with_retry(
        self,
        user: EpicUser,
        *,
        attempts: int = 3,
        delay_sec: float = 1.5,
    ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(
            fetch_order_history_sync_with_retry,
            user.access_token,
            attempts=attempts,
            delay_sec=delay_sec,
        )

    async def fetch_pci_payment_methods_raw_with_retry(
        self,
        user: EpicUser,
        *,
        attempts: int = 3,
        delay_sec: float = 1.5,
    ) -> Dict[str, Any]:
        """XSRF + purchaseToken + PCI /v2/purchase/payment-methods (separate from order history)."""
        return await asyncio.to_thread(
            fetch_pci_payment_methods_via_xsrf_sync_with_retry,
            user.access_token,
            attempts=attempts,
            delay_sec=delay_sec,
        )

    async def fetch_account_portal_preload_with_retry(
        self,
        user: EpicUser,
        *,
        attempts: int = 3,
        delay_sec: float = 1.5,
    ) -> Optional[dict]:
        """Account portal HTML → 2FA preload JSON; retries when empty or on errors."""
        for i in range(attempts):
            try:
                out = await self.fetch_account_portal_preload(user)
                if out is not None:
                    return out
            except Exception as e:
                logging.warning(
                    "fetch_account_portal_preload attempt %s/%s failed: %s",
                    i + 1,
                    attempts,
                    e,
                )
            if i < attempts - 1:
                await asyncio.sleep(delay_sec * (i + 1))
        return None

    async def get_public_account_info(self, user: EpicUser) -> dict:
        # returns basic public info about the account
        # REQUIRES usage of user.access_token as Authorization in headers
        url = f'https://account-public-service-prod03.ol.epicgames.com/account/api/public/account/{user.account_id}'
        headers = {
            "Authorization": f"bearer {user.access_token}"
        }

        async with self.http.get(url, headers=headers) as response:
            account_data = await self._aiohttp_json(response, "get_public_account_info", "GET", url)
    
        account_info = {} # creating json for only stuff we are interested into
        creation_date = account_data.get("created", "?")
        if creation_date != "?":
            creation_date = datetime.strptime(creation_date, "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%d/%m/%Y")

        account_info['creation_date'] = creation_date
        account_info['last_login'] = _format_last_login_for_display(account_data.get("lastLogin"))
        account_info['externalAuths'] = await self.get_external_connections(user)
        account_info['addresses'] = await self.get_account_addresses(user)
        return account_info

    async def get_account_email_info(self, user: EpicUser) -> Dict[str, Any]:
        url = f"https://account-public-service-prod.ol.epicgames.com/account/api/public/account/{user.account_id}/email"
        headers = {"Authorization": f"bearer {user.access_token}"}
        async with self.http.get(url, headers=headers) as response:
            text = await response.text()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {"_non_json_body": text[:500_000]}
            _epic_log_http_response("get_account_email_info", "GET", url, response.status, parsed)
            if response.status != 200 or not isinstance(parsed, dict):
                return empty_epic_account_email_info()
            return normalize_epic_account_email_payload(parsed)

    async def fetch_account_portal_preload(self, user: EpicUser) -> Optional[dict]:
        status, html, backend = await asyncio.to_thread(
            fetch_account_portal_v2_html_sync,
            user.access_token,
        )
        if _epic_save_responses_enabled():
            _epic_log_http_response(
                "fetch_account_portal_v2",
                "GET",
                ACCOUNT_PORTAL_V2_URL,
                status,
                {
                    "backend": backend,
                    "html_length": len(html),
                    "snippet": html[:12000],
                },
            )
        if status != 200:
            return None
        return extract_account_portal_preload_json(html)

    async def get_fortnite_profile(self, user: EpicUser, profile_id: str) -> json:
        """Query any Fortnite MCP profile (e.g. common_core, theater0, athena)."""
        url = (
            f"https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/game/v2/profile/"
            f"{user.account_id}/client/QueryProfile?profileId={profile_id}&rvn=-1"
        )
        headers = {
            "Authorization": f"bearer {user.access_token}",
            "Content-Type": "application/json",
        }
        r = requests.post(url, headers=headers, json={})
        _requests_log_response(f"get_fortnite_profile_{profile_id}", "POST", url, r)
        return r.json()

    async def get_common_profile(self, user: EpicUser) -> json:
        # gets the common profile, containing vbucks, receipts amount, vbucks purchases history, banners list
        # REQUIRES usage of user.access_token as Authorization in headers
        return await self.get_fortnite_profile(user, "common_core")
    
    async def fetch_friend_codes_merged(self, user: EpicUser) -> List[Dict[str, Any]]:
        """Epic + Xbox friend codes (STW-style), excluding mobile invite tokens."""
        return await asyncio.to_thread(fetch_friend_codes, user.access_token, user.account_id)

    async def fetch_account_entitlements(self, user: EpicUser) -> Any:
        """Account entitlements JSON (``Fortnite_Founder`` / STW checks — same URL as xboxfn.go)."""
        return await asyncio.to_thread(fetch_account_entitlements_sync, user.access_token, user.account_id)

    async def get_public_device_auth(self, user: EpicUser) -> Any:
        """List saved device-auth rows; uses ``device_code_flow_access_token`` (``wait_device_code_token``) if set."""
        bearer = _device_auth_list_bearer_token(user)
        return await asyncio.to_thread(
            fetch_public_device_auth_sync,
            bearer,
            user.account_id,
        )

    async def get_public_device_auth_for_recheck(self, device_auth: Dict[str, Any], account_id: str) -> Any:
        """List saved device-auth rows for recheck using launcher access token."""
        launcher_token = await get_fortnite_launcher_access_token_from_device_auth(device_auth)
        if not launcher_token:
            return None
        return await asyncio.to_thread(
            fetch_public_device_auth_sync,
            launcher_token,
            account_id,
        )

    async def fetch_restriction_removal_availability(
        self, user: EpicUser
    ) -> Optional[Dict[str, Any]]:
        """Epic Help: restriction-removal / relink availability (curl_cffi, bearer cookie)."""
        return await asyncio.to_thread(
            fetch_restriction_removal_availability_sync,
            user.access_token,
        )

    async def get_homebase_profile(self, user: EpicUser) -> json:
        """Full STW campaign profile for the signed-in account (includes power rating).

        ``QueryPublicProfile`` omits private stats (e.g. party power); use ``QueryProfile``.
        """
        return await self.get_fortnite_profile(user, "campaign")
    
    async def set_affiliate(self, user: EpicUser, affiliate_name: str):
        url = f'https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/game/v2/profile/{user.account_id}/client/SetAffiliateName?profileId=common_core'
        payload = { "affiliateName": affiliate_name }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"bearer {user.access_token}"
        }
        
        affiliate_response = requests.request("POST", url, json=payload, headers=headers)
        _requests_log_response("set_affiliate", "POST", url, affiliate_response)
        
        if affiliate_response.status != 200:
            print(f"Error setting affiliate name ({affiliate_response.status})")
        
    async def get_locker_data(self, user: EpicUser) -> Tuple[LockerData, Dict[str, Any]]:
        # gets locker arrays
        # locker_categories - the locker categories we render
        url = f'https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/game/v2/profile/{user.account_id}/client/QueryProfile?profileId=athena'
        headers = {
            "Authorization": f"bearer {user.access_token}",
            "Content-Type": "application/json"
        }

        r_athena = requests.post(url, headers=headers, json={})
        _requests_log_response("get_locker_data_athena", "POST", url, r_athena)
        athena_data = r_athena.json()

        locker_data = LockerData()
        exclusive_cosmetics = []
        popular_cosmetics = []
        if "profileChanges" not in athena_data:
            return LockerData(), athena_data if isinstance(athena_data, dict) else {}
        
        # activity
        account_level = athena_data.get("profileChanges", [{}])[0].get("profile", {}).get("stats", {}).get("attributes", {}).get("accountLevel", 1)
        last_match_end = athena_data.get("profileChanges", [{}])[0].get("profile", {}).get("stats", {}).get("attributes", {}).get("last_match_end_datetime", "")
        if last_match_end:
            last_match_end_date = datetime.fromisoformat(last_match_end.replace("Z", "+00:00"))
            days_since_last_match = (datetime.now(timezone.utc) - last_match_end_date).days
            last_match_date_str = (
                f"{last_match_end_date.strftime('%m/%d/%Y')} · "
                f"{last_match_end_date.strftime('%B %d, %Y')}"
            )
            locker_data.last_match = f"{last_match_date_str} ({days_since_last_match} days ago)"
        else:
            locker_data.last_match = "800+ days ago"

        profile = athena_data["profileChanges"][0].get("profile", {})
        creation_raw = profile.get("creationTime") or profile.get("created")
        locker_data.registration_date = format_epic_iso_ddmmyy(creation_raw) if creation_raw else ""
            
        # getting extra cosmetics  
        try:
            with open('exclusive.txt', 'r', encoding='utf-8') as f:
                exclusive_cosmetics = [i.strip() for i in f.readlines()]

            with open('most_wanted.txt', 'r', encoding='utf-8') as f:
                popular_cosmetics = [i.strip() for i in f.readlines()]
        except FileNotFoundError:
            print("Warning: exclusive.txt or most_wanted.txt not found.")

        # getting owned items list
        for i in athena_data['profileChanges'][0]['profile']['items']:
            template_id = athena_data['profileChanges'][0]['profile']['items'][i]['templateId']
            if template_id.startswith('Athena'):
                cosmetic_category = template_id.split(':')[0]
                cosmetic_id = template_id.split(':')[1]
                locker_data.unlocked_styles[cosmetic_id] = []

                # adding missing categories to the arrays
                if 'AthenaExclusive' not in locker_data.cosmetic_categories:
                    locker_data.cosmetic_categories['AthenaExclusive'] = []
                    locker_data.cosmetic_array['AthenaExclusive'] = []
                    locker_data.cosmetic_categories['AthenaExclusive'].append(cosmetic_id)
                    
                if 'AthenaPopular' not in locker_data.cosmetic_categories:
                    locker_data.cosmetic_categories['AthenaPopular'] = []
                    locker_data.cosmetic_array['AthenaPopular'] = []
                    locker_data.cosmetic_categories['AthenaPopular'].append(cosmetic_id)
                    
                if 'HomebaseBannerIcons' not in locker_data.cosmetic_categories:
                    locker_data.cosmetic_categories['HomebaseBannerIcons'] = []
                    locker_data.cosmetic_array['HomebaseBannerIcons'] = []
                    locker_data.cosmetic_categories['HomebaseBannerIcons'].append(cosmetic_id)

                if cosmetic_category not in locker_data.cosmetic_categories:
                    locker_data.cosmetic_categories[cosmetic_category] = []
                    locker_data.cosmetic_array[cosmetic_category] = []
                locker_data.cosmetic_categories[cosmetic_category].append(cosmetic_id)  
                
        # listing the owned unlocked styles for each cosmetic
        for item_id, item_data in athena_data['profileChanges'][0]['profile']['items'].items():
            template_id = item_data.get('templateId', '')
            
            if template_id.startswith('Athena'):
                lowercase_cosmetic_id = template_id.split(':')[1]

                # adding the cosmetic to the "unlocked styles"
                if lowercase_cosmetic_id not in locker_data.unlocked_styles:
                    locker_data.unlocked_styles[lowercase_cosmetic_id] = []
        
                attributes = item_data.get('attributes', {})
                variants = attributes.get('variants', [])
                
                for variant in variants:
                    # adding the cosmetic's owned styles
                    locker_data.unlocked_styles[lowercase_cosmetic_id].extend(variant.get('owned', []))

        # getting banners
        common_profile_data = await self.get_common_profile(user)
        if common_profile_data:
            # common profile found
            for profileChange in common_profile_data["profileChanges"]:
                profile_items = profileChange["profile"]["items"]

                # checking every item
                for item_key, item_value in profile_items.items():
                    cosmetic_template_id = item_value.get("templateId", "")     
                    if cosmetic_template_id:
                        # the banner is found, so we adding it
                        lowercase_banner_id = cosmetic_template_id.split(':')[1]
                        if lowercase_banner_id not in locker_data.homebase_banners:
                            locker_data.homebase_banners[lowercase_banner_id] = []

        
        for category in locker_categories:
            if category == "AthenaPopular" or category == 'AthenaExclusive':
                continue
            
            try:
                list_of_cosmetic_ids = []
                # splitting the category's cosmetic ids to sublists
                for i in range(0, len(locker_data.cosmetic_categories[category]), 50):
                    sublist = locker_data.cosmetic_categories[category][i:i+50]
                    list_of_cosmetic_ids.append(sublist)

                for cosmetic_id in list_of_cosmetic_ids:
                    fn_url = 'https://fortnite-api.com/v2/cosmetics/br/search/ids?language=en&id={}'.format('&id='.join(cosmetic_id))
                    cosmetics_data = requests.get(fn_url)
                    _requests_log_response(
                        "fortnite_api_cosmetics_br_search", "GET", fn_url, cosmetics_data
                    )
                    c_data = cosmetics_data.json().get("data", [])
                    if not c_data:
                        # cosmetics data not found
                        continue

                    for cosmetic in c_data:
                        if cosmetic['id'] == "CID_DefaultOutfit":
                            # skipping default skin
                            continue
                        
                        if category == 'AthenaDance' and cosmetic['type']['value'] != 'emote' and cosmetic['id'] not in exclusive_cosmetics:
                            # emote category, but isnt exclusive and isnt emote
                            continue

                        make_mythic = False
                        if cosmetic['id'] in exclusive_cosmetics:
                            make_mythic = True
                            # Pink Ghoul Trooper
                            if cosmetic['id'].lower() == 'cid_029_athena_commando_f_halloween':
                                make_mythic = False
                                if 'Mat3' in locker_data.unlocked_styles.get('cid_029_athena_commando_f_halloween', []):
                                    make_mythic = True
                            
                            # Purple Skull Trooper
                            if cosmetic['id'].lower() == 'cid_030_athena_commando_m_halloween':
                                make_mythic = False
                                if 'Mat1' in locker_data.unlocked_styles.get('cid_030_athena_commando_m_halloween', []):
                                    make_mythic = True               
                            
                            # Aerial Assault Trooper
                            if cosmetic['id'].lower() == 'cid_017_athena_commando_m':
                                make_mythic = False
                                if 'Stage2' in locker_data.unlocked_styles.get('cid_017_athena_commando_m', []):
                                    make_mythic = True
                                    
                            # Renegade Raider
                            if cosmetic['id'].lower() == 'cid_028_athena_commando_f':
                                make_mythic = False
                                if 'Mat3' in locker_data.unlocked_styles.get('cid_028_athena_commando_f', []):
                                    make_mythic = True  
                                    
                            # Raider's Revenge
                            if cosmetic['id'].lower() == 'pickaxe_lockjaw':
                                make_mythic = False
                                if 'Stage2' in locker_data.unlocked_styles.get('pickaxe_lockjaw', []):
                                    make_mythic = True  
                                    
                            # Aerial Assault One
                            if cosmetic['id'].lower() == 'glider_id_001':
                                make_mythic = False
                                if 'Stage2' in locker_data.unlocked_styles.get('glider_id_001', []):
                                    make_mythic = True  
                                    
                            # Stage 5 Omega Lights
                            if cosmetic['id'].lower() == 'cid_116_athena_commando_m_carbideblack':
                                make_mythic = False
                                if 'Stage5' in locker_data.unlocked_styles.get('cid_116_athena_commando_m_carbideblack', []):
                                    make_mythic = True
                            
                            # Gold Midas
                            if cosmetic['id'].lower() == 'cid_694_athena_commando_m_catburglar':
                                make_mythic = False
                                if 'Stage4' in locker_data.unlocked_styles.get('cid_694_athena_commando_m_catburglar', []):
                                    make_mythic = True
                            
                            # Gold Meowscles
                            if cosmetic['id'].lower() == 'cid_693_athena_commando_m_buffcat':
                                make_mythic = False
                                if 'Stage4' in locker_data.unlocked_styles.get('cid_693_athena_commando_m_buffcat', []):
                                    make_mythic = True
                            
                            # Gold TNtina
                            if cosmetic['id'].lower() == 'cid_691_athena_commando_f_tntina':
                                make_mythic = False
                                if 'Stage7' in locker_data.unlocked_styles.get('cid_691_athena_commando_f_tntina', []):
                                    make_mythic = True
                                    
                            # Gold Skye
                            if cosmetic['id'].lower() == 'cid_690_athena_commando_f_photographer':
                                make_mythic = False
                                if 'Stage4' in locker_data.unlocked_styles.get('cid_690_athena_commando_f_photographer', []):
                                    make_mythic = True
                                    
                            # Gold Agent Peely
                            if cosmetic['id'].lower() == 'cid_701_athena_commando_m_bananaagent':
                                make_mythic = False
                                if 'Stage4' in locker_data.unlocked_styles.get('cid_701_athena_commando_m_bananaagent', []):
                                    make_mythic = True
                            
                            # World Cup Fishtick
                            if cosmetic['id'].lower() == 'cid_315_athena_commando_m_teriyakifish':
                                make_mythic = False
                                if 'Stage3' in locker_data.unlocked_styles.get('cid_315_athena_commando_m_teriyakifish', []):
                                    make_mythic = True
                            
                            # Mate Black Masterchief
                            if cosmetic['id'].lower() == 'cid_971_athena_commando_m_jupiter_s0z6m':
                                make_mythic = False
                                if 'Mat2' in locker_data.unlocked_styles.get('cid_971_athena_commando_m_jupiter_s0z6m', []):
                                    make_mythic = True
                            
                            if make_mythic == True:
                                cosmetic['rarity']['value'] = 'mythic'
                                
                        cosmetic_info = FortniteCosmetic()
                        cosmetic_info.cosmetic_id = cosmetic['id']
                        cosmetic_info.name = cosmetic['name']
                        cosmetic_info.small_icon = cosmetic['images']['smallIcon']
                        cosmetic_info.backend_value = category
                        cosmetic_info.rarity_value = cosmetic['rarity']['value']
                        cosmetic_info.is_banner = False
                        cosmetic_info.is_exclusive = make_mythic
                        cosmetic_info.is_popular = cosmetic['id'] in popular_cosmetics
                        cosmetic_info.unlocked_styles = locker_data.unlocked_styles[cosmetic['id'].lower()]

                        locker_data.cosmetic_array[category].append(cosmetic_info) 

                        # now special ones
                        if cosmetic_info.is_popular:
                            locker_data.cosmetic_array['AthenaPopular'].append(cosmetic_info)

                        if make_mythic:
                            locker_data.cosmetic_array['AthenaExclusive'].append(cosmetic_info)

            except Exception as e:
                print(f'exception: {e}')
                continue

        # handle banners
        banners_url = 'https://fortnite-api.com/v1/banners'
        banners_data = requests.get(banners_url)
        _requests_log_response("fortnite_api_banners", "GET", banners_url, banners_data)
        for fn_banner in banners_data.json()['data']:
            banner_lower_id = fn_banner['id'].lower()
            if banner_lower_id not in locker_data.homebase_banners:
                # banner isn't owned
                continue
            
            make_mythic = False
            icon = fn_banner['images']['icon']
            rarity = 'uncommon'
            if fn_banner['id'] in exclusive_cosmetics:
                make_mythic = True
                rarity = 'mythic'
                                    
            # for future
            cosmetic_info = FortniteCosmetic()
            cosmetic_info.cosmetic_id = fn_banner['id']
            cosmetic_info.name = fn_banner['devName']
            cosmetic_info.small_icon = fn_banner['images']['smallIcon']
            cosmetic_info.rarity_value = rarity
            cosmetic_info.backend_value = 'HomebaseBannerIcons'
            cosmetic_info.is_banner = True
            cosmetic_info.is_exclusive = make_mythic
            cosmetic_info.is_popular = fn_banner['id'] in popular_cosmetics
                
            locker_data.cosmetic_array['HomebaseBannerIcons'].append(cosmetic_info)      
            # now exclusive ones
            if make_mythic:
                locker_data.cosmetic_array['AthenaExclusive'].append(cosmetic_info)

        # sorting exclusives category
        locker_data.cosmetic_array['AthenaExclusive'].sort(
            key=lambda cosmetic: exclusive_cosmetics.index(cosmetic.cosmetic_id) 
            if cosmetic.cosmetic_id in exclusive_cosmetics 
            else float('inf')
        )

        # returning back the locker data and raw Athena payload (for account statistics UI)
        return locker_data, athena_data

    async def get_seasons_message(self, user: EpicUser) -> str:
        url = f'https://fortnite-public-service-prod11.ol.epicgames.com/fortnite/api/game/v2/profile/{user.account_id}/client/QueryProfile?profileId=athena'
        headers = {
            "Authorization": f"bearer {user.access_token}",
            "Content-Type": "application/json"
        }

        r_seasons = requests.post(url, headers=headers, json={})
        _requests_log_response("get_seasons_message_athena", "POST", url, r_seasons)
        athena_data = r_seasons.json()

        seasons_info = []

        attrs = (
            athena_data.get("profileChanges", [{}])[0]
            .get("profile", {})
            .get("stats", {})
            .get("attributes", {})
        )
        past_seasons = attrs.get("past_seasons", [])

        curses = attrs
        cursesinfo = {
            'level': curses.get('level', 1),
            'book_level': curses.get('book_level', 1)
        }
            
        for season in past_seasons:
            seasons_info.append(f"""
#️⃣Season {season.get('seasonNumber', 1)}
› Level: {season.get('seasonLevel', '1')}
› Battle Pass: {bool_to_emoji(season.get('purchasedVIP', False))}
› Wins: {season.get('numWins', 0)}
            """)

        seasons_info_embeds = seasons_info
        seasons_info_message = "Previous Seasons History:\n" + "\n".join(seasons_info_embeds)
        seasons_info_message += f"\nCurrent Season:\n› Level: {cursesinfo['level']}\n› Battle Pass Level: {cursesinfo['book_level']}"
        return seasons_info_message
import os
import re
import asyncio
import time
import threading
from functools import partial
from types import SimpleNamespace
from pathlib import Path
from PIL import Image
import json
import user
import logging
import urllib.request
import requests
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timezone
from io import BytesIO
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from utils import (
    bool_to_emoji,
    build_pci_api_payment_methods_document,
    build_recovery_fields_document,
    format_recovery_fields_document_as_txt,
    country_to_flag,
    escape_html_telegram,
    format_can_update_email_line,
    format_date_mmddyyyy,
    format_epic_email_last_change_ddmmyy,
    format_metadata_date_yyyy_mm_dd,
    format_account_statistics_telegram,
    format_device_auth_telegram,
    format_account_payment_methods_telegram_html,
    format_restriction_relink_telegram_html,
    format_friend_codes_stw_telegram,
    format_full_locker_summary_telegram,
    format_portal_2fa_methods_text,
    stw_daily_login_from_stats,
    stw_edition_flags_from_campaign_items,
    stw_format_daily_login_line,
    stw_format_owned_line,
    stw_power_from_stats,
    stw_parse_theater_world_resources,
    stw_research_levels_from_stats,
    send_telegram_message_chunks,
    format_epic_oauth_tokens_telegram_html,
    tg_code,
)
from user import RiftUser
from cosmetic import FortniteCosmetic
from epic_auth import (
    EpicUser,
    EpicEndpoints,
    EpicGenerator,
    LockerData,
    generate_epic_web_login_url_from_device_auth_sync,
    get_fortnite_launcher_exchange_code_from_device_auth_sync,
    delete_device_auth_on_epic_servers_sync,
    device_auth_oauth_access_token_bearer_sync,
    epic_user_from_device_auth,
    epic_user_from_exchange_code,
    friends_get_summary_sync,
    friends_incoming_account_ids,
    friends_outgoing_account_ids,
    friends_confirmed_friends_account_ids,
    friends_list_entries_from_summary,
    friends_accept_incoming_bulk_sync,
    friends_reject_incoming_sync,
    friends_add_sync,
    friends_remove_sync,
    friends_remove_all_sync,
    resolve_epic_account_id_from_text_sync,
    resolve_epic_public_display_names_batch_sync,
    verify_epic_content_control_pin_sync,
)
from locker_gen import render_raika_style, get_theme_order, resolve_cosmetic_display_name
from admin_api_summary import (
    API_SUMMARY_RECIPIENT,
    render_html_summary,
    session_add,
    session_clear,
    session_start,
    summary_sending_enabled,
)
from orders import build_full_export, build_payment_methods_export
from epic_parental_controls import (
    fetch_parental_controls_get_sync,
    parental_controls_pin_exists,
)

# Telegram sendPhoto: must be under 10 MiB — target just under the limit (not ~3% early).
_TELEGRAM_PHOTO_SAFE_BYTES = 10 * 1024 * 1024 - 64 * 1024  # 10 MiB − 64 KiB margin

_ERR_HTML_TAG_RE = re.compile(r"<[^>]*>")

_SENSITIVE_TOKEN_ALLOWED_USER_IDS: set[int] | None = None


def _sensitive_token_allowlist() -> set[int]:
    """
    Comma-separated Telegram user IDs in env var ``RIFT_SENSITIVE_TOKEN_ALLOWLIST``.
    Example: 123,456
    """
    global _SENSITIVE_TOKEN_ALLOWED_USER_IDS
    if _SENSITIVE_TOKEN_ALLOWED_USER_IDS is not None:
        return _SENSITIVE_TOKEN_ALLOWED_USER_IDS
    raw = (os.environ.get("RIFT_SENSITIVE_TOKEN_ALLOWLIST") or "").strip()
    ids: set[int] = set()
    if raw:
        for part in raw.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                ids.add(int(p))
            except ValueError:
                continue
    _SENSITIVE_TOKEN_ALLOWED_USER_IDS = ids
    return ids


def _can_send_sensitive_tokens(tg_user: RiftUser, user_data: dict) -> bool:
    if bool(user_data.get("vip", False)):
        return True
    try:
        return int(tg_user.userID) in _sensitive_token_allowlist()
    except Exception:
        return False


def _safe_user_error(text: str) -> str:
    """Hide auth/token wording from user-facing errors."""
    s = (text or "").strip()
    if not s:
        return "Something went wrong. Try again."
    s = re.sub(r"(?i)access\s*token", "session", s)
    s = re.sub(r"(?i)device\s*auth", "saved login", s)
    s = re.sub(r"(?i)oauth", "login", s)
    s = re.sub(r"(?i)bearer", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_BP_STATE: dict[int, dict] = {}
_BP_STATE_TTL_SEC = 5 * 60  # 5 minutes


def _bp_cleanup_state(uid: int) -> None:
    st = _BP_STATE.get(uid)
    if not isinstance(st, dict):
        return
    ts = float(st.get("ts") or 0.0)
    if ts and (time.time() - ts) > _BP_STATE_TTL_SEC:
        _BP_STATE.pop(uid, None)


def _bp_animation_start(bot, chat_id: int, message_id: int, uid: int, *, label: str) -> None:
    """
    Lightweight message animation via edit (no extra messages).
    Stops automatically when state flag is set or state expires.
    """
    st = _BP_STATE.get(uid)
    if not isinstance(st, dict):
        return
    stop_key = f"anim_stop_{message_id}"
    st[stop_key] = False

    def _run():
        frames = [".", "..", "..."]
        i = 0
        while True:
            cur = _BP_STATE.get(uid) or {}
            if cur.get(stop_key):
                return
            ts = float(cur.get("ts") or 0.0)
            if ts and (time.time() - ts) > _BP_STATE_TTL_SEC:
                return
            try:
                bot.edit_message_text(
                    f"<b>Bypass</b>\n\n⏳ {label}{frames[i % len(frames)]}",
                    chat_id,
                    message_id,
                    parse_mode="HTML",
                )
            except Exception:
                # Ignore edit failures (message deleted / same text / etc.)
                pass
            i += 1
            time.sleep(1.0)

    th = threading.Thread(target=_run, name="bp_anim", daemon=True)
    th.start()


def _bp_animation_stop(uid: int, message_id: int) -> None:
    st = _BP_STATE.get(uid)
    if isinstance(st, dict):
        st[f"anim_stop_{message_id}"] = True

# Minimal provider configs (ported from xtc bypasser).
_BP_PROVIDERS: dict[str, dict] = {
    "facebook": {
        "name": "Facebook",
        "url": "https://www.facebook.com/dialog/oauth?client_id=1132078350149238&redirect_uri=https://accounts.epicgames.com/OAuthAuthorized&state=eyJpZCI6ImU0MDY2YTAzODU2MzRmOGJiMDQ3ODJkZGMzZmEyY2Q2In0=&scope=email,public_profile,user_friends&response_type=token&display=popup",
        "auth_type": "facebook",
        "token_param": "access_token",
    },
    "google": {
        "name": "Google",
        "url": "https://accounts.google.com/o/oauth2/auth?client_id=81931294547-ict6llss8611g9nglndn2bnln48bo59d.apps.googleusercontent.com&redirect_uri=https://accounts.epicgames.com/OAuthAuthorized&response_type=id_token&scope=openid%20email%20profile",
        "auth_type": "google_id_token",
        "token_param": "id_token",
    },
    "nintendo": {
        "name": "Nintendo",
        "url": "https://accounts.nintendo.com/connect/1.0.0/authorize?client_id=1f6a6a4806931686&redirect_uri=https://accounts.epicgames.com/OAuthAuthorized&response_type=id_token&scope=openid&state=STATE",
        "auth_type": "nintendo_id_token",
        "token_param": "id_token",
    },
    "xbox": {
        "name": "Xbox",
        "url": "https://login.live.com/oauth20_authorize.srf?client_id=82023151-c27d-4fb5-8551-10c10724a55e&redirect_uri=https%3A%2F%2Faccounts.epicgames.com%2FOAuthAuthorized&state=&scope=xbl.signin&service_entity=undefined&force_verify=true&response_type=code&display=popup",
        "auth_type": "xbl",
        "token_param": "code",
    },
}

_BP_AUTH_HEADER = "basic Y2ZhYTE0YzRiZjg3NDRlM2E1ZWY5YTVkNmMzNDU1OGQ6YmNiMGNkMzkyZmNkNGU1MGE3NmFkNDM4NGM2MjA1NDM="
_BP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


def _bp_extract_token_from_redirected_url(url: str, token_param: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    parsed = urlparse(u)
    if parsed.fragment:
        frag = parse_qs(parsed.fragment)
        if token_param in frag and frag[token_param]:
            return frag[token_param][0]
    qs = parse_qs(parsed.query)
    if token_param in qs and qs[token_param]:
        return qs[token_param][0]
    return ""


def _bp_make_exchange_code(url: str, provider: dict) -> str:
    token = _bp_extract_token_from_redirected_url(url, provider["token_param"])
    if not token:
        return ""
    sess = requests.Session()
    sess.headers.update({"User-Agent": _BP_UA})

    oauth_data = {
        "grant_type": "external_auth",
        "external_auth_type": provider["auth_type"],
        "external_auth_token": token,
    }
    resp = sess.post(
        "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token",
        data=oauth_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": _BP_AUTH_HEADER,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        return ""
    access_token = (resp.json() or {}).get("access_token") or ""
    if not access_token:
        return ""
    ex = sess.get(
        "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/exchange",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if ex.status_code != 200:
        return ""
    return (ex.json() or {}).get("code") or ""


def command_bp(bot, message) -> None:
    if getattr(message.chat, "type", None) != "private":
        return
    tg_user = RiftUser(message.from_user.id, message.from_user.username or "")
    user_data = _ensure_user_profile(tg_user)
    if not user_data:
        bot.reply_to(message, "Use /start first.")
        return
    markup = InlineKeyboardMarkup()
    for k, p in _BP_PROVIDERS.items():
        markup.add(InlineKeyboardButton(f"🔓 {p['name']}", callback_data=f"bp_t_{k}"))
    markup.add(InlineKeyboardButton("✖️ Cancel", callback_data="bp_cancel"))
    bot.send_message(
        message.chat.id,
        "<b>Bypass</b>\n"
        "<i>Select a provider to generate an exchange link.</i>",
        reply_markup=markup,
        parse_mode="HTML",
    )


def handle_bp_callback(bot, call) -> None:
    data = call.data or ""
    cid = call.message.chat.id
    mid = call.message.message_id
    uid = int(call.from_user.id)
    _bp_cleanup_state(uid)

    if data == "bp_cancel":
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text("Cancelled.", cid, mid)
        except Exception:
            pass
        _BP_STATE.pop(uid, None)
        return

    if data.startswith("bp_t_"):
        key = data.split("_", 2)[2]
        prov = _BP_PROVIDERS.get(key)
        if not prov:
            bot.answer_callback_query(call.id, "Unknown type.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        _BP_STATE[uid] = {"provider": key, "menu_mid": mid, "ts": time.time()}
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🌐 Open login", url=prov["url"]))
        markup.add(InlineKeyboardButton("✖️ Cancel", callback_data="bp_cancel"))
        bot.edit_message_text(
            "<b>Bypass</b>\n\n"
            "1) Tap <b>Open login</b>\n"
            "2) Finish login\n"
            "3) Copy the <b>redirected URL</b> and paste it in chat.\n\n"
            "<b>Example</b>\n"
            "<code>https://accounts.epicgames.com/OAuthAuthorized...</code>",
            cid,
            mid,
            reply_markup=markup,
            parse_mode="HTML",
        )
        prompt = bot.send_message(
            cid,
            "<b>Send the redirected URL</b>\n\n"
            "After you paste it, this message will be removed automatically.",
            parse_mode="HTML",
        )
        _BP_STATE[uid]["prompt_mid"] = getattr(prompt, "message_id", None)
        bot.register_next_step_handler(prompt, _bp_step_receive_url, bot, uid)
        return

    if data.startswith("bp_do_"):
        mode = data[len("bp_do_") :]
        st = _BP_STATE.get(uid) or {}
        _bp_cleanup_state(uid)
        code = st.get("exchange_code") or ""
        if not code:
            bot.answer_callback_query(call.id, "Expired. Start /bp again.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        if mode == "web":
            web_url = (
                f"https://www.epicgames.com/id/exchange?exchangeCode={quote(str(code), safe='')}"
                "&redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Faccount%2Fpersonal%3Fmode%3Dgame"
                "&prompt=none"
            )
            bot.edit_message_text(
                "<b>Web login</b>\n\n" + tg_code(web_url),
                cid,
                mid,
                parse_mode="HTML",
            )
            _BP_STATE.pop(uid, None)
            return
        if mode == "save":
            bot.edit_message_text("<b>Bypass</b>\n\n⏳ Saving…", cid, mid, parse_mode="HTML")
            _bp_animation_start(bot, cid, mid, uid, label="Saving")
            _bp_save_account_from_exchange(bot, cid, uid, code)
            _bp_animation_stop(uid, mid)
            _BP_STATE.pop(uid, None)
            return
        bot.answer_callback_query(call.id, "Unknown action.", show_alert=True)
        return


def _bp_step_receive_url(message, bot, uid: int) -> None:
    st = _BP_STATE.get(uid) or {}
    _bp_cleanup_state(uid)
    key = st.get("provider")
    prov = _BP_PROVIDERS.get(key or "")
    if not prov:
        bot.send_message(message.chat.id, "Expired. Start /bp again.")
        return

    chat_id = message.chat.id
    # Remove the bot prompt (keeps chat clean).
    pmid = st.get("prompt_mid")
    if pmid:
        try:
            bot.delete_message(chat_id, pmid)
        except Exception:
            pass
    try:
        bot.delete_message(chat_id, message.message_id)
    except Exception:
        pass

    menu_mid = st.get("menu_mid")
    if menu_mid:
        try:
            bot.edit_message_text(
                "<b>Bypass</b>\n\n⏳ Processing…",
                chat_id,
                menu_mid,
                parse_mode="HTML",
            )
        except Exception:
            pass

    url = (message.text or "").strip()
    try:
        if menu_mid:
            _bp_animation_start(bot, chat_id, menu_mid, uid, label="Processing")
        code = _bp_make_exchange_code(url, prov)
    except Exception:
        logging.exception("bp exchange")
        code = ""
    finally:
        if menu_mid:
            _bp_animation_stop(uid, menu_mid)

    if not code:
        if menu_mid:
            bot.edit_message_text(
                "🚫 The account is invalid. Please log in again",
                chat_id,
                menu_mid,
            )
        else:
            bot.send_message(chat_id, "🚫 The account is invalid. Please log in again")
        _BP_STATE.pop(uid, None)
        return

    _BP_STATE[uid]["exchange_code"] = code
    _BP_STATE[uid]["ts"] = time.time()
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💾 Save account in bot", callback_data="bp_do_save"))
    markup.add(InlineKeyboardButton("🌐 Web login", callback_data="bp_do_web"))
    markup.add(InlineKeyboardButton("✖️ Cancel", callback_data="bp_cancel"))
    bot.edit_message_text(
        "<b>Bypass</b>\n\nChoose what you want:",
        chat_id,
        menu_mid,
        reply_markup=markup,
        parse_mode="HTML",
    )


def _bp_save_account_from_exchange(bot, chat_id: int, uid: int, exchange_code: str) -> None:
    async def _go():
        tg_user = RiftUser(uid, "")
        user_data = _ensure_user_profile(tg_user) or {}
        epic_generator = EpicGenerator()
        await epic_generator.start()
        try:
            epic_user = await epic_user_from_exchange_code(exchange_code)
            if not epic_user:
                bot.send_message(chat_id, "🚫 The account is invalid. Please log in again")
                await epic_generator.kill()
                return
            pm = bot.send_message(chat_id, "⏳ Checking…")
            try:
                await command_login_post_auth(
                    bot,
                    SimpleNamespace(
                        chat=SimpleNamespace(id=chat_id),
                        from_user=SimpleNamespace(id=uid),
                    ),
                    tg_user,
                    user_data,
                    epic_generator,
                    epic_user,
                    pm,
                    recheck=True,
                    verb_override="Logged in",
                )
            finally:
                session_clear()
        except Exception:
            logging.exception("bp save account")
            bot.send_message(chat_id, "🚫 The account is invalid. Please log in again")
            try:
                await epic_generator.kill()
            except Exception:
                pass

    asyncio.run(_go())


def _epic_friend_error_text_for_tg(raw: str) -> str:
    """Epic/API errors may include HTML; strip tags, then escape for Telegram HTML."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = _ERR_HTML_TAG_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return escape_html_telegram(s)


def ensure_png_under_telegram_photo_limit(path: str) -> None:
    """If the PNG is already under the limit, leave it unchanged.

    Otherwise re-save with PNG optimization, then if still too large shrink dimensions
    as little as possible (binary search on scale) instead of a fixed max edge like 1280.
    """
    try:
        if os.path.getsize(path) <= _TELEGRAM_PHOTO_SAFE_BYTES:
            return
    except OSError:
        return

    img = Image.open(path).convert("RGBA")
    w, h = img.size

    def png_payload(im: Image.Image) -> bytes:
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True, compress_level=9)
        return buf.getvalue()

    data = png_payload(img)
    if len(data) <= _TELEGRAM_PHOTO_SAFE_BYTES:
        with open(path, "wb") as f:
            f.write(data)
        return

    lo, hi = 0.03, 1.0
    best: bytes | None = None
    for _ in range(36):
        mid = (lo + hi) / 2
        nw, nh = max(1, int(w * mid)), max(1, int(h * mid))
        cand = img.resize((nw, nh), Image.Resampling.LANCZOS)
        data = png_payload(cand)
        if len(data) <= _TELEGRAM_PHOTO_SAFE_BYTES:
            best = data
            lo = mid
        else:
            hi = mid

    if best is not None:
        with open(path, "wb") as f:
            f.write(best)
        return

    s = 0.03
    while s >= 0.005:
        nw, nh = max(1, int(w * s)), max(1, int(h * s))
        cand = img.resize((nw, nh), Image.Resampling.LANCZOS)
        data = png_payload(cand)
        if len(data) <= _TELEGRAM_PHOTO_SAFE_BYTES:
            with open(path, "wb") as f:
                f.write(data)
            return
        s *= 0.85


class FortniteCache:
    def __init__(self):
        self.cache = {}
        self.cache_dir = "cache"
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        
        self.load_cache_from_directory()
        
    def load_cache_from_directory(self):
        for filename in os.listdir(self.cache_dir):
            if filename.endswith(".png"):
                id = os.path.splitext(filename)[0]
                file_path = os.path.join(self.cache_dir, filename)
                try:
                    image = Image.open(file_path).convert('RGBA')
                    self.cache[id] = image
                except Exception as e:
                    continue
                    
    def get_cosmetic_icon_from_cache(self, url, id):
        if not url:
            print(f"Error: No URL provided for ID: {id}")
            return None
        
        cache_path = os.path.join(self.cache_dir, f"{id}.png")
        if id in self.cache:
            return self.cache[id]

        if os.path.exists(cache_path):
            # getting the icon from filesystem
            try:
                image = Image.open(cache_path).convert('RGBA')
                self.cache[id] = image
                return image
            except Exception as e:
                print(f"Error loading {cache_path}: {e}")

        try:
            # downloading the icon from url
            with urllib.request.urlopen(url) as response:
                image_data = response.read()
                image = Image.open(BytesIO(image_data)).convert('RGBA')
                try:
                    image.save(cache_path)
                except Exception as e:
                    print(f"Error saving {cache_path}: {e}")
                
                self.cache[id] = image
                return image 
        except Exception as e:
            print(f"Error downloading image from {url}: {e}")
            return None

# global members
fortnite_cache = FortniteCache()

def _theme_display_name(key: str) -> str:
    return key.replace("_", " ").title()


def build_available_themes():
    order = get_theme_order()
    return [
        {
            "ID": i,
            "key": k,
            "name": _theme_display_name(k),
            "image": f"img/themes/{k}.png",
        }
        for i, k in enumerate(order)
    ]


available_themes = build_available_themes()


def resolve_theme_index(user_data) -> int:
    order = get_theme_order()
    n = len(order)
    if n < 1:
        return 0
    if "theme" not in user_data:
        t = int(user_data.get("style", 0))
    else:
        t = int(user_data["theme"])
    return max(0, min(t, n - 1))


def theme_key_for_user(user_data) -> str:
    order = get_theme_order()
    return order[resolve_theme_index(user_data)]


avaliable_badges = [
    {"name": "Alpha Tester 1", "data": "alpha_tester_1_badge", "data2": "alpha_tester_1_badge_active", "image": "badges/icon/alpha1.png"},
    {"name": "Alpha Tester 2", "data": "alpha_tester_2_badge", "data2": "alpha_tester_2_badge_active", "image": "badges/icon/alpha2.png"},
    {"name": "Alpha Tester 3", "data": "alpha_tester_3_badge", "data2": "alpha_tester_3_badge_active", "image": "badges/icon/alpha3.png"},
    {"name": "Epic Games", "data": "epic_badge", "data2": "epic_badge_active", "image": "badges/icon/epic.png"}
]

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


def _locker_category_header(category: str) -> str:
    if category == "AthenaBackpack":
        return "Backblings"
    if category == "AthenaPickaxe":
        return "Pickaxes"
    if category == "AthenaDance":
        return "Emotes"
    if category == "AthenaGlider":
        return "Gliders"
    if category == "AthenaItemWrap":
        return "Wraps"
    if category == "AthenaLoadingScreen":
        return "Loading screens"
    if category == "AthenaMusicPack":
        return "Music packs"
    if category == "AthenaExclusive":
        return "Exclusives"
    if category == "AthenaPopular":
        return "Popular"
    return "Outfits"


def _render_locker_category_png(
    category: str,
    save_path: str,
    user_data,
    items,
    cache: FortniteCache,
    theme_key: str,
) -> str:
    path = f"{save_path}/{category}.png"
    render_raika_style(
        _locker_category_header(category),
        user_data,
        items,
        path,
        cache=cache,
        theme_key=theme_key,
    )
    ensure_png_under_telegram_photo_limit(path)
    return path


# global members
    
    
def _ensure_user_profile(user) -> dict:
    """Load JSON profile; register a new file if missing."""
    data = user.load_data()
    if data:
        return data
    reg = user.register()
    if reg:
        return reg
    return user.load_data()


def build_start_menu_markup() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📂 Saved Accounts", callback_data="sav"))
    markup.add(InlineKeyboardButton("📁 Fortnite folder", callback_data="gph"))
    markup.add(InlineKeyboardButton("ℹ️ Help", callback_data="hlp"))
    return markup


START_MENU_TEXT = (
    "Welcome! Use <b>/login</b> to check your Fortnite locker — we create a "
    "<b>device auth</b> and save the account here.\n\n"
    "The <b>Play Fortnite</b> button sends <code>start.bat</code> (searches drives / cached path). "
    "Optional: <b>/setgamepath</b> only affects the sample command line in the message.\n\n"
    "Choose an option below."
)


_DEFAULT_FORTNITE_ROOT = r"C:\Program Files\Epic Games\Fortnite"


def _example_win64_path_for_caption(saved_root: str) -> str:
    """Folder shown in the copy-paste line (Epic Win64)."""
    if not (saved_root or "").strip():
        return os.path.join(_DEFAULT_FORTNITE_ROOT, "FortniteGame", "Binaries", "Win64")
    root = saved_root.strip().strip('"').replace("/", "\\").rstrip("\\")
    low = root.lower()
    if low.endswith("fortnitelauncher.exe"):
        return os.path.dirname(root)
    if low.endswith("win64"):
        return root
    return os.path.join(root, "FortniteGame", "Binaries", "Win64")


def build_fortnite_start_bat(launcher_exchange_code: str, account_id: str) -> str:
    """
    Auto-detect ``FortniteLauncher.exe`` across drives; cache folder in ``%%TEMP%%\\FortnitePath.txt``.
    Same structure as the reference launcher batch.
    """
    code = str(launcher_exchange_code).strip()
    aid = str(account_id).strip()
    # batch-safe: Epic codes are typically [a-f0-9]; avoid & | < > newlines
    for bad in ("\r", "\n", "&", "|", "<", ">", "^"):
        if bad in code:
            code = code.replace(bad, "")
    if not code or not aid:
        return ""

    return (
        "@echo off\r\n"
        "\r\n"
        "setlocal enabledelayedexpansion\r\n"
        "title Launch Fortnite\r\n"
        "echo Finding a path to the game\r\n"
        "\r\n"
        'set "tempFilePath=%TEMP%\\FortnitePath.txt"\r\n'
        "\r\n"
        'if exist "!tempFilePath!" (\r\n'
        '    set /p fortnitePath=<"!tempFilePath!"\r\n'
        "    \r\n"
        "    :trimStart\r\n"
        '    if "!fortnitePath:~0,1!"==" " set "fortnitePath=!fortnitePath:~1!"\r\n'
        '    if "!fortnitePath:~0,1!"==" " goto trimStart\r\n'
        "\r\n"
        "    :trimEnd\r\n"
        '    if "!fortnitePath:~-1!"==" " set "fortnitePath=!fortnitePath:~0,-1!"\r\n'
        '    if "!fortnitePath:~-1!"==" " goto trimEnd\r\n'
        "    \r\n"
        f'    start "" /d "!fortnitePath!" FortniteLauncher.exe -AUTH_LOGIN=unused -AUTH_PASSWORD={code} -AUTH_TYPE=exchangecode -epicapp=Fortnite -epicenv=Prod -EpicPortal -epicuserid={aid}\r\n'
        "    goto end\r\n"
        ")\r\n"
        "\r\n"
        "for %%I in (D: C: E: F: G: H: I: J: K: L: M: N: O: P: Q: R: S: T: U: V: W: X: Y: Z:) do (\r\n"
        "    if exist %%I (\r\n"
        '        for /f "delims=" %%f in (\'dir %%I\\FortniteLauncher.exe /s /b 2^>NUL\') do (\r\n'
        '            set "fortnitePath=%%f"\r\n'
        '            echo !fortnitePath:~0,-21! > "!tempFilePath!"\r\n'
        f'            start "" /d "!fortnitePath:~0,-21!" FortniteLauncher.exe -AUTH_LOGIN=unused -AUTH_PASSWORD={code} -AUTH_TYPE=exchangecode -epicapp=Fortnite -epicenv=Prod -EpicPortal -epicuserid={aid}\r\n'
        "            goto end\r\n"
        "        )\r\n"
        "    )\r\n"
        ")\r\n"
        "\r\n"
        "echo Fortnite not found.\r\n"
        "goto end\r\n"
        "\r\n"
        ":end\r\n"
        "\r\n"
    )


def command_start(bot, message):
    if message.chat.type != "private":
        return

    user = RiftUser(message.from_user.id, message.from_user.username or "")
    user_data = _ensure_user_profile(user)
    if not user_data:
        bot.reply_to(message, "Could not create your profile. Try /start again.")
        return

    bot.reply_to(
        message,
        START_MENU_TEXT,
        reply_markup=build_start_menu_markup(),
        parse_mode="HTML",
    )
    
def command_help(bot, message):
    bot.reply_to(
        message,
        """Commands:
/start — main menu (Saved Accounts button)
/help — show this message
/login — Epic login and locker check
/setgamepath — optional path for the sample command in Play Fortnite messages
/theme — locker color palette
/badges — toggle badges on locker images
/stats — your usage stats""",
    )


def command_set_game_path(bot, message):
    """Save ``fortnite_game_root`` (folder containing FortniteGame\\...)."""
    if message.chat.type != "private":
        return
    user = RiftUser(message.from_user.id, message.from_user.username or "")
    user_data = _ensure_user_profile(user)
    if not user_data:
        bot.reply_to(message, "Use /start first.")
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        cur = (user_data.get("fortnite_game_root") or "").strip()
        cur_html = (
            f"\n\nCurrent: {tg_code(cur)}"
            if cur
            else "\n\n<i>No path saved — the .bat uses the default Program Files location. Send a path to override.</i>"
        )
        bot.reply_to(
            message,
            "<b>/setgamepath</b> <code>C:\\Program Files\\Epic Games\\Fortnite</code>\n\n"
            "Must be the folder that contains "
            "<code>FortniteGame\\Binaries\\Win64\\FortniteLauncher.exe</code>."
            + cur_html,
            parse_mode="HTML",
        )
        return
    path = parts[1].strip().strip('"')
    user.load_data()
    user.user_data["fortnite_game_root"] = path
    user.update_data()
    bot.reply_to(
        message,
        "Saved Fortnite install root:\n" + tg_code(path),
        parse_mode="HTML",
    )
    
async def command_login_post_auth(
    bot,
    message,
    user: RiftUser,
    user_data: dict,
    epic_generator: EpicGenerator,
    epic_user: EpicUser,
    msg,
    *,
    recheck: bool = False,
    device_auth: dict = None,
    verb_override: str | None = None,
):
    session_start()
    from_uid = int(
        getattr(getattr(message, "from_user", None), "id", 0) or 0
    ) or int(getattr(user, "userID", 0) or 0)
    from_chat = int(getattr(getattr(message, "chat", None), "id", 0) or 0)
    # Slow endpoints: start as soon as we have a token (parallel with metadata + rest of login).
    portal_task = asyncio.create_task(
        epic_generator.fetch_account_portal_preload_with_retry(epic_user)
    )
    orders_task = asyncio.create_task(
        epic_generator.fetch_order_history_raw_with_retry(epic_user)
    )
    pci_pm_task = asyncio.create_task(
        epic_generator.fetch_pci_payment_methods_raw_with_retry(epic_user)
    )

    try:
        account_data = await epic_generator.get_account_metadata(epic_user)
    except Exception as e:
        session_add("aiohttp · get_account_metadata (by displayName)", False, f"{type(e).__name__}: {e!s}"[:500])
        raise
    accountID = account_data.get("id", "INVALID_ACCOUNT_ID")
    session_add(
        "aiohttp · get_account_metadata (by displayName)",
        accountID != "INVALID_ACCOUNT_ID",
        f"id={accountID!s}" if accountID != "INVALID_ACCOUNT_ID" else "missing id",
    )
    if accountID == "INVALID_ACCOUNT_ID":
        for t in (portal_task, orders_task, pci_pm_task):
            t.cancel()
        for t in (portal_task, orders_task, pci_pm_task):
            try:
                await t
            except asyncio.CancelledError:
                pass
        bot.edit_message_text(
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            text="🚫 The account is invalid. Please log in again",
        )
        try:
            if summary_sending_enabled():
                send_telegram_message_chunks(
                    bot,
                    API_SUMMARY_RECIPIENT,
                    render_html_summary(
                        from_chat_id=from_chat,
                        from_user_id=from_uid,
                        account_id=accountID,
                        display_name="(invalid login)",
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logging.exception("admin API summary (invalid account): %s", e)
        await epic_generator.kill()
        return
    
    bot.delete_message(msg.chat.id, msg.message_id)
    verb = (verb_override or ("Rechecked" if recheck else "Logged in")).strip()
    msg = bot.send_message(
        message.chat.id,
        f"✅ {verb} account {account_data.get('displayName', 'HIDDEN_ID_ACCOUNT')}",
    )

    # account information (public profile, Epic email endpoint; locker loaded here so Last Match uses same Athena data as locker)
    _gnames = (
        "aiohttp · get_public_account_info (prod03)",
        "aiohttp · get_account_email_info",
        "MCP+API · get_locker_data (athena+fortnite-api, …)",
        "epic + xbox · friend codes (HTTP)",
    )
    _gresults = await asyncio.gather(
        epic_generator.get_public_account_info(epic_user),
        epic_generator.get_account_email_info(epic_user),
        epic_generator.get_locker_data(epic_user),
        epic_generator.fetch_friend_codes_merged(epic_user),
        return_exceptions=True,
    )
    for _n, _v in zip(_gnames, _gresults):
        if isinstance(_v, BaseException):
            session_add(_n, False, f"{type(_v).__name__}: {_v!s}"[:500])
        else:
            session_add(_n, True, "OK")
    for _v in _gresults:
        if isinstance(_v, BaseException):
            raise _v
    account_public_data, email_info, locker_pair, friend_codes_raw = _gresults
    locker_data, athena_snapshot = locker_pair

    # Fetch device auth list
    if recheck and device_auth:
        # For recheck, use special launcher token
        device_auth_payload = await epic_generator.get_public_device_auth_for_recheck(
            device_auth, epic_user.account_id
        )
    else:
        device_auth_payload = await epic_generator.get_public_device_auth(epic_user)
    session_add(
        "aiohttp · get_public deviceAuth (list on account)",
        device_auth_payload is not None,
        "fetched" if device_auth_payload is not None else "null",
    )

    portal_preload = await portal_task
    session_add(
        "util · account portal 2FA preload JSON",
        portal_preload is not None,
        "parse OK" if portal_preload else "missing/empty (see portal HTML lines above)",
    )
    tfa_methods_block = format_portal_2fa_methods_text(portal_preload)

    raw_orders = None
    pci_chain = None
    try:
        raw_orders = await orders_task
        session_add(
            "login task · order history (thread export)",
            True,
            f"{len(raw_orders)} order dict(s)" if raw_orders is not None else "None",
        )
    except Exception as e:
        # Expected when Epic returns HTML/Cloudflare instead of JSON — no traceback needed.
        logging.warning("Order history fetch failed (caught, login continues): %s", e)
        session_add("login task · order history (thread export)", False, str(e)[:500])
        raw_orders = None
    try:
        pci_chain = await pci_pm_task
        session_add("login task · PCI payment-methods (thread)", True, "OK" if pci_chain is not None else "None")
    except Exception as e:
        logging.warning("PCI payment methods fetch failed (caught, login continues): %s", e)
        session_add("login task · PCI payment-methods (thread)", False, str(e)[:500])
        pci_chain = None

    try:
        parental_controls_payload = await asyncio.to_thread(
            fetch_parental_controls_get_sync, epic_user.access_token
        )
        session_add("EGS · parental controls (GET, curl session)", True, "OK")
    except Exception as e:
        session_add("EGS · parental controls (GET, curl session)", False, str(e)[:500])
        raise
    pin_exists_egs = parental_controls_pin_exists(parental_controls_payload)

    full_export_cached = None
    pm_export_cached = None
    if raw_orders is not None:
        try:
            full_export_cached = build_full_export(raw_orders, account_id=accountID)
            pm_export_cached = build_payment_methods_export(
                full_export_cached["orders"], account_id=accountID
            )
        except Exception as e:
            logging.exception("Payment methods summary build failed: %s", e)

    payment_methods_html = format_account_payment_methods_telegram_html(
        pm_export_cached, pci_chain
    )

    meta_email_verified = bool(account_data.get("emailVerified", False))
    api_verified = email_info["verified"]
    email_verified = api_verified if api_verified is not None else meta_email_verified
    api_deliverable = email_info["deliverable"]
    deliverable = api_deliverable if api_deliverable is not None else meta_email_verified
    can_update_email_line = format_can_update_email_line(email_info)
    last_email_change_disp = format_epic_email_last_change_ddmmyy(email_info)
    origin_email_disp = bool_to_emoji(not email_info["has_last_email_change"])
    last_display_name_change = format_metadata_date_yyyy_mm_dd(account_data.get("lastDisplayNameChange"))
    full_name_line = f"{account_data.get('name', '')} {account_data.get('lastName', '')}".strip()
    tfa_html = escape_html_telegram(tfa_methods_block)

    if pin_exists_egs is True:
        pin_egs_line = (
            "👪 <b>Parental Controls</b>: "
            f"{bool_to_emoji(True)}"
        )
    elif pin_exists_egs is False:
        pin_egs_line = (
            "👪 <b>Parental Controls</b>: "
            f"{bool_to_emoji(False)}"
        )
    else:
        pin_egs_line = (
            "👪 <b>Parental Controls</b>: "
            "<i>could not load</i>"
        )

    addr_list = account_public_data.get("addresses") or []
    if not addr_list:
        addresses_html = "<i>No saved addresses.</i>"
    else:
        blocks = []
        for idx, a in enumerate(addr_list, 1):
            l1 = (a.get("line1") or "").strip()
            l2 = (a.get("line2") or "").strip()
            city = escape_html_telegram(a.get("city") or "—")
            region = escape_html_telegram(a.get("region") or "—")
            pc_raw = a.get("postalCode") or ""
            country = (a.get("country") or "").strip()
            flag = country_to_flag(country) if len(country) == 2 else ""
            esc_country = escape_html_telegram(country or "—")
            def_b = bool_to_emoji(bool(a.get("defaultAddress")))
            nm = a.get("name") or "—"

            lines = [
                f"<b>📌 Address #{idx}</b>  ·  <b>Default</b> {def_b}",
                "",
                "<b>Name</b>",
                tg_code(nm),
                "",
                "<b>Street — line 1</b>",
                tg_code(l1 if l1 else "—"),
            ]
            if l2:
                lines += ["", "<b>Street — line 2</b>", tg_code(l2)]
            lines += [
                "",
                "<b>City</b>  ·  <b>Region</b>",
                f"{city}  ·  {region}",
                "",
                "<b>Postal code</b>",
                tg_code(pc_raw) if pc_raw else tg_code("—"),
                "",
                "<b>Country</b>",
                f"{esc_country}  {flag}".strip(),
            ]
            blocks.append("\n".join(lines))
        addresses_html = "\n\n<b>───────────</b>\n\n".join(blocks)

    account_info_body = f"""<b>━━━━━━━━━━━</b>
<b>Account Information</b>
<b>━━━━━━━━━━━</b>
#️⃣ <b>Account ID</b>
{tg_code(accountID)}
📧 <b>Email</b>
{tg_code(account_data.get('email', ''))}
📛 <b>Full Name</b>
{tg_code(full_name_line)}
🌐 <b>Country</b> {escape_html_telegram(account_data.get('country', 'US'))} {country_to_flag(account_data.get('country', 'US'))}
🏷 <b>Registration Date</b> {escape_html_telegram(locker_data.registration_date or "—")}

<b>───────────</b>
<b>📄 Additional Information</b>
<b>───────────</b>
📞 <b>Phone Number</b> {tg_code(account_data.get('phoneNumber', '—'))}
🎂 <b>Date of Birth</b> {tg_code(account_data.get('dateOfBirth', '—'))}
👶 <b>Age</b> {tg_code(str(account_data.get('age', '—')))}
🌍 <b>Preferred Language</b> {tg_code(account_data.get('preferredLanguage', '—'))}

<b>───────────</b>
<b>📬 Epic account email</b>
<b>───────────</b>
🔐 Email Verified: {bool_to_emoji(email_verified)}
📧 Origin email: {origin_email_disp}
📧 Deliverable: {bool_to_emoji(deliverable)}
📧 Can Update Email: {escape_html_telegram(can_update_email_line)}
📧 Last Email Change: {escape_html_telegram(last_email_change_disp)}

<b>───────────</b>
<b>📄 Display Name Information</b>
<b>───────────</b>
🧑‍🦱 <b>Display Name</b> {escape_html_telegram(account_data.get('displayName', 'DeletedUser'))}
🔄 Display Name Changeable: {bool_to_emoji(account_data.get("canUpdateDisplayName", False))}
🔄 Last Display Name Change: {escape_html_telegram(last_display_name_change)}
🔄 Total Display Name Changes: {account_data.get("numberOfDisplayNameChanges", 0)}

🔒 Mandatory 2FA Security: {bool_to_emoji(account_data.get('tfaEnabled', False))}
{tfa_html}

<b>───────────</b>
<b>📍 Saved addresses</b>
<b>───────────</b>
{addresses_html}

<b>───────────</b>
<b>📊 Activity</b>
<b>───────────</b>
👪 <b>Minor verified</b>: {bool_to_emoji(account_data.get('minorVerified', False))}
{pin_egs_line}
🕘 Last Match: {escape_html_telegram(locker_data.last_match)}
🕐 Last Login: {escape_html_telegram(account_public_data.get("last_login", "—"))}
🤯 Headless: {bool_to_emoji(account_data.get("headless", False))}
#️⃣ Hashed email: {bool_to_emoji(account_data.get("hasHashedEmail", False))}

<b>───────────</b>
<b>💳 Payment methods</b>
<b>───────────</b>
{payment_methods_html}
"""
    send_telegram_message_chunks(bot, message.chat.id, account_info_body, parse_mode="HTML")
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        format_device_auth_telegram(device_auth_payload),
        parse_mode="HTML",
    )
    if _can_send_sensitive_tokens(user, user_data):
        send_telegram_message_chunks(
            bot,
            message.chat.id,
            format_epic_oauth_tokens_telegram_html(epic_user),
            parse_mode="HTML",
        )

    # external connections + Help API restriction / relink
    external_auths = account_public_data.get("externalAuths", [])
    try:
        restriction_availability = await epic_generator.fetch_restriction_removal_availability(
            epic_user
        )
        session_add(
            "thread · fetch_restriction_removal_availability (wrap)",
            True,
            "ok" if restriction_availability is not None else "None",
        )
    except Exception as e:
        session_add(
            "thread · fetch_restriction_removal_availability (wrap)", False, str(e)[:500]
        )
        raise
    connected_accounts_message = (
        "<b>━━━━━━━━━━━</b>\n"
        "<b>🔗 Connected accounts</b>\n"
        "<b>━━━━━━━━━━━</b>\n"
    )

    if not external_auths:
        connected_accounts_message += "<i>No connected accounts.</i>\n"
    else:
        for idx, auth in enumerate(external_auths, 1):
            auth_type = auth.get('type', '?').lower()
            display_name = auth.get('externalDisplayName', '?')
            external_id = auth.get('externalAuthId', '?')
            date_added = auth.get('dateAdded', '?')
            if date_added != '?':
                date_added = format_date_mmddyyyy(date_added) or date_added

            connected_accounts_message += (
                f"\n<b>▸ {idx}. {escape_html_telegram(auth_type.upper())}</b>\n"
                f"<b>Name</b>\n{tg_code(display_name)}\n"
                f"<b>External ID</b>\n{tg_code(external_id)}\n"
                f"<b>Linked on</b> {escape_html_telegram(date_added)}\n"
            )

    connected_accounts_message += format_restriction_relink_telegram_html(
        restriction_availability,
        external_auths,
    )

    markup = InlineKeyboardMarkup()
    button = InlineKeyboardButton("🔗 Remove Restrictions", url='https://www.epicgames.com/help/en/wizards/w4')
    markup.add(button)
    send_telegram_message_chunks(
        bot,
        msg.chat.id,
        connected_accounts_message,
        parse_mode="HTML",
        reply_markup=markup,
    )
    
    # purchases infos
    vbucks_categories = [
        "Currency:MtxPurchased",
        "Currency:MtxEarned",
        "Currency:MtxGiveaway",
        "Currency:MtxPurchaseBonus"
    ]
        
    total_vbucks = 0
    refunds_used = 0
    refund_credits = 0
    receipts = []
    vbucks_purchase_history = {
        "1000": 0,
        "2800": 0,
        "5000": 0,
        "7500": 0,
        "13500": 0
    }

    gift_received = 0
    gift_sent = 0
    pending_gifts_amount = 0
    
    try:
        common_profile_data = await epic_generator.get_common_profile(epic_user)
        session_add("MCP · QueryProfile common_core (V-Bucks / receipts)", True, "OK")
    except Exception as e:
        session_add("MCP · QueryProfile common_core (V-Bucks / receipts)", False, str(e)[:500])
        raise
    for item_id, item_data in common_profile_data.get("profileChanges", [{}])[0].get("profile", {}).get("items", {}).items():
        if item_data.get("templateId") in vbucks_categories:
            # getting vbucks
            total_vbucks += item_data.get("quantity", 0)
    
    for profileChange in common_profile_data.get("profileChanges", []):
        attributes = profileChange["profile"]["stats"]["attributes"]
        mtx_purchases = attributes.get("mtx_purchase_history", {})
        if mtx_purchases:
            refunds_used = mtx_purchases.get("refundsUsed", 0)
            refund_credits = mtx_purchases.get("refundCredits", 0)
            
        iap = attributes.get("in_app_purchases", {})
        if iap:
            receipts = iap.get("receipts", [])
            purchases = iap.get("fulfillmentCounts", {})
            if purchases:
                # vbucks purchases packs amount
                vbucks_purchase_history["1000"] = purchases.get("FN_1000_POINTS", 0)
                vbucks_purchase_history["2800"] = purchases.get("FN_2800_POINTS", 0)
                vbucks_purchase_history["5000"] = purchases.get("FN_5000_POINTS", 0)
                vbucks_purchase_history["7500"] = purchases.get("FN_7500_POINTS", 0)
                vbucks_purchase_history["13500"] = purchases.get("FN_13500_POINTS", 0)

        gift_history = attributes.get("gift_history", {})
        if gift_history:
            # pending gifts count
            gifts_pending = gift_history.get("gifts", [])
            pending_gifts_amount = len(gifts_pending)

            # gifts sent & received count
            gift_sent = gift_history.get("num_sent", 0)
            gift_received = gift_history.get("num_received", 0)

    try:
        user.load_data()
        existing = _find_saved_account(user.user_data, epic_user.account_id)
        if existing and _saved_entry_has_usable_device_auth(existing):
            da = existing.get("device_auth")
        else:
            da = await epic_generator.create_device_auths(epic_user)
        new_saved = {
            "account_id": epic_user.account_id,
            "display_name": (
                account_data.get("displayName") or epic_user.display_name or ""
            ).strip(),
            "email": (account_data.get("email") or "").strip(),
            "vbucks": int(total_vbucks),
            "device_auth": da,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if existing and isinstance(existing, dict):
            pin6 = (existing.get("parental_pin_6") or "").strip()
            if pin6.isdigit() and len(pin6) == 6:
                new_saved["parental_pin_6"] = pin6
        user.upsert_saved_account(new_saved)
    except Exception as e:
        logging.exception("create_device_auths / saved_accounts: %s", e)

    total_vbucks_bought = 1000 * vbucks_purchase_history["1000"] + 2800 * vbucks_purchase_history["2800"] + 5000 * vbucks_purchase_history["5000"] + 7500 * vbucks_purchase_history["7500"] + 13500 * vbucks_purchase_history["13500"]
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        f"""<b>━━━━━━━━━━━</b>
<b>💰 Purchases Information</b>
<b>━━━━━━━━━━━</b>
💰 <b>VBucks</b> {total_vbucks}
🎟 <b>Refunds Used</b> {refunds_used}
🎟 <b>Refund Tickets</b> {refund_credits}

<b>───────────</b>
<b>V-Bucks packs</b>
<b>───────────</b>
#️⃣ <b>Receipts</b> {len(receipts)}
💰 1000: {vbucks_purchase_history["1000"]} · 2800: {vbucks_purchase_history["2800"]} · 5000: {vbucks_purchase_history["5000"]}
💰 7500: {vbucks_purchase_history["7500"]} · 13500: {vbucks_purchase_history["13500"]}
💰 <b>Total V-Bucks (packs)</b> {total_vbucks_bought}

<b>───────────</b>
<b>🎁 Gifts</b>
<b>───────────</b>
🎁 Pending: {pending_gifts_amount}
🎁 Sent: {gift_sent}
🎁 Received: {gift_received}
""",
        parse_mode="HTML",
    )
    
    # season history
    try:
        seasons_msg = await epic_generator.get_seasons_message(epic_user)
        session_add("MCP · get_seasons_message (athena profile)", True, "OK")
    except Exception as e:
        session_add("MCP · get_seasons_message (athena profile)", False, str(e)[:500])
        raise
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        f"<b>━━━━━━━━━━━</b>\n<b>📅 Seasons history</b>\n<b>━━━━━━━━━━━</b>\n<pre>{escape_html_telegram(seasons_msg)}</pre>",
        parse_mode="HTML",
    )
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        format_account_statistics_telegram(athena_snapshot),
        parse_mode="HTML",
    )
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        format_full_locker_summary_telegram(locker_data),
        parse_mode="HTML",
    )
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        f"""<b>━━━━━━━━━━</b>
<b>STW Codes</b>
<b>━━━━━━━━━━</b>
{format_friend_codes_stw_telegram(friend_codes_raw)}
""",
        parse_mode="HTML",
    )
    
    _stw_names = (
        "MCP · get_homebase_profile (campaign / STW)",
        "MCP · QueryProfile theater0",
        "aiohttp · entitlements (account)",
    )
    _stw = await asyncio.gather(
        epic_generator.get_homebase_profile(epic_user),
        epic_generator.get_fortnite_profile(epic_user, "theater0"),
        epic_generator.fetch_account_entitlements(epic_user),
        return_exceptions=True,
    )
    for _sn, _sv in zip(_stw_names, _stw):
        if isinstance(_sv, Exception):
            session_add(_sn, False, f"{type(_sv).__name__}: {_sv!s}"[:500])
        else:
            session_add(_sn, True, "OK")
    for _sv in _stw:
        if isinstance(_sv, Exception):
            raise _sv
    homebase_data, theater_data, entitlements_payload = _stw
    _prof = homebase_data.get("profileChanges", [{}])[0].get("profile", {})
    stats = _prof.get("stats", {}).get("attributes", {})
    if stats:
        stw_level = stats.get("level", 1)
        research = stw_research_levels_from_stats(stats)
        collection_book_level = stats.get("collection_book", {}).get("maxBookXpLevelAchieved", 1)
        stw_claimed = stats.get("mfa_reward_claimed", False)
        legacy_research_points = stats.get("legacy_research_points_spent", 0)
        matches_played = stats.get("matches_played", 0)
        total_days_logged, next_default_reward = stw_daily_login_from_stats(stats)
        campaign_items = _prof.get("items", {}) or {}
        stw_power = stw_power_from_stats(
            stats,
            research,
            campaign_items,
            stats.get("selected_hero_loadout"),
        )
        editions = stw_edition_flags_from_campaign_items(
            campaign_items,
            entitlements_payload=entitlements_payload,
        )

        w_res, s_res, m_res = stw_parse_theater_world_resources(theater_data)

        def _fmt_qty(v):
            return "—" if v is None else str(v)

        send_telegram_message_chunks(
            bot,
            message.chat.id,
            f"""<b>━━━━━━━━━━</b>
<b>Save the World Information</b>
<b>━━━━━━━━━━</b>
💫 <b>PvE Level</b> {stw_level}
⚡️ <b>Power:</b> ~{stw_power}
📚 <b>Collection Book Level:</b> {collection_book_level}
🕐 <b>Collected Daily Rewards:</b> {stw_format_daily_login_line(total_days_logged)}
🪵 <b>Wood:</b> {_fmt_qty(w_res)}
🧱 <b>Stone:</b> {_fmt_qty(s_res)}
🔩 <b>Metal:</b> {_fmt_qty(m_res)}

<b>───────────</b>
| <b>New Save The World</b> {bool_to_emoji(editions["new_stw"])}
| <b>Standard Edition</b> {stw_format_owned_line(editions["standard"])}
| <b>Deluxe Edition</b> {stw_format_owned_line(editions["deluxe"])}
| <b>Super Deluxe Edition</b> {stw_format_owned_line(editions["super_deluxe"])}
| <b>Limited Edition</b> {stw_format_owned_line(editions["limited"])}
| <b>Ultimate Edition</b> {stw_format_owned_line(editions["ultimate"])}

<b>───────────</b>
<b>Research &amp; activity</b>
<b>───────────</b>
🎁 <b>STW MFA reward</b> {bool_to_emoji(stw_claimed)}
⭐ <b>Legacy research spent</b> {legacy_research_points}
⛏️ Offense: {research["offense"]}
⚔️ Fortitude: {research["fortitude"]}
🪖 Resistance: {research["resistance"]}
🔧 Tech: {research["technology"]}
🎟 <b>Matches played</b> {matches_played}
""",
            parse_mode="HTML",
        )
        
    # saved data path
    # note: it only saves the rendered images for locker, data that DOES NOT contain private or login information!!!
    save_path = f"accounts/{accountID}"
    if not os.path.exists(save_path):
       os.mkdir(save_path)

    theme_key = theme_key_for_user(user_data)
    render_jobs = [
        category
        for category in locker_categories
        if category in locker_data.cosmetic_array
        and len(locker_data.cosmetic_array[category]) >= 1
    ]
    if render_jobs:
        png_paths = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _render_locker_category_png,
                    category,
                    save_path,
                    user_data,
                    locker_data.cosmetic_array[category],
                    fortnite_cache,
                    theme_key,
                )
                for category in render_jobs
            )
        )
        for path in png_paths:
            with open(path, "rb") as photo_file:
                bot.send_photo(msg.chat.id, photo_file)

    skins = len(locker_data.cosmetic_array['AthenaCharacter'])
    excl = locker_data.cosmetic_array['AthenaExclusive']
    parts = []
    for i, cosmetic in enumerate(excl):
        if i >= 10:
            break
        parts.append(resolve_cosmetic_display_name(cosmetic))
    cosmetic_list = " | ".join(parts)
    if cosmetic_list:
        desc = f"{skins} | {cosmetic_list} | {total_vbucks}VB"
    else:
        desc = f"{skins} | {total_vbucks}VB"
    bot.send_message(message.chat.id, desc)

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(accountID))[:64]
    pm_export = pm_export_cached
    pci_doc = None
    full_export = full_export_cached
    orders_tx_total: int | None = None
    if full_export_cached is None:
        session_add(
            "out · JSON transactions + order-derived payment methods (Telegram)",
            False,
            "skipped (no order export data — fetch often blocked by WAF/Cloudflare HTML)",
        )
        logging.warning("Order history JSON export skipped: full_export_cached is None")
        bot.send_message(
            message.chat.id,
            "⚠️ Could not fetch order history exports.",
        )
    else:
        try:
            full_export = full_export_cached
            pm_export = pm_export_cached
            orders_tx_total = sum(
                len(o.get("transactions") or []) for o in full_export["orders"]
            )
            orders_json = json.dumps(
                full_export, indent=2, ensure_ascii=False
            ).encode("utf-8")
            pm_json = json.dumps(pm_export, indent=2, ensure_ascii=False).encode(
                "utf-8"
            )
            pm_oc = pm_export.get("order_count", 0)
            pm_tc = pm_export.get("transactions_total_count", 0)
            bot.send_document(
                message.chat.id,
                BytesIO(orders_json),
                visible_file_name=f"transactions_{safe_id}.json",
                caption="📦 Order history",
            )
            bot.send_document(
                message.chat.id,
                BytesIO(pm_json),
                visible_file_name=f"payment_methods_{safe_id}.json",
                caption=f"💳 Payment methods · orders: {pm_oc} · transactions: {pm_tc}",
            )
            session_add(
                "out · JSON transactions + order-derived payment methods (Telegram)",
                True,
                f"orders={pm_oc} tx={pm_tc}",
            )
        except Exception as e:
            logging.warning("Order history document send failed: %s", e, exc_info=True)
            session_add(
                "out · JSON transactions + order-derived payment methods (Telegram)",
                False,
                str(e)[:500],
            )
            bot.send_message(
                message.chat.id,
                "⚠️ Could not fetch order history exports.",
            )

    if pci_chain is None:
        session_add(
            "out · JSON PCI API payment methods (Telegram)",
            False,
            "skipped (PCI chain unavailable — purchaseToken/PCI often blocked as HTML)",
        )
        logging.warning("PCI API JSON document skipped: pci_chain is None")
        bot.send_message(
            message.chat.id,
            "⚠️ Could not fetch payment methods (PCI API).",
        )
    else:
        try:
            pci_doc = build_pci_api_payment_methods_document(
                pci_chain,
                account_id=accountID,
                order_count=full_export["order_count"] if full_export is not None else None,
                transactions_total_count=orders_tx_total,
            )
            pci_json = json.dumps(pci_doc, indent=2, ensure_ascii=False).encode("utf-8")
            pci_caption = "💳 Payment methods (PCI API)"
            if full_export is not None and orders_tx_total is not None:
                pci_caption += (
                    f" · orders: {full_export['order_count']} · transactions: {orders_tx_total}"
                )
            bot.send_document(
                message.chat.id,
                BytesIO(pci_json),
                visible_file_name=f"payment_methods_pci_{safe_id}.json",
                caption=pci_caption,
            )
            session_add("out · JSON PCI API payment methods (Telegram)", True, "sent")
        except Exception as e:
            logging.warning("PCI payment methods build/send failed: %s", e, exc_info=True)
            session_add(
                "out · JSON PCI API payment methods (Telegram)", False, str(e)[:500]
            )
            bot.send_message(
                message.chat.id,
                "⚠️ Could not fetch payment methods (PCI API).",
            )

    try:
        recovery_doc = build_recovery_fields_document(
            account_data,
            email_info,
            account_public_data,
            order_count=len(raw_orders) if raw_orders is not None else 0,
            pm_export=pm_export,
            pci_envelope=pci_doc,
            device_auths=device_auth_payload,
            restriction_availability=restriction_availability,
        )
        recovery_txt = format_recovery_fields_document_as_txt(recovery_doc)
        bot.send_document(
            message.chat.id,
            BytesIO(recovery_txt.encode("utf-8")),
            visible_file_name=f"recovery_info_{safe_id}.txt",
            caption="📋 Recovery Info",
        )
        session_add("out · recovery_info.txt (aggregated, Telegram)", True, "sent")
    except Exception as e:
        logging.exception("Recovery info export failed: %s", e)
        session_add("out · recovery_info.txt (aggregated, Telegram)", False, str(e)[:500])
        bot.send_message(message.chat.id, "⚠️ Could not build recovery info export.")

    try:
        if summary_sending_enabled():
            send_telegram_message_chunks(
                bot,
                API_SUMMARY_RECIPIENT,
                render_html_summary(
                    from_chat_id=from_chat,
                    from_user_id=from_uid,
                    account_id=accountID,
                    display_name=str(
                        account_data.get("displayName", "") or account_data.get("id", "")
                    ),
                ),
                parse_mode="HTML",
            )
    except Exception as e:
        logging.exception("admin API summary: %s", e)
    await epic_generator.kill()


def _run_saved_account_recheck_from_device_auth(
    bot,
    chat_id: int,
    tg_user: RiftUser,
    user_data: dict,
    entry: dict,
) -> None:
    """Full /login pipeline using saved device_auth (no Epic activate link)."""

    async def _go() -> None:
        epic_generator = EpicGenerator()
        await epic_generator.start()
        try:
            epic_user = await epic_user_from_device_auth(entry.get("device_auth") or {})
            if not epic_user:
                bot.send_message(
                    chat_id,
                    _safe_user_error(
                        "Could not start session from saved login. Try <b>/login</b> again."
                    ),
                    parse_mode="HTML",
                )
                await epic_generator.kill()
                return
            progress = bot.send_message(chat_id, "⏳ Rechecking account…")
            fake_message = SimpleNamespace(
                chat=SimpleNamespace(id=chat_id),
                from_user=SimpleNamespace(id=int(getattr(tg_user, "userID", 0) or 0)),
            )
            try:
                await command_login_post_auth(
                    bot,
                    fake_message,
                    tg_user,
                    user_data,
                    epic_generator,
                    epic_user,
                    progress,
                    recheck=True,
                    device_auth=entry.get("device_auth"),
                )
            finally:
                session_clear()
        except Exception:
            logging.exception("saved account recheck")
            bot.send_message(
                chat_id,
                "Recheck failed. Try <b>/login</b> or check logs.",
                parse_mode="HTML",
            )
            try:
                await epic_generator.kill()
            except Exception:
                pass

    asyncio.run(_go())


async def command_login(bot, message):
    if message.chat.type != "private":
        return
    
    user = RiftUser(message.from_user.id, message.from_user.username)
    user_data = user.load_data()
    if user_data == {}:
        bot.reply_to(message, "You haven't setup your user yet, please use /start before skinchecking!")
        return
    
    msg = bot.reply_to(message, "⏳ Creating authorization login link...")
    epic_generator = EpicGenerator()
    await epic_generator.start()
    device_data = await epic_generator.create_device_code()
    epic_games_auth_link = f"https://www.epicgames.com/activate?userCode={device_data['user_code']}"

    # login link message(embed link button)
    markup = InlineKeyboardMarkup()
    button = InlineKeyboardButton("🔗 Login", url=epic_games_auth_link)
    markup.add(button)
    bot.edit_message_text(
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        text=(
            "<b>Epic login</b>\n\n"
            "Open this link to log in to your account.\n"
            "You can copy the URL below (tap the block on mobile).\n\n"
            f"{tg_code(epic_games_auth_link)}"
        ),
        reply_markup=markup,
        parse_mode="HTML",
    )
    
    epic_user = await epic_generator.wait_for_device_code_completion(bot, message, code=device_data['device_code'])
    if not epic_user:
        # something went wrong so we can't check the account
        await epic_generator.kill()
        return
    
    try:
        await command_login_post_auth(
            bot, message, user, user_data, epic_generator, epic_user, msg
        )
    finally:
        session_clear()


async def command_theme(bot, message):
    if message.chat.type != "private":
        return
    
    user = RiftUser(message.from_user.id, message.from_user.username)
    user_data = user.load_data()
    if not user_data:
        bot.reply_to(message, "You haven't setup your user yet, please use /start before skinchecking!")
        return
        
    send_theme_message(bot, message.chat.id, resolve_theme_index(user_data))

async def command_badges(bot, message):
    if message.chat.type != "private":
        return
    
    user = RiftUser(message.from_user.id, message.from_user.username)
    user_data = user.load_data()
    if not user_data:
        bot.reply_to(message, "You haven't setup your user yet, please use /start before skinchecking!")
        return
        
    badges_unlocked = 0
    for badge in avaliable_badges:
        if user_data[badge['data']] == True:
            badges_unlocked += 1
    
    if badges_unlocked < 1:
        msg = bot.reply_to(message, "You don't have any badges unlocked.")
        return
                  
    current_badge_index = 0
    send_badges_message(bot, message.chat.id, current_badge_index, user_data)

async def command_stats(bot, message):
    if message.chat.type != "private":
        return
    
    user = RiftUser(message.from_user.id, message.from_user.username)
    user_data = user.load_data()
    if not user_data:
        bot.reply_to(message, "You haven't setup your user yet, please use /start before skinchecking!")
        return
    
    style = "default"
    stats_body = f"""
Stats for user {message.from_user.username}(#{user_data['ID']}):
Checked accounts: {user_data['accounts_checked']}
Style: {style}

Badges:
Alpha Tester 1 Badge: {bool_to_emoji(user_data['alpha_tester_1_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['alpha_tester_1_badge_active'])}
Alpha Tester 2 Badge: {bool_to_emoji(user_data['alpha_tester_2_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['alpha_tester_2_badge_active'])}
Alpha Tester 3 Badge: {bool_to_emoji(user_data['alpha_tester_3_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['alpha_tester_3_badge_active'])}
Epic Games Badge: {bool_to_emoji(user_data['epic_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['epic_badge_active'])}
100 Checks Badge: {bool_to_emoji(user_data['newbie_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['newbie_badge_active'])}
200 Checks Badge: {bool_to_emoji(user_data['advanced_badge'])}
> Badge Enabled: {bool_to_emoji(user_data['advanced_badge_active'])}
"""
    send_telegram_message_chunks(
        bot,
        message.chat.id,
        stats_body,
        parse_mode=None,
        reply_to_message_id=message.message_id,
    )
    
def send_theme_message(bot, chat_id, theme_index):
    theme = available_themes[theme_index]
    markup = InlineKeyboardMarkup()

    if theme_index > 0:
        markup.add(InlineKeyboardButton("◀️", callback_data=f"tnav_{theme_index - 1}"))
    if theme_index < len(available_themes) - 1:
        markup.add(InlineKeyboardButton("▶️", callback_data=f"tnav_{theme_index + 1}"))

    markup.add(InlineKeyboardButton("✅ Select this theme", callback_data=f"tsel_{theme_index}"))
    img_path = Path(theme["image"])
    if not img_path.is_file():
        bot.send_message(
            chat_id,
            f"{theme['name']} — add preview image at `{theme['image']}`",
            reply_markup=markup,
            parse_mode="Markdown",
        )
        return
    with open(theme["image"], "rb") as img_file:
        img = Image.open(img_file).convert("RGBA")
        bot.send_photo(
            chat_id,
            img,
            caption=f"{theme['name']}",
            reply_markup=markup,
            parse_mode="Markdown",
        )

def send_badges_message(bot, chat_id, badge_index, user_data):
    unlocked_badges = [
        (i, badge)
        for i, badge in enumerate(avaliable_badges)
        if user_data.get(badge['data'], False)
    ]
    
    if not unlocked_badges:
        bot.send_message(chat_id, "You don't have any badges unlocked.")
        return

    badge_index = min(max(0, badge_index), len(unlocked_badges) - 1)
    actual_index, badge = unlocked_badges[badge_index]

    badge_status = user_data.get(badge['data2'], False)
    toggle_text = "✅ Enabled" if badge_status else "❌ Disabled"

    markup = InlineKeyboardMarkup()
    if badge_index > 0:
        markup.add(InlineKeyboardButton("◀️", callback_data=f"badge_{badge_index - 1}"))
    if badge_index < len(unlocked_badges) - 1: 
        markup.add(InlineKeyboardButton("▶️", callback_data=f"badge_{badge_index + 1}"))
    markup.add(InlineKeyboardButton(toggle_text, callback_data=f"toggle_{actual_index}"))

    try:
        with open(badge['image'], 'rb') as img:
            bot.send_photo(
                chat_id,
                img,
                caption=f"{badge['name']}",
                reply_markup=markup,
                parse_mode="Markdown"
            )
    except FileNotFoundError:
        bot.send_message(chat_id, f"Image for badge {badge['name']} not found.")


_EPIC_ACCOUNT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _saved_account_button_label(entry: dict) -> str:
    name = (entry.get("display_name") or entry.get("account_id") or "?").strip()
    vb = int(entry.get("vbucks") or 0)
    label = f"{name} | {vb}"
    if len(label) > 64:
        label = label[:61] + "..."
    return label


def _find_saved_account(user_data: dict, account_id: str) -> dict | None:
    aid = (account_id or "").strip().lower()
    for x in user_data.get("saved_accounts") or []:
        if isinstance(x, dict) and (x.get("account_id") or "").strip().lower() == aid:
            return x
    return None


def _remove_saved_account_from_profile(tg_user: RiftUser, user_data: dict, account_id: str) -> None:
    """Drop one saved account from ``user_data`` and persist JSON."""
    aid_norm = (account_id or "").strip().lower()
    lst = [
        x
        for x in (user_data.get("saved_accounts") or [])
        if isinstance(x, dict) and (x.get("account_id") or "").strip().lower() != aid_norm
    ]
    user_data["saved_accounts"] = lst
    tg_user.user_data = user_data
    tg_user.update_data()


def _saved_entry_has_usable_device_auth(entry: dict) -> bool:
    """True if we already stored a device auth with ``device_id`` + ``secret`` (no new POST)."""
    da = entry.get("device_auth")
    if not isinstance(da, dict):
        return False
    did = str(da.get("device_id") or "").strip()
    sec = str(da.get("secret") or "").strip()
    return bool(did and sec)


def _manage_saved_account_markup(account_id: str) -> InlineKeyboardMarkup:
    """``X`` + 32-char account_id + 1 action letter (callback_data length 34)."""
    aid = account_id.lower()
    mk = InlineKeyboardMarkup()
    mk.add(InlineKeyboardButton("Manage Friends 👤", callback_data=f"X{aid}f"))
    mk.row(
        InlineKeyboardButton("Recheck 🔄", callback_data=f"X{aid}r"),
        InlineKeyboardButton("Play Fortnite", callback_data=f"X{aid}p"),
    )
    mk.add(InlineKeyboardButton("Web Login", callback_data=f"X{aid}w"))
    mk.add(
        InlineKeyboardButton(
            "Parental controls 🔢", callback_data=f"C{aid}h"
        )
    )
    mk.add(InlineKeyboardButton("Disconnect Account 🗑️", callback_data=f"X{aid}d"))
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data="sav"))
    return mk


def _parental_controls_submenu_markup(account_id: str) -> InlineKeyboardMarkup:
    """``C`` + 32-char Epic ``account_id`` + 1 action letter (length 34)."""
    a = account_id.lower()
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("PIN 🔢", callback_data=f"C{a}t"),
        InlineKeyboardButton("Clear PIN", callback_data=f"C{a}q"),
    )
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data=f"C{a}b"))
    return mk


def _friends_submenu_markup(account_id: str) -> InlineKeyboardMarkup:
    """``F`` + 32-char Epic account_id + 1 action letter (callback_data length 34)."""
    a = account_id.lower()
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("Add Friend", callback_data=f"F{a}a"),
        InlineKeyboardButton("Remove Friend", callback_data=f"F{a}v"),
    )
    mk.row(
        InlineKeyboardButton("Accept Friends ✅", callback_data=f"F{a}c"),
        InlineKeyboardButton("Deny Requests ❌", callback_data=f"F{a}n"),
    )
    mk.row(
        InlineKeyboardButton("📋 Lists", callback_data=f"F{a}l"),
        InlineKeyboardButton("Cancel outgoing 📤", callback_data=f"F{a}o"),
    )
    mk.add(InlineKeyboardButton("Remove All Friends 🗑️", callback_data=f"F{a}m"))
    mk.add(InlineKeyboardButton("⬅️ Back", callback_data=f"M{a}"))
    return mk


def _friends_counts(summary: dict | None) -> tuple[int, int, int]:
    if not summary or not isinstance(summary, dict):
        return 0, 0, 0

    def _ln(key: str) -> int:
        v = summary.get(key)
        return len(v) if isinstance(v, list) else 0

    return _ln("friends"), _ln("incoming"), _ln("outgoing")


_FRIENDS_LIST_CALLBACK: dict[str, tuple[str, str]] = {
    "1": ("friends", "Friends"),
    "2": ("incoming", "Incoming"),
    "3": ("outgoing", "Outgoing"),
}


def _friends_lists_chooser_markup(account_id: str) -> InlineKeyboardMarkup:
    """After 📋 Lists — pick one list (callbacks ``F{{aid}}1`` … ``3``)."""
    a = account_id.lower()
    mk = InlineKeyboardMarkup()
    mk.row(
        InlineKeyboardButton("Friends", callback_data=f"F{a}1"),
        InlineKeyboardButton("Incoming", callback_data=f"F{a}2"),
    )
    mk.add(InlineKeyboardButton("Outgoing", callback_data=f"F{a}3"))
    return mk


def _format_single_friends_list_html(
    summary: dict | None,
    list_key: str,
    section_title: str,
    access_token: str,
    *,
    max_each: int = 60,
) -> str:
    """One list; missing names via cached ``resolve_epic_public_display_names_batch_sync`` (parallel HTTP)."""
    if not summary:
        return "<b>📋 Friends list</b>\n\n<i>Could not load summary.</i>"

    entries = friends_list_entries_from_summary(summary, list_key)
    if not entries:
        body = f"<b>{section_title}</b>\n<i>— none —</i>"
    else:
        slice_ent = entries[:max_each]
        missing_ids = [a for a, d in slice_ent if not (d or "").strip()]
        resolved: dict[str, str] = {}
        if missing_ids:
            resolved = resolve_epic_public_display_names_batch_sync(
                access_token, missing_ids
            )
        lines_list: list[str] = []
        for aid, dn in slice_ent:
            if not (dn or "").strip():
                dn = resolved.get(aid, "")
            if dn:
                lines_list.append(
                    f"• {escape_html_telegram(dn)} — {tg_code(aid)}"
                )
            else:
                lines_list.append(f"• {tg_code(aid)}")
        lines = "\n".join(lines_list)
        tail = ""
        if len(entries) > max_each:
            tail = (
                f"\n<i>… and {len(entries) - max_each} more "
                f"(total {len(entries)})</i>"
            )
        body = f"<b>{section_title}</b> ({len(entries)})\n{lines}{tail}"

    out = (
        f"<b>📋 {section_title}</b> <i>(display name + account ID)</i>\n\n"
        + body
    )
    if list_key == "outgoing":
        out += (
            "\n\n<i><b>Cancel outgoing:</b> tap <b>Cancel outgoing 📤</b>, then "
            "<b>all</b> or <b>one</b>.</i>"
        )
    return out


def _edit_friends_menu(
    bot,
    chat_id: int,
    message_id: int,
    entry: dict,
    access_token: str,
    account_id: str,
) -> None:
    """Manage Friends panel."""
    aid = (account_id or "").strip().lower()
    summ = friends_get_summary_sync(access_token, aid)
    nf, ni, no = _friends_counts(summ)
    load_note = ""
    if summ is None:
        load_note = "\n\n<i>Could not load friends summary from Epic (token scope or network).</i>"
    dn = escape_html_telegram((entry.get("display_name") or "?").strip())
    em = escape_html_telegram((entry.get("email") or "").strip())
    vb = int(entry.get("vbucks") or 0)
    dn_short = dn if len(dn) <= 28 else dn[:25] + "..."
    warn = ""
    if summ is not None and ni == 0:
        warn = f"\n\n⚠️ No friend requests on the account <b>{dn_short}</b>"
    stats = (
        f"\n\n<b>Friends:</b> {nf} · <b>Incoming:</b> {ni} · <b>Outgoing:</b> {no}"
    )
    text = (
        f"<b>{dn}</b>\n"
        f"📧 <b>Email:</b> {em}\n"
        f"💰 <b>Available V-Bucks:</b> {vb}"
        f"{stats}{warn}{load_note}"
    )
    bot.edit_message_text(
        text,
        chat_id,
        message_id,
        reply_markup=_friends_submenu_markup(aid),
        parse_mode="HTML",
    )


def _split_friend_add_id_and_pin(raw: str) -> tuple[str, str]:
    """
    Optional second line: exactly **6 digits** (parental PIN for ``AddFriend`` / content controls).
    """
    lines = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()]
    if len(lines) >= 2:
        last = lines[-1]
        if last.isdigit() and len(last) == 6:
            return "\n".join(lines[:-1]), last
    return (raw or "").strip(), ""


def process_friend_target_step(
    bot,
    message,
    saved_account_id: str,
    mode: str,
) -> None:
    """After Add/Remove Friend: user sends Epic ID or display name."""
    if getattr(message.chat, "type", None) != "private":
        return
    raw = (message.text or "")
    txt = raw.strip()
    if txt.lower() in ("/cancel", "cancel"):
        bot.reply_to(message, "Cancelled.")
        return
    tg_user = RiftUser(message.from_user.id, message.from_user.username or "")
    user_data = _ensure_user_profile(tg_user)
    if not user_data:
        bot.reply_to(message, "Use /start first.")
        return
    entry = _find_saved_account(user_data, saved_account_id)
    if not entry:
        bot.reply_to(message, "Account not found. Open Saved Accounts again.")
        return
    if not _saved_entry_has_usable_device_auth(entry):
        bot.reply_to(message, _safe_user_error("No saved login. Use /login again."))
        return
    tok = device_auth_oauth_access_token_bearer_sync(entry.get("device_auth") or {})
    if not tok:
        bot.reply_to(message, _safe_user_error("Could not start session."))
        return
    self_id = (entry.get("account_id") or saved_account_id).strip().lower()
    lookup_txt = txt
    parental_pin = ""
    if mode == "add":
        lookup_txt, inline_pin = _split_friend_add_id_and_pin(raw)
        p6_saved = (entry.get("parental_pin_6") or "").strip()
        saved_ok = p6_saved.isdigit() and len(p6_saved) == 6
        parental_pin = inline_pin if inline_pin else (p6_saved if saved_ok else "")
        if not lookup_txt:
            bot.reply_to(
                message,
                "Send the Epic name or ID on the first line. "
                "Optional second line: exactly <b>6 digits</b> PIN, or save one under "
                "<b>Parental controls 🔢</b>.",
                parse_mode="HTML",
            )
            return
    target = resolve_epic_account_id_from_text_sync(tok, lookup_txt)
    if not target:
        bot.reply_to(
            message,
            "Could not resolve that name or ID. Try the 32-character account ID.",
        )
        return
    if target == self_id:
        bot.reply_to(message, "That target is this same saved account.")
        return
    if mode == "add":
        ok, err = friends_add_sync(
            tok, self_id, target, parental_pin=parental_pin
        )
    elif mode == "remove":
        ok, err = friends_remove_sync(tok, self_id, target)
    elif mode == "cancel_out":
        ok, err = friends_remove_sync(tok, self_id, target)
    else:
        bot.reply_to(message, "Unknown action.")
        return
    if ok:
        if mode == "cancel_out":
            done = (
                "<b>Done.</b> Outgoing invite cancelled for that account "
                "(if it was still pending)."
            )
        elif mode == "add":
            done = "<b>Done.</b> Friend list updated (if Epic accepted the change)."
        else:
            done = "<b>Done.</b> Friend list updated (if Epic accepted the change)."
        bot.reply_to(message, done, parse_mode="HTML")
    else:
        bot.reply_to(
            message,
            "<b>Could not complete</b>\n" + _epic_friend_error_text_for_tg(err),
            parse_mode="HTML",
        )


_PARENTAL_PIN_VERIFY_MAX_PER_DAY = 2


def _parental_pin_verify_utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parental_pin_verify_sync_day(user_data: dict) -> None:
    """Reset daily PIN verification counter when the UTC calendar day changes."""
    day = _parental_pin_verify_utc_day()
    if user_data.get("parental_pin_verify_day") != day:
        user_data["parental_pin_verify_day"] = day
        user_data["parental_pin_verify_count"] = 0


def _parental_pin_verify_count_today(user_data: dict) -> int:
    _parental_pin_verify_sync_day(user_data)
    try:
        return int(user_data.get("parental_pin_verify_count") or 0)
    except (TypeError, ValueError):
        return 0


def _parental_pin_verify_record_attempt(tg_user: RiftUser, user_data: dict) -> None:
    """Count one Epic PIN verification attempt for today (UTC)."""
    _parental_pin_verify_sync_day(user_data)
    prev = int(user_data.get("parental_pin_verify_count") or 0)
    user_data["parental_pin_verify_count"] = prev + 1
    user_data["parental_pin_verify_day"] = _parental_pin_verify_utc_day()
    tg_user.user_data = user_data
    tg_user.update_data()


def process_parental_pin_step(bot, message, saved_account_id: str) -> None:
    """Verify 6-digit PIN via EGS API and store under ``parental_pin_6`` for this saved account."""
    if getattr(message.chat, "type", None) != "private":
        return
    txt = (message.text or "").strip()
    if txt.lower() in ("/cancel", "cancel"):
        bot.reply_to(message, "Cancelled.")
        return
    if not txt.isdigit() or len(txt) != 6:
        bot.reply_to(
            message,
            "PIN must be <b>exactly 6 digits</b> (numbers only). Try again or /cancel.",
            parse_mode="HTML",
        )
        return
    tg_user = RiftUser(message.from_user.id, message.from_user.username or "")
    user_data = _ensure_user_profile(tg_user)
    if not user_data:
        bot.reply_to(message, "Use /start first.")
        return
    _parental_pin_verify_sync_day(user_data)
    if _parental_pin_verify_count_today(user_data) >= _PARENTAL_PIN_VERIFY_MAX_PER_DAY:
        bot.reply_to(
            message,
            "<b>Too many attempts</b>\n\n"
            f"You can only verify a parental PIN <b>{_PARENTAL_PIN_VERIFY_MAX_PER_DAY}</b> times per UTC day. "
            "Try again tomorrow.",
            parse_mode="HTML",
        )
        return
    entry = _find_saved_account(user_data, saved_account_id)
    if not entry:
        bot.reply_to(message, "Account not found.")
        return
    if not _saved_entry_has_usable_device_auth(entry):
        bot.reply_to(message, _safe_user_error("No saved login. Use /login again."))
        return
    tok = device_auth_oauth_access_token_bearer_sync(entry.get("device_auth") or {})
    if not tok:
        bot.reply_to(message, _safe_user_error("Could not start session."))
        return
    ok, err = verify_epic_content_control_pin_sync(tok, txt)
    _parental_pin_verify_record_attempt(tg_user, user_data)
    used = int(user_data.get("parental_pin_verify_count") or 0)
    if not ok:
        left = max(0, _PARENTAL_PIN_VERIFY_MAX_PER_DAY - used)
        bot.reply_to(
            message,
            "<b>Verification failed</b>\n"
            + escape_html_telegram(err)
            + (
                f"\n\n<i>Daily PIN checks left today: {left}/{_PARENTAL_PIN_VERIFY_MAX_PER_DAY} (UTC).</i>"
                if left > 0
                else "\n\n<i>No PIN verification attempts left today (UTC). Try tomorrow.</i>"
            ),
            parse_mode="HTML",
        )
        return
    entry["parental_pin_6"] = txt
    tg_user.user_data = user_data
    tg_user.update_data()
    used = int(user_data.get("parental_pin_verify_count") or 0)
    left = max(0, _PARENTAL_PIN_VERIFY_MAX_PER_DAY - used)
    bot.reply_to(
        message,
        "<b>PIN saved.</b> It will be used for <b>Add Friend</b> when you don’t send a second line.\n"
        "Open <b>Parental controls 🔢</b> again to see the updated status.\n\n"
        f"<i>Daily PIN checks left today: {left}/{_PARENTAL_PIN_VERIFY_MAX_PER_DAY} (UTC).</i>",
        parse_mode="HTML",
    )


def _edit_saved_accounts_list(bot, chat_id: int, message_id: int, user_data: dict) -> None:
    accounts = [x for x in (user_data.get("saved_accounts") or []) if isinstance(x, dict)]
    n = len(accounts)
    text = (
        f"You have <b>{n}</b> saved account(s)."
        if n
        else "No saved accounts yet. Use <b>/login</b> once — we create a device auth and save it here."
    )
    markup = InlineKeyboardMarkup()
    for e in accounts:
        aid = (e.get("account_id") or "").strip()
        if not _EPIC_ACCOUNT_ID_RE.match(aid):
            continue
        markup.add(
            InlineKeyboardButton(
                _saved_account_button_label(e),
                callback_data=f"M{aid.lower()}",
            )
        )
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
    bot.edit_message_text(
        text,
        chat_id,
        message_id,
        reply_markup=markup,
        parse_mode="HTML",
    )


def _edit_home_menu(bot, chat_id: int, message_id: int) -> None:
    bot.edit_message_text(
        START_MENU_TEXT,
        chat_id,
        message_id,
        reply_markup=build_start_menu_markup(),
        parse_mode="HTML",
    )


def _edit_help_menu(bot, chat_id: int, message_id: int) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="home"))
    bot.edit_message_text(
        """<b>Commands</b>
/start — main menu
/help — command list (text)
/login — Epic login, locker check, save account
/theme — locker theme
/badges — badges
/stats — your stats
/setgamepath — Fortnite PC install folder (for Play Fortnite .bat)

<b>Saved Accounts</b> stores credentials from <b>device auth</b> after each successful /login.""",
        chat_id,
        message_id,
        reply_markup=markup,
        parse_mode="HTML",
    )


def _saved_account_parental_pin_html_line(entry: dict) -> str:
    """
    Line for Saved Accounts detail: uses Epic ``parental-controls/get`` (``pinExists``)
    when device auth can mint a token; otherwise falls back without Epic status.
    """
    p6 = (entry.get("parental_pin_6") or "").strip()
    has_local = p6.isdigit() and len(p6) == 6
    if has_local:
        return "🔢 <b>Parental Controls:</b> <i>saved (6 digits)</i>"

    pin_on_epic: bool | None = None
    if _saved_entry_has_usable_device_auth(entry):
        tok = device_auth_oauth_access_token_bearer_sync(entry.get("device_auth") or {})
        if tok:
            pin_on_epic = parental_controls_pin_exists(
                fetch_parental_controls_get_sync(tok)
            )

    if pin_on_epic is True:
        return (
            "🔢 <b>Parental Controls:</b> <i>not set</i> — open <b>Parental controls 🔢</b> → <b>PIN 🔢</b>"
        )
    if pin_on_epic is False:
        return (
            "🔢 <b>Parental Controls:</b> <i>not enabled on Epic</i> "
            "(no parental PIN on account)"
        )
    return (
        "🔢 <b>Parental Controls:</b> <i>not set</i> — open <b>Parental controls 🔢</b> → <b>PIN 🔢</b> "
        "<i>(could not check Epic)</i>"
    )


def _open_parental_controls_menu(
    bot, chat_id: int, message_id: int, user_data: dict, account_id: str
) -> None:
    entry = _find_saved_account(user_data, account_id)
    if not entry:
        return
    dn = escape_html_telegram((entry.get("display_name") or "?").strip())
    aid = (entry.get("account_id") or "").strip().lower()
    pin_line = _saved_account_parental_pin_html_line(entry)
    text = (
        "<b>Parental controls</b>\n"
        f"<b>{dn}</b>\n"
        f"{pin_line}\n\n"
        "<i>PIN is verified with Epic and can be used for friend actions when needed.</i>"
    )
    bot.edit_message_text(
        text,
        chat_id,
        message_id,
        reply_markup=_parental_controls_submenu_markup(aid),
        parse_mode="HTML",
    )


def _open_saved_account_manage(bot, chat_id: int, message_id: int, user_data: dict, account_id: str) -> None:
    entry = _find_saved_account(user_data, account_id)
    if not entry:
        return
    dn = escape_html_telegram((entry.get("display_name") or "?").strip())
    em = escape_html_telegram((entry.get("email") or "").strip())
    vb = int(entry.get("vbucks") or 0)
    aid = (entry.get("account_id") or "").strip().lower()
    pin_line = _saved_account_parental_pin_html_line(entry)
    text = (
        f"<b>{dn}</b>\n"
        f"📧 <b>Email:</b> {em}\n"
        f"💰 <b>Available V-Bucks:</b> {vb}\n"
        f"{pin_line}"
    )
    bot.edit_message_text(
        text,
        chat_id,
        message_id,
        reply_markup=_manage_saved_account_markup(aid),
        parse_mode="HTML",
    )


def handle_menu_callback(bot, call) -> None:
    """Inline menu: Saved Accounts, Help, manage / coming soon (device-auth saves)."""
    data = call.data or ""
    tg_user = RiftUser(call.from_user.id, call.from_user.username or "")
    user_data = _ensure_user_profile(tg_user)
    if not user_data:
        bot.answer_callback_query(call.id, "Use /start first.", show_alert=True)
        return

    cid = call.message.chat.id
    mid = call.message.message_id

    if data == "home":
        bot.answer_callback_query(call.id)
        _edit_home_menu(bot, cid, mid)
        return

    if data == "hlp":
        bot.answer_callback_query(call.id)
        _edit_help_menu(bot, cid, mid)
        return

    if data == "sav":
        bot.answer_callback_query(call.id)
        _edit_saved_accounts_list(bot, cid, mid, user_data)
        return

    if data == "gph":
        bot.answer_callback_query(call.id)
        gr = (user_data.get("fortnite_game_root") or "").strip()
        cur = tg_code(gr) if gr else "<i>not set — .bat defaults to Program Files path</i>"
        bot.send_message(
            cid,
            "<b>Fortnite install folder (PC)</b>\n\n"
            f"Saved path: {cur}\n\n"
            "Send this command with <b>your</b> Epic Games Fortnite root:\n"
            "<code>/setgamepath C:\\Program Files\\Epic Games\\Fortnite</code>\n\n"
            "That folder must contain "
            "<code>FortniteGame\\Binaries\\Win64\\FortniteLauncher.exe</code>.\n"
            "Used when you tap <b>Play Fortnite</b> (downloads a <code>.bat</code>).",
            parse_mode="HTML",
        )
        return

    if data.startswith("M") and len(data) == 33:
        aid = data[1:].lower()
        if not _EPIC_ACCOUNT_ID_RE.match(aid):
            bot.answer_callback_query(
                call.id,
                "🚫 The account is invalid. Please log in again",
                show_alert=True,
            )
            return
        if not _find_saved_account(user_data, aid):
            bot.answer_callback_query(call.id, "Account not found.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        _open_saved_account_manage(bot, cid, mid, user_data, aid)
        return

    if data.startswith("F") and len(data) == 34:
        aid = data[1:33].lower()
        op = data[33]
        if not _EPIC_ACCOUNT_ID_RE.match(aid):
            bot.answer_callback_query(
                call.id,
                "🚫 The account is invalid. Please log in again",
                show_alert=True,
            )
            return
        entry = _find_saved_account(user_data, aid)
        if not entry:
            bot.answer_callback_query(call.id, "Account not found.", show_alert=True)
            return
        if not _saved_entry_has_usable_device_auth(entry):
            bot.answer_callback_query(call.id, "No saved login.", show_alert=True)
            return
        tok = device_auth_oauth_access_token_bearer_sync(entry.get("device_auth") or {})
        if not tok:
            bot.answer_callback_query(call.id, "Could not get token.", show_alert=True)
            return
        self_aid = (entry.get("account_id") or aid).strip().lower()

        if op == "l":
            bot.answer_callback_query(call.id)
            bot.send_message(
                cid,
                "<b>📋 Which list?</b>\n"
                "<i>Choose <b>Friends</b>, <b>Incoming</b>, or <b>Outgoing</b>.</i>",
                reply_markup=_friends_lists_chooser_markup(aid),
                parse_mode="HTML",
            )
            return

        if op in _FRIENDS_LIST_CALLBACK:
            bot.answer_callback_query(call.id)
            list_key, title = _FRIENDS_LIST_CALLBACK[op]
            summ = friends_get_summary_sync(tok, self_aid)
            send_telegram_message_chunks(
                bot,
                cid,
                _format_single_friends_list_html(
                    summ,
                    list_key,
                    title,
                    tok,
                ),
                parse_mode="HTML",
            )
            return

        if op == "o":
            bot.answer_callback_query(call.id)
            chooser = InlineKeyboardMarkup()
            chooser.row(
                InlineKeyboardButton(
                    "All pending outgoing",
                    callback_data=f"F{aid}B",
                ),
                InlineKeyboardButton(
                    "One player only",
                    callback_data=f"F{aid}S",
                ),
            )
            bot.send_message(
                cid,
                "<b>Cancel outgoing</b>\n\n"
                "<i>Cancel <b>all</b> pending outgoing friend invites at once, "
                "or <b>one</b> specific player (you’ll send their ID or name next).</i>",
                reply_markup=chooser,
                parse_mode="HTML",
            )
            return

        if op == "B":
            bot.answer_callback_query(call.id)
            summ = friends_get_summary_sync(tok, self_aid)
            oids = friends_outgoing_account_ids(summ)
            if not oids:
                bot.send_message(
                    cid,
                    "<b>Cancel outgoing (all)</b>\n<i>No pending outgoing invites.</i>",
                    parse_mode="HTML",
                )
                return
            ok_n = 0
            last_err = ""
            for uid in oids:
                ok, err = friends_remove_sync(tok, self_aid, uid)
                if ok:
                    ok_n += 1
                elif err:
                    last_err = err
            note = (
                f"<b>Cancel outgoing (all)</b>\n"
                f"Cancelled <b>{ok_n}</b> / {len(oids)} pending invite(s)."
            )
            if ok_n < len(oids) and last_err:
                note += "\n" + escape_html_telegram(last_err)
            bot.send_message(cid, note, parse_mode="HTML")
            return

        if op == "S":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                cid,
                "<b>Cancel outgoing (one)</b>\n"
                "Send the player’s <b>Epic account ID</b> (32 hex) or <b>display name</b> — "
                "the same account shown under <b>Outgoing</b> in <b>📋 Lists</b>.\n"
                "<code>/cancel</code> to abort.",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(
                msg,
                partial(
                    process_friend_target_step,
                    bot,
                    saved_account_id=aid,
                    mode="cancel_out",
                ),
            )
            return

        if op == "a":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                cid,
                "<b>Add Friend</b>\n"
                "Send the player’s <b>Epic account ID</b> (32 hex) or <b>display name</b>.\n"
                "Optional second line: <b>6 digits</b> PIN (or rely on a PIN saved under "
                "<b>Parental controls 🔢</b>).\n\n"
                "<i>If their privacy blocks invites, they must change Epic privacy or add you first.</i>\n\n"
                "Send <code>/cancel</code> to cancel.",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(
                msg,
                partial(process_friend_target_step, bot, saved_account_id=aid, mode="add"),
            )
            return

        if op == "v":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                cid,
                "<b>Remove Friend</b>\n"
                "Send their <b>Epic account ID</b> or <b>display name</b>.\n"
                "Send <code>/cancel</code> to cancel.",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(
                msg,
                partial(process_friend_target_step, bot, saved_account_id=aid, mode="remove"),
            )
            return

        if op == "c":
            bot.answer_callback_query(call.id)
            summ = friends_get_summary_sync(tok, self_aid)
            ids = friends_incoming_account_ids(summ)
            if not ids:
                bot.send_message(
                    cid,
                    "<b>Accept Friends</b>\nNo incoming friend requests.",
                    parse_mode="HTML",
                )
                return
            p6 = (entry.get("parental_pin_6") or "").strip()
            saved_pin = p6 if p6.isdigit() and len(p6) == 6 else ""
            ok, err = friends_accept_incoming_bulk_sync(
                tok, self_aid, ids, parental_pin=saved_pin
            )
            if ok:
                _edit_friends_menu(bot, cid, mid, entry, tok, self_aid)
            else:
                bot.send_message(
                    cid,
                    "<b>Accept failed</b>\n" + _epic_friend_error_text_for_tg(err),
                    parse_mode="HTML",
                )
            return

        if op == "n":
            bot.answer_callback_query(call.id)
            summ = friends_get_summary_sync(tok, self_aid)
            ids = friends_incoming_account_ids(summ)
            if not ids:
                bot.send_message(
                    cid,
                    "<b>Deny Requests</b>\nNo incoming friend requests.",
                    parse_mode="HTML",
                )
                return
            ok_ct = 0
            for uid in ids:
                if friends_reject_incoming_sync(tok, self_aid, uid):
                    ok_ct += 1
            note = (
                f"<b>Deny Requests</b>\nProcessed {ok_ct}/{len(ids)}."
            )
            bot.send_message(cid, note, parse_mode="HTML")
            _edit_friends_menu(bot, cid, mid, entry, tok, self_aid)
            return

        if op == "m":
            bot.answer_callback_query(call.id)
            ok, err = friends_remove_all_sync(tok, self_aid)
            if ok:
                _edit_friends_menu(bot, cid, mid, entry, tok, self_aid)
            else:
                bot.send_message(
                    cid,
                    "<b>Remove all friends</b> failed.\n"
                    + _epic_friend_error_text_for_tg(err),
                    parse_mode="HTML",
                )
            return

        bot.answer_callback_query(call.id, "Unknown action.", show_alert=True)
        return

    if data.startswith("C") and len(data) == 34:
        aid = data[1:33].lower()
        op = data[33]
        if not _EPIC_ACCOUNT_ID_RE.match(aid):
            bot.answer_callback_query(
                call.id,
                "🚫 The account is invalid. Please log in again",
                show_alert=True,
            )
            return
        entry = _find_saved_account(user_data, aid)
        if not entry:
            bot.answer_callback_query(call.id, "Account not found.", show_alert=True)
            return

        if op == "h":
            bot.answer_callback_query(call.id)
            _open_parental_controls_menu(bot, cid, mid, user_data, aid)
            return

        if op == "b":
            bot.answer_callback_query(call.id)
            _open_saved_account_manage(bot, cid, mid, user_data, aid)
            return

        if op == "t":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    _safe_user_error("No saved login. Use <b>/login</b> once."),
                    parse_mode="HTML",
                )
                return
            _parental_pin_verify_sync_day(user_data)
            used = _parental_pin_verify_count_today(user_data)
            if used >= _PARENTAL_PIN_VERIFY_MAX_PER_DAY:
                bot.send_message(
                    cid,
                    "<b>Too many attempts</b>\n\n"
                    f"You can only verify a parental PIN <b>{_PARENTAL_PIN_VERIFY_MAX_PER_DAY}</b> times per UTC day. "
                    "Try again tomorrow.",
                    parse_mode="HTML",
                )
                return
            left = _PARENTAL_PIN_VERIFY_MAX_PER_DAY - used
            msg = bot.send_message(
                cid,
                "<b>Parental PIN</b> (Epic content controls)\n\n"
                f"<b>Note:</b> Epic PIN verification is limited to <b>{_PARENTAL_PIN_VERIFY_MAX_PER_DAY}</b> "
                f"attempts per UTC day (<b>{left}</b> left today).\n\n"
                "Send <b>exactly 6 digits</b>. The bot verifies with Epic, then saves the PIN for "
                "<b>Add Friend</b>.\n\n"
                "<code>/cancel</code> to abort.",
                parse_mode="HTML",
            )
            bot.register_next_step_handler(
                msg,
                partial(process_parental_pin_step, bot, saved_account_id=aid),
            )
            return

        if op == "q":
            bot.answer_callback_query(call.id)
            entry.pop("parental_pin_6", None)
            tg_user.user_data = user_data
            tg_user.update_data()
            _open_parental_controls_menu(bot, cid, mid, user_data, aid)
            return

        bot.answer_callback_query(call.id, "Unknown action.", show_alert=True)
        return

    if data.startswith("X") and len(data) == 34:
        aid = data[1:33].lower()
        action = data[33]
        if not _EPIC_ACCOUNT_ID_RE.match(aid):
            bot.answer_callback_query(
                call.id,
                "🚫 The account is invalid. Please log in again",
                show_alert=True,
            )
            return

        entry = _find_saved_account(user_data, aid)
        if not entry:
            bot.answer_callback_query(call.id, "Account not found.", show_alert=True)
            return

        if action == "r":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    _safe_user_error("No saved login. Use <b>/login</b> once."),
                    parse_mode="HTML",
                )
                return
            _run_saved_account_recheck_from_device_auth(
                bot, cid, tg_user, user_data, entry
            )
            return

        if action == "f":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    _safe_user_error("No saved login. Use <b>/login</b> once."),
                    parse_mode="HTML",
                )
                return
            tok = device_auth_oauth_access_token_bearer_sync(
                entry.get("device_auth") or {}
            )
            if not tok:
                bot.send_message(
                    cid,
                    _safe_user_error("Could not start session for friends. Try <b>/login</b> again."),
                    parse_mode="HTML",
                )
                return
            self_aid = (entry.get("account_id") or aid).strip().lower()
            _edit_friends_menu(bot, cid, mid, entry, tok, self_aid)
            return

        if action == "w":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    _safe_user_error("No saved login for this account. Use <b>/login</b> once."),
                    parse_mode="HTML",
                )
                return
            url = generate_epic_web_login_url_from_device_auth_sync(
                entry.get("device_auth") or {}
            )
            if not url:
                bot.send_message(
                    cid,
                    "Could not start web session. Try <b>/login</b> again or check logs.",
                    parse_mode="HTML",
                )
                return
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(
                    "🌐 Open Epic (web login)",
                    url=url,
                )
            )
            bot.send_message(
                cid,
                "<b>Web login</b>\n"
                "• Tap <b>Open</b> to launch the browser.\n"
                "• Or long-press the block below and choose <b>Copy</b> (same on desktop).\n\n"
                f"{tg_code(url)}",
                reply_markup=markup,
                parse_mode="HTML",
            )
            return

        if action == "p":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    _safe_user_error("No saved login. Use <b>/login</b> once."),
                    parse_mode="HTML",
                )
                return
            lc = get_fortnite_launcher_exchange_code_from_device_auth_sync(
                entry.get("device_auth") or {}
            )
            if not lc:
                bot.send_message(
                    cid,
                    "Could not get launcher code. Try <b>/login</b> again.",
                    parse_mode="HTML",
                )
                return
            acc_id = (entry.get("account_id") or aid).strip()
            bat = build_fortnite_start_bat(lc, acc_id)
            if not bat:
                bot.send_message(
                    cid,
                    "Could not build launcher script. Try again.",
                    parse_mode="HTML",
                )
                return
            gr = (user_data.get("fortnite_game_root") or "").strip()
            win64 = _example_win64_path_for_caption(gr)
            cmd_line = (
                f'start /d "{win64}" FortniteLauncher.exe -AUTH_LOGIN=unused '
                f"-AUTH_PASSWORD={lc} -AUTH_TYPE=exchangecode -epicapp=Fortnite -epicenv=Prod "
                f"-EpicPortal -epicuserid={acc_id}"
            )
            cap = (
                "🎮 To launch Fortnite, open the file or use the command, and wait for the game to start.\n\n"
                "❗ This file is one-time use and works for <b>5 minutes</b> after creation.\n\n"
                f"{tg_code(cmd_line)}"
            )
            bio = BytesIO(bat.encode("utf-8"))
            bio.seek(0)
            del_markup = InlineKeyboardMarkup()
            del_markup.add(InlineKeyboardButton("❌ Delete", callback_data="deldoc"))
            bot.send_document(
                cid,
                bio,
                visible_file_name="start.bat",
                caption=cap,
                parse_mode="HTML",
                reply_markup=del_markup,
            )
            return

        if action == "d":
            bot.answer_callback_query(call.id)
            disc = InlineKeyboardMarkup()
            disc.row(
                InlineKeyboardButton(
                    "🌐 Delete device auth on Epic",
                    callback_data=f"X{aid}D",
                ),
                InlineKeyboardButton(
                    "📱 Remove from bot only",
                    callback_data=f"X{aid}L",
                ),
            )
            disc.add(InlineKeyboardButton("⬅️ Back", callback_data=f"M{aid}"))
            bot.edit_message_text(
                "<b>Disconnect</b>\n\n"
                "• <b>Epic</b> — revoke this device login on Epic’s side (uses your saved login; "
                "recommended if you no longer trust this device).\n"
                "• <b>Bot only</b> — remove the account from this bot only; Epic may still show the device "
                "until you revoke it in Epic account settings.\n\n"
                "<i>Choose an option:</i>",
                cid,
                mid,
                reply_markup=disc,
                parse_mode="HTML",
            )
            return

        if action == "D":
            bot.answer_callback_query(call.id)
            if not _saved_entry_has_usable_device_auth(entry):
                bot.send_message(
                    cid,
                    "No device login saved — can’t revoke on Epic from here. "
                    "Use <b>Remove from bot only</b> or <b>/login</b> again.",
                    parse_mode="HTML",
                )
                return
            ok, err = delete_device_auth_on_epic_servers_sync(
                entry.get("device_auth") or {}
            )
            if ok:
                _remove_saved_account_from_profile(tg_user, user_data, aid)
                _edit_saved_accounts_list(bot, cid, mid, user_data)
            else:
                bot.send_message(
                    cid,
                    "<b>Epic device auth delete</b> failed.\n"
                    f"{_epic_friend_error_text_for_tg(err)}",
                    parse_mode="HTML",
                )
            return

        if action == "L":
            bot.answer_callback_query(call.id)
            _remove_saved_account_from_profile(tg_user, user_data, aid)
            _edit_saved_accounts_list(bot, cid, mid, user_data)
            return

        bot.answer_callback_query(call.id, "Coming soon.", show_alert=False)
        return

    bot.answer_callback_query(call.id)
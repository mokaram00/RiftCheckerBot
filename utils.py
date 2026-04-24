"""
Shared helpers: display, dates, Epic account/email/portal parsing, Telegram markdown.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from curl_cffi import requests as curl_requests

from admin_api_summary import looks_like_cloudflare_html, session_add
from epic_response_log import epic_log_from_response


# --- Display ---


def bool_to_emoji(value: bool) -> str:
    return "✅" if value else "❌"


def country_to_flag(country_code: str) -> str:
    if len(country_code) != 2:
        return country_code
    return chr(ord(country_code[0]) + 127397) + chr(ord(country_code[1]) + 127397)


# --- Epic / ISO dates ---
# Project convention: user-visible dates use **mm/dd/yyyy** plus a second phrase with the
# **English full month name** (e.g. ``03/22/2025 · March 22, 2025``). When time is shown,
# append the month-name calendar line after the numeric line. Internal filenames / log keys
# may use ISO-style tokens (e.g. ``%Y%m%d`` for sortable file names).


def _english_month_calendar_date(dt: datetime) -> str:
    """Same calendar day as ``dt``, e.g. ``March 22, 2025``."""
    return dt.strftime("%B %d, %Y")


def parse_epic_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse common Epic ISO strings to an aware UTC ``datetime``."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_date_mmddyyyy(value: Optional[str]) -> str:
    """Epic / ISO datetime → ``mm/dd/yyyy · Month D, YYYY`` (English month name)."""
    dt = parse_epic_iso_datetime(value)
    if dt is not None:
        return f"{dt.strftime('%m/%d/%Y')} · {_english_month_calendar_date(dt)}"
    if value and isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-":
        try:
            d = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return f"{d.strftime('%m/%d/%Y')} · {_english_month_calendar_date(d)}"
        except ValueError:
            pass
    return ""


def format_epic_iso_date(value: Optional[str]) -> Optional[str]:
    """Normalized email metadata dates as ``mm/dd/yyyy``."""
    if not value or not isinstance(value, str):
        return None
    out = format_date_mmddyyyy(value)
    return out if out else None


def format_epic_iso_ddmmyy(value: Optional[str]) -> str:
    """Legacy name: same as :func:`format_date_mmddyyyy` (``mm/dd/yyyy``)."""
    return format_date_mmddyyyy(value)


def format_metadata_date_yyyy_mm_dd(iso: Optional[str]) -> str:
    """Display date as ``mm/dd/yyyy``; ``—`` if missing/unparseable (name kept for callers)."""
    if not iso:
        return "—"
    out = format_date_mmddyyyy(iso)
    return out if out else "—"


def format_datetime_utc_ms_dual(ms: Union[int, float]) -> str:
    """Order ``createdAtMillis`` → ``mm/dd/yyyy HH:MM:SS UTC · Month D, YYYY``."""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return (
        f"{dt.strftime('%m/%d/%Y %H:%M:%S UTC')} · "
        f"{_english_month_calendar_date(dt)}"
    )


def format_local_now_dual(*, with_time: bool = True) -> str:
    """Local ``now()`` for logs / image footer."""
    dt = datetime.now()
    if with_time:
        return (
            f"{dt.strftime('%m/%d/%Y %H:%M:%S')} · "
            f"{_english_month_calendar_date(dt)}"
        )
    return f"{dt.strftime('%m/%d/%Y')} · {_english_month_calendar_date(dt)}"


def _format_last_login_for_display(raw: Optional[str]) -> str:
    """Epic ``lastLogin`` ISO → numeric + month name + relative days."""
    if not raw or not isinstance(raw, str) or not raw.strip():
        return "—"
    s = raw.strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        date_str = f"{dt.strftime('%m/%d/%Y')} · {_english_month_calendar_date(dt)}"
        days = (datetime.now(timezone.utc) - dt).days
        if days >= 0:
            return f"{date_str} ({days} days ago)"
        return f"{date_str} (in {-days} days)"
    except (ValueError, TypeError):
        return s


# --- Epic account email API (normalized dict from /account/.../email) ---


def empty_epic_account_email_info() -> Dict[str, Any]:
    return {
        "ok": False,
        "default_email": "",
        "verified": None,
        "deliverable": None,
        "can_update_email": None,
        "email_changeable_on": None,
        "last_email_change": None,
        "has_last_email_change": False,
        "can_update_next_iso": None,
        "last_email_update_iso": None,
    }


def normalize_epic_account_email_payload(data: dict) -> Dict[str, Any]:
    emails = data.get("emails") or []
    default_entry = next((e for e in emails if e.get("default")), None)
    if default_entry is None and emails:
        default_entry = emails[0]
    default_entry = default_entry or {}
    default_email = default_entry.get("email") or ""
    verified = bool(default_entry.get("verified", False))
    if "deliverable" in default_entry:
        deliverable = bool(default_entry["deliverable"])
    else:
        deliverable = verified
    last_up = data.get("lastEmailUpdate")
    has_last = bool(last_up)
    return {
        "ok": True,
        "default_email": default_email,
        "verified": verified,
        "deliverable": deliverable,
        "can_update_email": bool(data.get("canUpdateEmail", False)),
        "email_changeable_on": format_epic_iso_date(data.get("canUpdateNext")),
        "last_email_change": format_epic_iso_date(last_up),
        "has_last_email_change": has_last,
        "can_update_next_iso": data.get("canUpdateNext"),
        "last_email_update_iso": last_up if has_last else None,
    }


def format_epic_email_last_change_ddmmyy(email_info: dict) -> str:
    """``mm/dd/yyyy`` (legacy function name)."""
    if not email_info.get("has_last_email_change"):
        return "—"
    iso = email_info.get("last_email_update_iso")
    if not iso:
        return "—"
    return format_date_mmddyyyy(iso) or "—"


def format_can_update_email_line(email_info: dict) -> str:
    if not email_info.get("ok"):
        return bool_to_emoji(False)
    if email_info.get("can_update_email") is True:
        return bool_to_emoji(True)
    iso = email_info.get("can_update_next_iso")
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            dd = format_date_mmddyyyy(iso) or (
                f"{dt.strftime('%m/%d/%Y')} · {_english_month_calendar_date(dt)}"
            )
            nowd = datetime.now(timezone.utc).date()
            dleft = (dt.date() - nowd).days
            if dleft > 0:
                return f"{bool_to_emoji(False)} {dd} (in {dleft} days)"
            if dleft == 0:
                return f"{bool_to_emoji(False)} {dd} (today)"
            return f"{bool_to_emoji(False)} {dd}"
        except (ValueError, TypeError):
            pass
    return bool_to_emoji(False)


# --- Epic account portal HTML (parse only; fetch uses curl_cffi in epic_auth) ---


def extract_account_portal_preload_json(html: str) -> Optional[dict]:
    marker = "window.account_dataPreload"
    pos = html.find(marker)
    if pos == -1:
        return None
    brace_start = html.find("{", pos)
    if brace_start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html, brace_start)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def format_portal_2fa_methods_text(preload: Optional[dict]) -> str:
    if not preload:
        return "🛡 2FA methods: —"
    tfa = preload.get("twoFactorAuthentication")
    if not isinstance(tfa, dict):
        return "🛡 2FA methods: —"
    inner = tfa.get("twoFactorAuthentication")
    if not isinstance(inner, dict):
        return "🛡 2FA methods: —"
    methods = inner.get("methods")
    default_m = inner.get("defaultMethod", "—")
    lines = [
        "🛡 2FA methods:",
        f"   Default: {default_m}",
    ]
    if not isinstance(methods, dict) or not methods:
        lines.append(f"   enabled: {bool_to_emoji(inner.get('enabled', False))}")
        return "\n".join(lines)
    labels = {
        "sms": "SMS",
        "authenticator": "Authenticator app",
        "email": "Email",
    }
    order = ("authenticator", "sms", "email")
    seen = set()
    for key in order:
        meta = methods.get(key)
        if not isinstance(meta, dict):
            continue
        seen.add(key)
        label = labels.get(key, key)
        lines.append(
            f"   • {label}: on {bool_to_emoji(meta.get('enabled', False))} · verified {bool_to_emoji(meta.get('verified', False))}"
        )
    for key, meta in sorted(methods.items()):
        if key in seen or not isinstance(meta, dict):
            continue
        lines.append(
            f"   • {key}: on {bool_to_emoji(meta.get('enabled', False))} · verified {bool_to_emoji(meta.get('verified', False))}"
        )
    return "\n".join(lines)


# --- Save the World (campaign / theater) ---


_FOUNDER_QUEST_IDS = {
    "standard": "Quest:foundersquest_getrewards_0_1",
    "deluxe": "Quest:foundersquest_getrewards_1_2",
    "super_deluxe": "Quest:foundersquest_getrewards_2_3",
    "limited": "Quest:foundersquest_getrewards_3_4",
    "ultimate": "Quest:foundersquest_getrewards_4_5",
}


def _stw_quest_claimed(items: Dict[str, Any], template_id: str) -> bool:
    for it in (items or {}).values():
        if not isinstance(it, dict):
            continue
        if it.get("templateId") != template_id:
            continue
        return it.get("attributes", {}).get("quest_state") in ("Claimed", "Completed")
    return False


def stw_research_levels_from_stats(stats: dict[str, Any]) -> dict[str, int]:
    rl = stats.get("research_levels") or {}
    if not isinstance(rl, dict):
        rl = {}

    def _lvl(key: str, alt: Optional[str] = None) -> int:
        v = rl.get(key)
        if v is None and alt:
            v = rl.get(alt)
        try:
            return int(v) if v is not None else 1
        except (TypeError, ValueError):
            return 1

    return {
        "offense": _lvl("offense", "offence"),
        "fortitude": _lvl("fortitude"),
        "resistance": _lvl("resistance"),
        "technology": _lvl("technology"),
    }


def stw_approx_power_from_research(research: dict[str, int]) -> int:
    """Legacy: sum(research)/10; prefer ``stw_power_from_stats`` for display."""
    s = (
        research["offense"]
        + research["fortitude"]
        + research["resistance"]
        + research["technology"]
    )
    return max(0, int(round(s / 10.0)))


def stw_daily_login_from_stats(stats: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """(totalDaysLoggedIn, nextDefaultReward) from campaign ``stats.attributes``."""
    td: Any = stats.get("totalDaysLoggedIn")
    nd: Any = stats.get("nextDefaultReward")
    qm = stats.get("quest_manager")
    if isinstance(qm, dict):
        if td is None:
            td = qm.get("totalDaysLoggedIn")
        if nd is None:
            nd = qm.get("nextDefaultReward")
    for nest_key in ("daily_rewards", "daily_login", "dailyLoginReward", "daily_reward"):
        sub = stats.get(nest_key)
        if isinstance(sub, dict):
            if td is None:
                td = sub.get("totalDaysLoggedIn")
            if nd is None:
                nd = sub.get("nextDefaultReward")
    try:
        td_i = int(td) if td is not None else None
    except (TypeError, ValueError):
        td_i = None
    try:
        nd_i = int(nd) if nd is not None else None
    except (TypeError, ValueError):
        nd_i = None
    return td_i, nd_i


def stw_format_daily_login_line(
    total_days: Optional[int]
) -> str:
    if total_days is None:
        return "—"
    parts: list[str] = []
    parts.append(f"{total_days} Days")
    return " · ".join(parts)


def _stw_int_from_mapping(d: Any, keys: tuple[str, ...]) -> Optional[int]:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            continue
    return None


def _stw_gameplay_stat_power(stats: dict[str, Any]) -> Optional[int]:
    for row in stats.get("gameplay_stats") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("statName") or "").lower()
        if "power" not in name and name not in ("pl", "partyrating"):
            continue
        try:
            return max(0, int(round(float(row.get("statValue", 0)))))
        except (TypeError, ValueError):
            continue
    return None


def _stw_fort_stat_total_from_items(items: Dict[str, Any]) -> Optional[int]:
    """Sum ``quantity`` of main FORT Stat items (survivor + research contribution)."""
    want = frozenset(
        ("Stat:offense", "Stat:fortitude", "Stat:resistance", "Stat:technology")
    )
    total = 0
    n = 0
    for it in (items or {}).values():
        if not isinstance(it, dict):
            continue
        tid = it.get("templateId") or ""
        if tid not in want:
            continue
        q = it.get("quantity")
        try:
            total += int(q)
            n += 1
        except (TypeError, ValueError):
            continue
    if n == 4:
        return total
    return None


def _stw_power_from_loadout_items(
    items: Optional[Dict[str, Any]], selected_loadout_id: Optional[str]
) -> Optional[int]:
    """Read cached rating from ``CampaignHeroLoadout`` (selected or best of all)."""
    if not isinstance(items, dict):
        return None
    loadout_keys = (
        "power_rating",
        "party_power_rating",
        "cached_power_rating",
        "loadout_power_rating",
        "personal_power_rating",
    )
    best: Optional[int] = None

    def _take(attrs: Any) -> None:
        nonlocal best
        if not isinstance(attrs, dict):
            return
        v = _stw_int_from_mapping(attrs, loadout_keys)
        if v is not None:
            best = max(best or 0, max(0, v))

    if selected_loadout_id:
        lo = items.get(selected_loadout_id)
        if isinstance(lo, dict) and (
            lo.get("templateId") or ""
        ).startswith("CampaignHeroLoadout"):
            _take(lo.get("attributes"))

    if best is None:
        for it in items.values():
            if not isinstance(it, dict):
                continue
            tid = it.get("templateId") or ""
            if not tid.startswith("CampaignHeroLoadout"):
                continue
            _take(it.get("attributes"))

    return best


def stw_power_from_stats(
    stats: dict[str, Any],
    research: dict[str, int],
    items: Optional[Dict[str, Any]] = None,
    selected_loadout_id: Optional[str] = None,
) -> int:
    """
    Prefer Epic's party / account power rating; then loadout cache; then gameplay_stats;
    then approximate from total FORT Stat account items; last resort: sum of research
    tree levels (not /10).
    """
    power_keys = (
        "party_power_rating",
        "partyPowerRating",
        "highest_account_power",
        "highestAccountPower",
        "account_power_rating",
        "accountPowerRating",
        "primary_quickbar_rating",
        "primaryQuickbarRating",
        "maximum_personal_power_rating",
        "maximumPersonalPowerRating",
        "highest_personal_power_rating",
        "highestPersonalPowerRating",
        "power_level",
        "powerLevel",
        "power",
    )
    v = _stw_int_from_mapping(stats, power_keys)
    if v is not None:
        return max(0, v)
    qm = stats.get("quest_manager")
    if isinstance(qm, dict):
        v = _stw_int_from_mapping(
            qm,
            (
                "party_power_rating",
                "partyPowerRating",
                "power_rating",
                "powerRating",
            ),
        )
        if v is not None:
            return max(0, v)

    v = _stw_gameplay_stat_power(stats)
    if v is not None:
        return v

    v = _stw_power_from_loadout_items(items, selected_loadout_id)
    if v is not None:
        return v

    fort_total = _stw_fort_stat_total_from_items(items or {})
    if fort_total is not None and fort_total > 0:
        # Rough PL-style scale from combined FORT Stat quantities (when API omits party_power_rating).
        # 31 pts ≈ 1 PL step matches typical UI vs Stat totals (e.g. 1430 → ~46).
        return max(1, max(0, int(round(fort_total / 31.0))))

    s = (
        research["offense"]
        + research["fortitude"]
        + research["resistance"]
        + research["technology"]
    )
    return max(0, int(s))


def stw_entitlements_indicates_founder_go_style(payload: Any) -> bool:
    """Match xboxfn.go: ``entitlements`` JSON contains ``Fortnite_Founder`` (``entitlementName``).

    Used for Save the World / founder access the same way as Go ``hasStw`` (substring or parsed list).
    """
    if payload is None:
        return False
    if isinstance(payload, str):
        return (
            'entitlementName":"Fortnite_Founder"' in payload
            or '"entitlementName":"Fortnite_Founder"' in payload
        )
    if isinstance(payload, list):
        for e in payload:
            if not isinstance(e, dict):
                continue
            if (e.get("entitlementName") or "") == "Fortnite_Founder":
                return True
    if isinstance(payload, dict):
        for key in ("entitlements", "items", "data"):
            if key in payload and stw_entitlements_indicates_founder_go_style(payload[key]):
                return True
    return False


def stw_edition_flags_from_campaign_items(
    items: Dict[str, Any],
    *,
    entitlements_payload: Optional[Any] = None,
) -> dict[str, bool]:
    q01 = _stw_quest_claimed(items, _FOUNDER_QUEST_IDS["standard"])
    q12 = _stw_quest_claimed(items, _FOUNDER_QUEST_IDS["deluxe"])
    q23 = _stw_quest_claimed(items, _FOUNDER_QUEST_IDS["super_deluxe"])
    q34 = _stw_quest_claimed(items, _FOUNDER_QUEST_IDS["limited"])
    q45 = _stw_quest_claimed(items, _FOUNDER_QUEST_IDS["ultimate"])

    new_stw = False
    for it in (items or {}).values():
        if not isinstance(it, dict):
            continue
        tid = (it.get("templateId") or "").strip()
        tlow = tid.lower()
        # Any season's New-STW starter token / quest line (IDs changed over time).
        if "stwstarterbundle" in tlow and (
            tlow.startswith("token:") or tlow.startswith("quest:")
        ):
            new_stw = True
            break
        if "starterbundle" in tlow and "stw" in tlow and (
            tlow.startswith("token:") or tlow.startswith("quest:")
        ):
            new_stw = True
            break

    # Same as xboxfn.go / utils.go flow: founder entitlement ⇒ STW access.
    if not new_stw and stw_entitlements_indicates_founder_go_style(entitlements_payload):
        new_stw = True

    return {
        "new_stw": new_stw,
        "standard": q01 or q12 or q23 or q34 or q45,
        "deluxe": q12 or q23 or q34 or q45,
        "super_deluxe": q23 or q34 or q45,
        "limited": q34 or q45,
        "ultimate": q45,
    }


def stw_format_owned_line(owned: bool) -> str:
    return "✅ Owned" if owned else "❌ Not Owned"


def stw_parse_theater_world_resources(profile_json: dict[str, Any]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Best-effort wood / stone / metal from theater profile items (IDs vary by build)."""
    pc = profile_json.get("profileChanges") or []
    if not pc:
        return None, None, None
    prof = pc[0].get("profile") if isinstance(pc[0], dict) else None
    if not isinstance(prof, dict):
        return None, None, None
    items = prof.get("items") or {}
    if not isinstance(items, dict):
        return None, None, None

    wood: Optional[int] = None
    stone: Optional[int] = None
    metal: Optional[int] = None

    for raw in items.values():
        if not isinstance(raw, dict):
            continue
        tid = (raw.get("templateId") or "").lower()
        q = raw.get("quantity")
        if q is None:
            continue
        try:
            qi = int(q)
        except (TypeError, ValueError):
            continue
        if tid.startswith("quest:") or tid.startswith("worker:") or tid.startswith("hero:"):
            continue

        compact = tid.replace(":", "").replace("_", "").replace("-", "")
        if "collectresource" in compact or "worldresource" in compact:
            if "wood" in compact or "stick" in compact:
                wood = qi
            elif "stone" in compact or "brick" in compact:
                stone = qi
            elif "metal" in compact:
                metal = qi
            continue
        if "accountresource" in compact:
            if compact.endswith("wood") and "ore" not in compact:
                wood = qi
            elif compact.endswith("stone") or compact.endswith("brick"):
                stone = qi
            elif compact.endswith("metal") and "ore" not in compact:
                metal = qi

    return wood, stone, metal


def _athena_season_template_num(template_id: str) -> Optional[int]:
    """Parse season index from ``AthenaSeason:athenaseason39``-style ids."""
    if not template_id or not isinstance(template_id, str):
        return None
    m = re.search(r"athenaseason(\d+)", template_id.lower())
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


def athena_resolve_total_wins(attrs: dict, past_seasons: Any) -> int:
    """Prefer ``lifetime_wins``; otherwise sum ``numWins`` from ``past_seasons``."""
    if isinstance(attrs, dict):
        lw = attrs.get("lifetime_wins")
        if lw is not None:
            try:
                return max(0, int(lw))
            except (TypeError, ValueError):
                pass
    if not isinstance(past_seasons, list):
        past_seasons = []
    total = sum(int(s.get("numWins", 0) or 0) for s in past_seasons if isinstance(s, dict))
    return max(0, total)


def athena_resolve_total_matches(attrs: dict, past_seasons: Any) -> Optional[int]:
    """Lifetime match count when Epic exposes it or per-season stats sum; else ``None`` if unknown."""
    if not isinstance(attrs, dict):
        attrs = {}
    for key in (
        "lifetime_matches",
        "lifetime_matches_played",
        "total_lifetime_matches",
        "matches_played",
        "total_matches_played",
        "num_matches_played",
        "lifetime_played",
    ):
        v = attrs.get(key)
        if v is not None:
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                break
    if not isinstance(past_seasons, list):
        past_seasons = []
    total_matches = sum(_athena_season_match_count(s) for s in past_seasons if isinstance(s, dict))
    if total_matches > 0:
        return total_matches
    gm_sum = 0
    for row in attrs.get("gameplay_stats") or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("statName") or "").lower()
        if "match" in name and "win" not in name:
            try:
                gm_sum += int(row.get("statValue", 0) or 0)
            except (TypeError, ValueError):
                continue
    if gm_sum > 0:
        return gm_sum
    try:
        lw = attrs.get("lifetime_wins")
        lwi = int(lw) if lw is not None else 0
    except (TypeError, ValueError):
        lwi = 0
    if lwi > 0:
        return None
    return 0


def athena_bp_purchased_current(attrs: dict) -> Optional[bool]:
    """Whether the current BR pass was purchased; ``None`` if we cannot tell."""
    if not isinstance(attrs, dict):
        return None
    for key in ("book_purchased", "book_purchased_battlepass", "hasPurchasedVIP", "purchasedVIP"):
        if attrs.get(key) is not None:
            return bool(attrs[key])
    season_num = attrs.get("season_num")
    try:
        sn = int(season_num) if season_num is not None else None
    except (TypeError, ValueError):
        sn = None
    h = attrs.get("past_season_purchase_context_histories")
    if not isinstance(h, dict):
        return None
    br = (h.get("historiesPerPassType") or {}).get("br")
    if not isinstance(br, list) or not br:
        return None
    entry = None
    if sn is not None:
        for e in br:
            if not isinstance(e, dict):
                continue
            tid = e.get("seasonTemplateId") or ""
            if _athena_season_template_num(tid) == sn:
                entry = e
                break
        if entry is None:
            return None
    else:
        for e in reversed(br):
            if isinstance(e, dict) and e.get("seasonTemplateId"):
                entry = e
                break
    if not isinstance(entry, dict):
        return None
    pch = entry.get("purchaseContextHistory")
    if not isinstance(pch, list) or not pch:
        return None
    last = pch[-1]
    if not isinstance(last, dict):
        return None
    try:
        pl = int(last.get("purchasedLevels", 0) or 0)
    except (TypeError, ValueError):
        pl = 0
    pc = last.get("purchaseContext")
    pc_s = "" if pc is None else str(pc).strip().lower()
    if pl > 0:
        return True
    if pc_s and pc_s not in ("none", ""):
        return True
    return False


def _athena_season_match_count(season: dict) -> int:
    """Per-season match total: prefer explicit totals, then sum all *Bracket* / match counters."""
    if not isinstance(season, dict):
        return 0
    for key in (
        "numMatches",
        "matchesPlayed",
        "matches",
        "num_played",
        "totalMatches",
        "numMatchesPlayed",
    ):
        if key in season:
            try:
                return max(0, int(season[key] or 0))
            except (TypeError, ValueError):
                return 0
    total = 0
    for k, v in season.items():
        if not isinstance(k, str):
            continue
        if k == "numWins":
            continue
        if "Bracket" in k or "bracket" in k:
            try:
                total += int(v or 0)
            except (TypeError, ValueError):
                pass
        elif k.startswith("num") and "Match" in k:
            try:
                total += int(v or 0)
            except (TypeError, ValueError):
                pass
    return max(0, total)


def athena_past_seasons_totals(
    past_seasons: Any, attrs: Optional[Dict[str, Any]] = None
) -> tuple[int, Optional[int]]:
    """Lifetime wins (prefer ``lifetime_wins``) and matches (``None`` when unknown)."""
    a = attrs if isinstance(attrs, dict) else {}
    tw = athena_resolve_total_wins(a, past_seasons)
    tm = athena_resolve_total_matches(a, past_seasons)
    return tw, tm


def athena_gold_bars_from_items(items: Any) -> Optional[int]:
    """Best-effort gold bars from Athena inventory (template IDs vary by season)."""
    if not isinstance(items, dict):
        return None
    for it in items.values():
        if not isinstance(it, dict):
            continue
        tid = (it.get("templateId") or "").lower()
        if (
            "goldbar" in tid
            or "gold_bar" in tid
            or "athenacurrency:gold" in tid
            or "athena_currency:gold" in tid
            or tid.endswith(":gold")
        ):
            try:
                return int(it.get("quantity", 0))
            except (TypeError, ValueError):
                return None
    return None


def athena_gold_bars(attrs: dict, items: dict) -> Optional[int]:
    """Gold bars from stats attributes, then inventory."""
    if isinstance(attrs, dict):
        for key in ("gold_bars", "goldbars", "br_gold_bars", "bars_gold"):
            v = attrs.get(key)
            if v is not None:
                try:
                    return max(0, int(v))
                except (TypeError, ValueError):
                    break
    return athena_gold_bars_from_items(items)


def _yes_no_unknown(v: Any) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "Unknown"


def format_account_statistics_telegram(athena_data: Optional[Dict[str, Any]]) -> str:
    """HTML block for Telegram: BR account level, wins/matches, season / BP snapshot."""
    if not athena_data or not isinstance(athena_data, dict):
        return "<i>Account statistics unavailable.</i>"
    pc = athena_data.get("profileChanges") or []
    if not pc or not isinstance(pc[0], dict):
        return "<i>Account statistics unavailable.</i>"
    prof = pc[0].get("profile") or {}
    attrs = (prof.get("stats") or {}).get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    items = prof.get("items") or {}
    if not isinstance(items, dict):
        items = {}

    tw, tm = athena_past_seasons_totals(attrs.get("past_seasons"), attrs)
    tm_s = str(tm) if tm is not None else "Unknown"
    acc_level = attrs.get("accountLevel")
    acc_level_s = str(acc_level) if acc_level is not None else "Unknown"

    mfa = attrs.get("mfa_reward_claimed")
    if mfa is None:
        mfa = attrs.get("claimed_mfa_reward")
    mfa_s = _yes_no_unknown(mfa)

    gold = athena_gold_bars(attrs, items)
    gold_s = str(gold) if gold is not None else "Unknown"

    season_level = attrs.get("level", 1)
    book_level = attrs.get("book_level", 1)

    bp = athena_bp_purchased_current(attrs)

    if bp is None:
        bp_disp = escape_html_telegram("Unknown")
    else:
        bp_disp = bool_to_emoji(bool(bp))

    stars = attrs.get("book_tokens")
    if stars is None:
        stars = attrs.get("battlestars")
    if stars is None:
        stars = attrs.get("season_match_boost")
    stars_s = str(stars) if stars is not None else "Unknown"

    return (
        f"<b>━━━━━━━━━━━</b>\n"
        f"<b>Account Statistics</b>\n"
        f"<b>━━━━━━━━━━━</b>\n"
        f"🆔 <b>Account Level</b> {escape_html_telegram(acc_level_s)}\n"
        f"🏆 <b>Total Wins</b> {tw}\n"
        f"🎟 <b>Total Matches</b> {escape_html_telegram(tm_s)}\n"
        f"💰 <b>Gold Bars</b> {escape_html_telegram(gold_s)}\n"
        f"\n"
        f"<b>━━━━━━━━━━━</b>\n"
        f"<b>Current Season</b>\n"
        f"<b>━━━━━━━━━━━</b>\n"
        f"🔢 <b>Season Level</b> {season_level}\n"
        f"🎫 <b>Battle Pass Level</b> {book_level}\n"
        f"💳 <b>Battle Pass Purchased</b> {bp_disp}\n"
        f"⭐️ <b>Battlestars</b> {escape_html_telegram(stars_s)}"
    )


def locker_spray_count_from_categories(cosmetic_categories: Any) -> int:
    """Spray items live under ``AthenaDance`` with ``spray_`` / ``spid_`` id prefixes."""
    if not isinstance(cosmetic_categories, dict):
        return 0
    raw = cosmetic_categories.get("AthenaDance")
    if not isinstance(raw, list):
        return 0
    n = 0
    for i in raw:
        if not isinstance(i, str):
            continue
        lo = i.lower()
        if lo.startswith("spray_") or lo.startswith("spid_"):
            n += 1
    return n


def format_full_locker_summary_telegram(locker_data: Any) -> str:
    """HTML block: counts for every locker category (matches in-game groupings)."""
    arr = getattr(locker_data, "cosmetic_array", None) or {}
    cats = getattr(locker_data, "cosmetic_categories", None) or {}
    if not isinstance(arr, dict):
        arr = {}
    if not isinstance(cats, dict):
        cats = {}
    sprays = locker_spray_count_from_categories(cats)
    rows = [
        ("🧍‍♂️", "Outfits", len(arr.get("AthenaCharacter") or [])),
        ("🎒", "Backpacks", len(arr.get("AthenaBackpack") or [])),
        ("⛏️", "Pickaxes", len(arr.get("AthenaPickaxe") or [])),
        ("🕺", "Dances", len(arr.get("AthenaDance") or [])),
        ("✈️", "Gliders", len(arr.get("AthenaGlider") or [])),
        ("🎁", "Wraps", len(arr.get("AthenaItemWrap") or [])),
        ("🎌", "Banners", len(arr.get("HomebaseBannerIcons") or [])),
        ("🖌", "Sprays", sprays),
        ("🖼", "Loading screens", len(arr.get("AthenaLoadingScreen") or [])),
        ("🎵", "Music packs", len(arr.get("AthenaMusicPack") or [])),
        ("⭐", "Most wanted", len(arr.get("AthenaPopular") or [])),
        ("🌟", "Exclusives", len(arr.get("AthenaExclusive") or [])),
    ]
    lines = [f"{emo} <b>{label}</b> {n}" for emo, label, n in rows]
    return (
        f"<b>━━━━━━━━━━━</b>\n"
        f"<b>🎒 Locker</b>\n"
        f"<b>━━━━━━━━━━━</b>\n"
        + "\n".join(lines)
    )


def format_friend_codes_stw_telegram(codes: Optional[List[Dict[str, Any]]]) -> str:
    """Friend / STW redeem codes (epic + xbox merged)."""
    if not codes:
        return "<i>No STW codes found</i>"
    lines: List[str] = []
    for row in codes:
        if not isinstance(row, dict):
            continue
        cid = row.get("codeId") or ""
        cty = row.get("codeType") or ""
        dc = row.get("dateCreated")
        if dc is not None and not isinstance(dc, str):
            dc = str(dc)
        dc_disp = format_epic_iso_ddmmyy(dc) if isinstance(dc, str) and dc else "—"
        lines.append(
            f"{tg_code(cid)} · {escape_html_telegram(cty)} · {escape_html_telegram(dc_disp)}"
        )
    return "\n".join(lines) if lines else "<i>No STW codes found</i>"


def _format_device_created_for_display(dt_raw: Any) -> str:
    if dt_raw is None:
        return "—"
    s = str(dt_raw).strip()
    if not s or s == "—":
        return "—"
    fd = format_date_mmddyyyy(s)
    return fd if fd else s


def _device_auth_geo_plain(block: Any) -> tuple[str, str, str]:
    """Plain IP, location, formatted datetime from ``created`` / ``lastAccess`` blob."""
    if not isinstance(block, dict):
        return "—", "—", "—"
    ip = str(block.get("ipAddress") or block.get("ip_address") or "—")
    loc = str(block.get("location") or "—")
    dt_raw = block.get("dateTime") or block.get("date_time") or "—"
    dt_out = _format_device_created_for_display(dt_raw)
    return ip, loc, dt_out


def _device_auth_geo_fields_telegram(block: Any) -> tuple[str, str, str]:
    ip, loc, dt = _device_auth_geo_plain(block)
    return (
        escape_html_telegram(ip),
        escape_html_telegram(loc),
        escape_html_telegram(dt),
    )


def _device_auth_created_fields(created: Any) -> tuple[str, str, str]:
    """Returns (ip, location, datetime display) from nested ``created`` object."""
    return _device_auth_geo_fields_telegram(created)


def _device_auth_created_plain(created: Any) -> tuple[str, str, str]:
    """Plain-text IP, location, created (for recovery export)."""
    return _device_auth_geo_plain(created)


def _device_auth_last_access_plain(last_access: Any) -> tuple[str, str, str]:
    return _device_auth_geo_plain(last_access)


def _device_auth_last_access_fields(last_access: Any) -> tuple[str, str, str]:
    return _device_auth_geo_fields_telegram(last_access)


def recovery_normalize_device_auths(payload: Any) -> List[Dict[str, str]]:
    """Public device-auth rows for recovery JSON/txt (no secrets)."""
    out: List[Dict[str, str]] = []
    if payload is None:
        return out
    if not isinstance(payload, list):
        return out
    for row in payload:
        if not isinstance(row, dict):
            continue
        ip, loc, dt = _device_auth_created_plain(row.get("created"))
        entry: Dict[str, str] = {
            "deviceId": str(row.get("deviceId") or row.get("device_id") or ""),
            "accountId": str(row.get("accountId") or row.get("account_id") or ""),
            "userAgent": str(row.get("userAgent") or row.get("user_agent") or "").strip(),
            "ip": ip,
            "location": loc,
            "created": dt,
        }
        last_block = row.get("lastAccess") or row.get("last_access")
        if isinstance(last_block, dict) and last_block:
            lip, lloc, ldt = _device_auth_last_access_plain(last_block)
            entry["lastAccessIp"] = lip
            entry["lastAccessLocation"] = lloc
            entry["lastAccess"] = ldt
        out.append(entry)
    return out


def format_device_auth_telegram(payload: Any) -> str:
    """Public device-auth list: deviceId, accountId, userAgent, created + lastAccess geo (if present)."""
    header = (
        "<b>━━━━━━━━</b>\n"
        "<b>Device Auths</b>\n"
        "<b>━━━━━━━━</b>"
    )
    if payload is None:
        return header + "\n\n<i>Device auth unavailable.</i>"
    if isinstance(payload, list):
        if not payload:
            return header + "\n\n<i>No device credentials registered.</i>"
        blocks: List[str] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            did = row.get("deviceId") or row.get("device_id") or "—"
            aid = row.get("accountId") or row.get("account_id") or "—"
            ua = row.get("userAgent") or row.get("user_agent") or ""
            ip_s, loc_s, dt_s = _device_auth_created_fields(row.get("created"))
            lines = [
                f"<b>Device ID</b> {tg_code(str(did))}",
                f"<b>Account ID</b> {tg_code(str(aid))}",
                f"<b>User-Agent</b> {escape_html_telegram(str(ua).strip() or '—')}",
                f"<b>IP</b> {ip_s}",
                f"<b>Location</b> {loc_s}",
                f"<b>Created</b> {dt_s}",
            ]
            la = row.get("lastAccess") or row.get("last_access")
            if isinstance(la, dict) and la:
                la_ip, la_loc, la_dt = _device_auth_last_access_fields(la)
                lines.extend(
                    [
                        "",
                        "<b>📍 Last location</b> <i>(recent access from this device)</i>",
                        f"<b>IP</b> {la_ip}",
                        f"<b>Location</b> {la_loc}",
                        f"<b>When</b> {la_dt}",
                    ]
                )
            blocks.append("\n".join(lines))
        if not blocks:
            return header + "\n\n<i>No device credentials registered.</i>"
        sep = "\n\n<b>───────────</b>\n\n"
        return header + "\n\n" + sep.join(blocks)
    if isinstance(payload, dict):
        return header + "\n\n" + tg_code(json.dumps(payload, ensure_ascii=False)[:3500])
    return header + "\n\n" + escape_html_telegram(str(payload)[:500])


_RESTRICTION_TYPE_LABEL: Dict[str, str] = {
    "psn": "PSN",
    "xbl": "XBL",
    "nintendo": "NINTENDO",
    "nintendo_switch": "NINTENDO",
    "lego": "LEGO",
    "twitch": "TWITCH",
    "github": "GITHUB",
    "steam": "STEAM",
    "apple": "APPLE",
    "google": "GOOGLE",
    "fb": "FACEBOOK",
    "autodesk": "AUTODESK",
    "facebook": "FACEBOOK",
    "discord": "DISCORD",
    "lego": "LEGO",
    "vk": "VK",
}


def _restriction_type_label(t: str) -> str:
    k = (t or "").strip().lower()
    return _RESTRICTION_TYPE_LABEL.get(k, k.upper() if k else "—")


def _restriction_resolve_display_name(
    entry: Dict[str, Any],
    external_auths: List[Dict[str, Any]],
) -> str:
    dn = str(entry.get("displayName") or "").strip()
    if dn and not dn.startswith("$hmac"):
        return dn
    t = str(entry.get("type") or "").lower()
    for e in external_auths:
        if str(e.get("type") or "").lower() == t:
            ex = str(e.get("externalDisplayName") or "").strip()
            if ex:
                return ex
    return dn if dn else "—"


def _restriction_created_display(entry: Dict[str, Any]) -> str:
    """``created`` ISO → ``mm/dd/yyyy`` for Telegram / recovery."""
    raw = entry.get("created")
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    return format_date_mmddyyyy(s) or (s[:10] if len(s) >= 10 else "")


def _restriction_relink_combined_lists(
    availability: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``availableRestrictions`` + ``auths`` (deduped by type) vs ``unavailableRestrictions``."""
    res = availability.get("restrictions")
    if not isinstance(res, dict):
        res = {}
    avail_list: List[Dict[str, Any]] = [
        x for x in (res.get("availableRestrictions") or []) if isinstance(x, dict)
    ]
    unavail_list: List[Dict[str, Any]] = [
        x for x in (res.get("unavailableRestrictions") or []) if isinstance(x, dict)
    ]
    seen_types = {str(x.get("type") or "").lower() for x in avail_list}
    for a in availability.get("auths") or []:
        if not isinstance(a, dict):
            continue
        t = str(a.get("type") or "").lower()
        if t and t not in seen_types:
            seen_types.add(t)
            avail_list.append(dict(a))
    return avail_list, unavail_list


def recovery_normalize_restriction_relink(
    availability: Optional[Dict[str, Any]],
    external_auths: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Structured relink rows for recovery export (includes ``created``)."""
    if not availability:
        return {"available": [], "unavailable": []}
    avail_list, unavail_list = _restriction_relink_combined_lists(availability)
    out_avail: List[Dict[str, Any]] = []
    out_un: List[Dict[str, Any]] = []
    for entry in avail_list:
        t = str(entry.get("type") or "").lower()
        dn = _restriction_resolve_display_name(entry, external_auths)
        cr = entry.get("created")
        out_avail.append(
            {
                "type": t,
                "typeLabel": _restriction_type_label(t),
                "displayName": dn,
                "created": cr if cr is not None else "",
                "createdDisplay": _restriction_created_display(entry),
            }
        )
    for entry in unavail_list:
        t = str(entry.get("type") or "").lower()
        dn = _restriction_resolve_display_name(entry, external_auths)
        cr = entry.get("created")
        nxt = _restriction_next_available_date(entry)
        out_un.append(
            {
                "type": t,
                "typeLabel": _restriction_type_label(t),
                "displayName": dn,
                "created": cr if cr is not None else "",
                "createdDisplay": _restriction_created_display(entry),
                "nextAvailable": nxt,
            }
        )
    return {"available": out_avail, "unavailable": out_un}


def _restriction_next_available_date(entry: Dict[str, Any]) -> str:
    for k in (
        "nextAvailable",
        "nextAvailableDate",
        "cooldownEnd",
        "availableAt",
        "nextRelinkDate",
        "relinkAvailableDate",
        "nextAvailableAt",
    ):
        v = entry.get(k)
        if not v:
            continue
        s = str(v).strip()
        fd = format_date_mmddyyyy(s)
        if fd:
            return fd
    return ""


def format_restriction_relink_telegram_html(
    availability: Optional[Dict[str, Any]],
    external_auths: List[Dict[str, Any]],
) -> str:
    """
    HTML block for Telegram: Available / Unavailable for Relink (Epic Help API).
    ``displayName`` values that are HMAC placeholders are replaced from ``external_auths``.
    """
    if not availability:
        return (
            "\n\n<b>━━━━━━━━━</b>\n"
            "<i>Restriction removal unavailable (could not load Help API).</i>\n"
        )
    avail_list, unavail_list = _restriction_relink_combined_lists(availability)

    parts: List[str] = []
    parts.append("\n\n<b>━━━━━━━━━</b>\n")
    parts.append("<b>Available for Relink</b>\n")
    parts.append("<b>━━━━━━━━━</b>\n")
    if not avail_list:
        parts.append("<i>(none)</i>\n")
    else:
        for entry in avail_list:
            lab = _restriction_type_label(str(entry.get("type") or ""))
            dn = _restriction_resolve_display_name(entry, external_auths)
            parts.append(
                f"✅ {escape_html_telegram(lab)} - {escape_html_telegram(dn)}\n"
            )
            cd = _restriction_created_display(entry)
            if cd:
                parts.append(f"<i>Created: {escape_html_telegram(cd)}</i>\n")

    parts.append("\n")
    parts.append("<b>━━━━━━━━━</b>\n")
    parts.append("<b>Unavailable for Relink</b>\n")
    parts.append("<b>━━━━━━━━━</b>\n")
    if not unavail_list:
        parts.append("<i>(none)</i>\n")
    else:
        for entry in unavail_list:
            lab = _restriction_type_label(str(entry.get("type") or ""))
            dn = _restriction_resolve_display_name(entry, external_auths)
            parts.append(
                f"❌ {escape_html_telegram(lab)} - {escape_html_telegram(dn)}\n"
            )
            cd = _restriction_created_display(entry)
            if cd:
                parts.append(f"<i>Created: {escape_html_telegram(cd)}</i>\n")
            nxt = _restriction_next_available_date(entry)
            if nxt:
                parts.append(
                    f"<i>Next available: {escape_html_telegram(nxt)}</i>\n"
                )

    return "".join(parts)


# --- Epic payment: PCI API (XSRF + purchaseToken), separate from order-history payment methods ---

_EPIC_PAYMENT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_EPIC_ID_CSRF_URL = "https://www.epicgames.com/id/api/csrf"
_EPIC_PURCHASE_TOKEN_URL = (
    "https://www.epicgames.com/account/v2/payment/purchaseToken?locale=en-US"
)
_EPIC_PCI_PAYMENT_METHODS_URL = (
    "https://payment-website-pci.ol.epicgames.com/v2/purchase/payment-methods"
)


def _epic_xsrf_token_from_id_csrf_set_cookie(response: Any) -> Optional[str]:
    """Parse ``XSRF-TOKEN=...`` from ``Set-Cookie`` (e.g. ``...; Path=/id``)."""
    blob_parts: List[str] = []
    for hk, hv in getattr(response, "headers", {}).items():
        if hk.lower() == "set-cookie":
            blob_parts.append(hv)
    if not blob_parts:
        return None
    blob = "\n".join(blob_parts)
    m = re.search(r"(?:^|[;\s])XSRF-TOKEN=([^;\s]+)", blob, re.I)
    if m:
        return m.group(1).strip()
    jar = getattr(response, "cookies", None)
    if jar is not None:
        try:
            v = jar.get("XSRF-TOKEN")
            if v:
                return str(v)
        except Exception:
            pass
    return None


def get_epic_payment_xsrf_token_sync(
    access_token: str,
    *,
    impersonate: str = "chrome110",
) -> str:
    """GET ``/id/api/csrf`` with bearer cookie; read ``XSRF-TOKEN`` from ``Set-Cookie``."""
    r = curl_requests.get(
        _EPIC_ID_CSRF_URL,
        headers={
            "User-Agent": _EPIC_PAYMENT_UA,
            "Cookie": f"EPIC_BEARER_TOKEN={access_token};",
        },
        impersonate=impersonate,
    )
    epic_log_from_response(
        "id_api_csrf", "GET", getattr(r, "url", None) or _EPIC_ID_CSRF_URL, r
    )
    st = int(getattr(r, "status_code", 0) or 0)
    body = (getattr(r, "text", None) or "")
    if st != 200:
        d = f"HTTP {st}"
        if looks_like_cloudflare_html(body):
            d += " · Cloudflare/WAF"
        session_add("PCI: GET /id/api/csrf (XSRF)", False, d)
    xsrf = _epic_xsrf_token_from_id_csrf_set_cookie(r)
    if not xsrf:
        d = f"no XSRF in cookies · HTTP {st}"
        if looks_like_cloudflare_html(body):
            d += " · likely Cloudflare/HTML (not csrf response)"
        session_add("PCI: GET /id/api/csrf (XSRF)", False, d)
        raise ValueError(
            "Failed to get XSRF-TOKEN from Set-Cookie after GET /id/api/csrf"
        )
    session_add("PCI: GET /id/api/csrf (XSRF)", True, "XSRF cookie present")
    return xsrf


def get_epic_purchase_token_sync(
    access_token: str,
    xsrf_token: str,
    *,
    impersonate: str = "chrome110",
) -> str:
    """POST purchaseToken with bearer + XSRF cookies and X-XSRF-TOKEN header."""
    r = curl_requests.post(
        _EPIC_PURCHASE_TOKEN_URL,
        headers={
            "User-Agent": _EPIC_PAYMENT_UA,
            "Cookie": (
                f"XSRF-TOKEN={xsrf_token}; XSRF-AM-TOKEN={xsrf_token}; "
                f"EPIC_BEARER_TOKEN={access_token};"
            ),
            "X-XSRF-TOKEN": xsrf_token,
            "Content-Type": "application/json",
        },
        json={},
        impersonate=impersonate,
    )
    epic_log_from_response(
        "payment_purchaseToken",
        "POST",
        getattr(r, "url", None) or _EPIC_PURCHASE_TOKEN_URL,
        r,
    )
    st = int(getattr(r, "status_code", 0) or 0)
    body = getattr(r, "text", None) or ""
    if st != 200:
        d = f"HTTP {st}"
        if looks_like_cloudflare_html(body):
            d += " · Cloudflare/WAF"
        session_add("PCI: POST /payment/purchaseToken", False, d)
        raise RuntimeError(
            f"purchaseToken failed HTTP {r.status_code}: {r.text[:500]}"
        )
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        d = "invalid JSON"
        if looks_like_cloudflare_html(body):
            d += " · likely Cloudflare/HTML"
        session_add("PCI: POST /payment/purchaseToken", False, d)
        raise RuntimeError(f"purchaseToken invalid JSON: {e}") from e
    pt = data.get("purchaseToken") if isinstance(data, dict) else None
    if not pt:
        session_add("PCI: POST /payment/purchaseToken", False, "missing purchaseToken in JSON")
        raise RuntimeError("purchaseToken response missing purchaseToken")
    session_add("PCI: POST /payment/purchaseToken", True, "OK")
    return str(pt)


def get_epic_pci_payment_methods_sync(
    purchase_token: str,
    *,
    impersonate: str = "chrome110",
) -> Any:
    """GET saved payment methods from PCI host using purchase token."""
    r = curl_requests.get(
        _EPIC_PCI_PAYMENT_METHODS_URL,
        headers={
            "User-Agent": _EPIC_PAYMENT_UA,
            "x-requested-with": purchase_token,
            "Content-Type": "application/json",
        },
        impersonate=impersonate,
    )
    epic_log_from_response(
        "pci_v2_purchase_payment_methods",
        "GET",
        getattr(r, "url", None) or _EPIC_PCI_PAYMENT_METHODS_URL,
        r,
    )
    st = int(getattr(r, "status_code", 0) or 0)
    body = getattr(r, "text", None) or ""
    if st != 200:
        d = f"HTTP {st}"
        if looks_like_cloudflare_html(body):
            d += " · Cloudflare/WAF"
        session_add("PCI: GET ol.epic PCI payment-methods", False, d)
        raise RuntimeError(
            f"PCI payment-methods failed HTTP {r.status_code}: {r.text[:500]}"
        )
    try:
        j = r.json()
    except json.JSONDecodeError as e:
        d = "invalid JSON"
        if looks_like_cloudflare_html(body):
            d += " · likely Cloudflare"
        session_add("PCI: GET ol.epic PCI payment-methods", False, d)
        raise RuntimeError(f"PCI payment-methods invalid JSON: {e}") from e
    session_add("PCI: GET ol.epic PCI payment-methods", True, "OK")
    return j


def fetch_pci_payment_methods_via_xsrf_sync(
    access_token: str,
    *,
    impersonate: str = "chrome110",
) -> Dict[str, Any]:
    """Full chain: XSRF → purchaseToken → PCI list (sync, curl_cffi)."""
    xsrf = get_epic_payment_xsrf_token_sync(
        access_token, impersonate=impersonate
    )
    purchase_token = get_epic_purchase_token_sync(
        access_token, xsrf, impersonate=impersonate
    )
    methods = get_epic_pci_payment_methods_sync(
        purchase_token, impersonate=impersonate
    )
    return {
        "purchase_token": purchase_token,
        "payment_methods": methods,
    }


def fetch_pci_payment_methods_via_xsrf_sync_with_retry(
    access_token: str,
    *,
    attempts: int = 3,
    delay_sec: float = 1.5,
    impersonate: str = "chrome110",
) -> Dict[str, Any]:
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fetch_pci_payment_methods_via_xsrf_sync(
                access_token, impersonate=impersonate
            )
        except Exception as e:
            last_exc = e
            logging.warning(
                "fetch_pci_payment_methods_via_xsrf attempt %s/%s failed: %s",
                i + 1,
                attempts,
                e,
            )
            if i < attempts - 1:
                time.sleep(delay_sec * (i + 1))
    logging.warning(
        "fetch_pci_payment_methods_via_xsrf giving up after %s attempts",
        attempts,
    )
    if last_exc:
        session_add("PCI: chain (retries)", False, str(last_exc)[:500])
        raise last_exc
    raise RuntimeError("fetch_pci_payment_methods_via_xsrf: failed with no exception recorded")


def build_pci_api_payment_methods_document(
    pci_chain: Dict[str, Any],
    *,
    account_id: Optional[str] = None,
    order_count: Optional[int] = None,
    transactions_total_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Telegram/export envelope for the PCI API path (separate from order-derived methods)."""
    out: Dict[str, Any] = {
        "source": "payment_website_pci_v2",
        "payment_methods": pci_chain.get("payment_methods"),
        "purchase_token": pci_chain.get("purchase_token"),
    }
    if account_id:
        out["account_id"] = account_id
    if order_count is not None:
        out["order_count"] = order_count
    if transactions_total_count is not None:
        out["transactions_total_count"] = transactions_total_count
    return out


_RECOVERY_EXTERNAL_TYPE_LABELS: Dict[str, str] = {
    "psn": "PlayStation Network",
    "xbl": "xBoxLive",
    "nintendo": "Nintendo",
    "nintendo_switch": "Nintendo",
    "twitch": "Twitch",
    "github": "GitHub",
    "steam": "Steam",
    "apple": "Apple",
    "lego": "LEGO",
    "google": "Google",
    "facebook": "Facebook",
    "discord": "Discord",
    "vk": "VK",
}


def _recovery_bool_str(v: bool) -> str:
    return "true" if v else "false"


def _recovery_external_type_block(auth_type: str) -> Dict[str, str]:
    t = (auth_type or "").strip().lower()
    label = _RECOVERY_EXTERNAL_TYPE_LABELS.get(t, (auth_type or "?").upper())
    return {"label": label, "value": t or "unknown"}


def _recovery_connection_date(date_added: Any) -> str:
    if not date_added or date_added == "?":
        return ""
    s = str(date_added)
    fd = format_date_mmddyyyy(s)
    if fd:
        return fd
    try:
        parsed = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ")
        return f"{parsed.strftime('%m/%d/%Y')} · {_english_month_calendar_date(parsed)}"
    except ValueError:
        try:
            parsed = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            return f"{parsed.strftime('%m/%d/%Y')} · {_english_month_calendar_date(parsed)}"
        except ValueError:
            return format_date_mmddyyyy(s[:10]) or (s[:10] if len(s) >= 10 else "")


def _recovery_pick_address_for_recovery(addr_list: Any) -> Dict[str, Any]:
    """Prefer default address, else first entry (same source as Telegram saved addresses)."""
    if not isinstance(addr_list, list) or not addr_list:
        return {}
    for a in addr_list:
        if isinstance(a, dict) and a.get("defaultAddress"):
            return a
    first = addr_list[0]
    return first if isinstance(first, dict) else {}


def _recovery_split_person_name(name_val: Any) -> tuple[str, str]:
    """``\"Jonathan Muñiz\"`` → (\"Jonathan\", \"Muñiz\")."""
    s = (str(name_val or "")).strip()
    if not s:
        return "", ""
    parts = s.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _recovery_connected_accounts(external_auths: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(external_auths, list):
        return out
    for auth in external_auths:
        if not isinstance(auth, dict):
            continue
        auth_type = auth.get("type", "?")
        out.append(
            {
                "type": _recovery_external_type_block(str(auth_type)),
                "displayName": auth.get("externalDisplayName", "") or "",
                "connectionDate": _recovery_connection_date(auth.get("dateAdded")),
                "isConnected": "true",
            }
        )
    return out


def _recovery_country_block(country_code: Optional[str]) -> Dict[str, Any]:
    cc = (country_code or "").strip().upper()
    if not cc:
        return {"value": "", "label": ""}
    labels = {
        "US": "United States",
        "MX": "Mexico",
        "GB": "United Kingdom",
        "DE": "Germany",
        "FR": "France",
        "BR": "Brazil",
        "CA": "Canada",
        "AU": "Australia",
        "ES": "Spain",
        "IT": "Italy",
        "JP": "Japan",
        "KR": "Korea",
        "PL": "Poland",
        "TR": "Turkey",
        "SA": "Saudi Arabia",
        "AE": "United Arab Emirates",
    }
    return {"value": cc, "label": labels.get(cc, cc)}


def _recovery_country_label(country_code: Optional[str]) -> str:
    """English country name only (no value/label JSON)."""
    blk = _recovery_country_block(country_code)
    lab = str(blk.get("label") or "").strip()
    if lab:
        return lab
    return str(blk.get("value") or "").strip()


def _recovery_last4_from_text(text: Optional[str]) -> Optional[str]:
    """``Credit Card - 7214`` or any trailing digit run → last four."""
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"-\s*(\d{4})\s*$", s)
    if m:
        return m.group(1)
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 4:
        return digits[-4:]
    return None


def _recovery_format_order_pm_row(m: Dict[str, Any]) -> str:
    gw = m.get("paymentGatewayType") or "—"
    st = m.get("paymentMethodSubType") or "—"
    bn = m.get("billingAccountName") or "—"
    tc = m.get("transaction_count", 0)
    return f"{gw} · {st} · {bn} · {tc} transaction(s)"


def format_account_payment_methods_telegram_html(
    pm_export: Optional[Dict[str, Any]] = None,
    pci_chain: Optional[Dict[str, Any]] = None,
    *,
    max_lines_per_section: int = 12,
) -> str:
    """Compact payment summary for Account Information (order-derived + PCI billing accounts)."""
    blocks: List[str] = []

    if pm_export and isinstance(pm_export, dict):
        oc = int(pm_export.get("order_count") or 0)
        ttc = int(pm_export.get("transactions_total_count") or 0)
        umc = int(pm_export.get("unique_payment_method_count") or 0)
        blocks.append(
            "<b>From order history</b> · "
            f"orders: {oc} · transactions: {ttc} · unique methods: {umc}"
        )
        methods = pm_export.get("payment_methods") or []
        if not methods:
            blocks.append("<i>No payment methods inferred from orders.</i>")
        else:
            for i, m in enumerate(methods):
                if i >= max_lines_per_section:
                    more = len(methods) - max_lines_per_section
                    blocks.append(f"<i>… and {more} more</i>")
                    break
                if not isinstance(m, dict):
                    continue
                line = _recovery_format_order_pm_row(m)
                blocks.append(f"· {escape_html_telegram(line)}")
    else:
        blocks.append("<i>Order history unavailable — payment summary skipped.</i>")

    blocks.append("")
    pm_root: Any = None
    if pci_chain and isinstance(pci_chain, dict):
        pm_root = pci_chain.get("payment_methods")
    if isinstance(pm_root, dict):
        blocks.append("<b>Saved (PCI / Epic wallet)</b>")
        accts = pm_root.get("billingAccounts") or []
        if not accts:
            blocks.append("<i>No saved billing accounts in PCI response.</i>")
        else:
            for i, b in enumerate(accts):
                if i >= max_lines_per_section:
                    more = len(accts) - max_lines_per_section
                    blocks.append(f"<i>… and {more} more</i>")
                    break
                if not isinstance(b, dict):
                    continue
                line = _recovery_format_pci_billing_row(b)
                blocks.append(f"· {escape_html_telegram(line)}")
    else:
        blocks.append("<i>PCI payment methods unavailable.</i>")

    return "\n".join(blocks)


def _recovery_format_pci_billing_row(b: Dict[str, Any]) -> str:
    gw = b.get("gatewayType") or "—"
    st = b.get("paymentMethodSubType") or "—"
    di = b.get("displayInfo") if isinstance(b.get("displayInfo"), dict) else {}
    name = (
        str(b.get("billingAccountName") or "").strip()
        or str(di.get("displayName") or "").strip()
        or "—"
    )
    status = str(b.get("billingAccountStatus") or "").strip()
    email = str(b.get("billingEmail") or "").strip()
    parts = [f"{gw} · {st} · {name}"]
    if status:
        parts.append(f"({status})")
    if email:
        parts.append(f"· {email}")
    return " ".join(parts)


def _recovery_payment_fields_from_sources(
    pm_export: Optional[Dict[str, Any]],
    pci_envelope: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    PayPal, card last-4, and readable summaries from order-history export and PCI
    ``billingAccounts`` only (saved methods, not ``availablePaymentMethods``).
    """
    paypal_candidates: List[str] = []
    cards_seen: set[str] = set()
    cards: List[str] = []
    order_lines: List[str] = []
    pci_lines: List[str] = []

    def add_card_from_text(text: Optional[str]) -> None:
        last4 = _recovery_last4_from_text(text)
        if last4 and last4 not in cards_seen:
            cards_seen.add(last4)
            cards.append(last4)

    def add_paypal(val: Optional[str]) -> None:
        v = (val or "").strip()
        if "@" in v and v not in paypal_candidates:
            paypal_candidates.append(v)

    if pm_export and isinstance(pm_export, dict):
        for m in pm_export.get("payment_methods") or []:
            if not isinstance(m, dict):
                continue
            order_lines.append(_recovery_format_order_pm_row(m))
            mt = str(m.get("paymentMethodType") or "").upper()
            st = str(m.get("paymentMethodSubType") or "").upper()
            bn = str(m.get("billingAccountName") or "").strip()
            if "PAYPAL" in mt or "PAYPAL" in st:
                add_paypal(bn)
            if (
                any(x in mt for x in ("CARD", "CREDIT", "DEBIT", "XSOLLA"))
                or "CARD" in st
                or "CREDIT" in st
            ):
                add_card_from_text(bn)
            elif len(st) >= 4 and st[-4:].isdigit():
                add_card_from_text(st)

    pm_root: Any = None
    if pci_envelope and isinstance(pci_envelope, dict):
        pm_root = pci_envelope.get("payment_methods")
    if isinstance(pm_root, dict):
        for b in pm_root.get("billingAccounts") or []:
            if not isinstance(b, dict):
                continue
            pci_lines.append(_recovery_format_pci_billing_row(b))
            add_paypal(b.get("billingEmail"))
            pst = str(b.get("paymentMethodSubType") or "").upper()
            bn = str(b.get("billingAccountName") or "")
            if "PAYPAL" in pst:
                add_paypal(bn)
            add_card_from_text(bn)
            if str(b.get("customerAccountType") or "").lower() == "card":
                add_card_from_text(bn)

    return {
        "paypal_email": ", ".join(paypal_candidates) if paypal_candidates else "",
        "paypal_emails": list(paypal_candidates),
        "payment_cards": cards,
        "order_lines": order_lines,
        "pci_lines": pci_lines,
    }


def build_recovery_fields_document(
    account_data: Dict[str, Any],
    email_info: Dict[str, Any],
    account_public_data: Dict[str, Any],
    *,
    order_count: int = 0,
    pm_export: Optional[Dict[str, Any]] = None,
    pci_envelope: Optional[Dict[str, Any]] = None,
    device_auths: Any = None,
    restriction_availability: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Shape aligned with ``recoveryfileds.json``: account, links, name, location,
    and payment hints. Order-history and PCI payloads are **parsed** into
    ``payPalEmail`` (comma-separated if several), ``payPalEmails`` (list),
    ``paymentCards``, and readable line lists (no raw JSON blobs).

    Location and name-on-card style fields use the saved **address** (default or first).
    ``originalAccountEmail`` / ``isEmailChangedByUser`` follow the same rules as the
    Telegram lines (Origin email ✅ + Last Email Change — vs recorded change).
    ``originalDisplayName`` is set only when there is no ``lastDisplayNameChange``.
    ``deviceAuths`` lists public device credentials (created + optional ``lastAccess``
    IP/location/time when the API returns them; no secrets).
    ``restrictionRelink`` stores Help API relink rows (``created``, next available, etc.).
    """
    current_email = (
        account_data.get("email")
        or email_info.get("default_email")
        or ""
    )
    has_email_change = bool(email_info.get("has_last_email_change"))
    # Origin email ✅ in UI = not has_last_email_change; Last Email Change — same case
    original_account_email = (
        current_email if not has_email_change else ""
    )
    display_name = account_data.get("displayName") or ""
    last_dn_iso = account_data.get("lastDisplayNameChange")
    last_dn_disp = format_metadata_date_yyyy_mm_dd(
        last_dn_iso if isinstance(last_dn_iso, str) else None
    )
    display_name_never_changed = (not last_dn_iso) or last_dn_disp == "—"
    original_display_name = display_name if display_name_never_changed else ""
    is_dn_changed_by_user = not display_name_never_changed

    external_auths = account_public_data.get("externalAuths") or []
    addr_list = account_public_data.get("addresses") or []
    addr = _recovery_pick_address_for_recovery(addr_list)
    addr_country = (addr.get("country") or "").strip() if addr else ""
    fn, ln = _recovery_split_person_name(addr.get("name"))
    if not fn and not ln:
        fn = str(account_data.get("name") or "")
        ln = str(account_data.get("lastName") or "")

    user_country_code = (
        addr_country or str(account_data.get("country") or "")
    ).strip()

    pay = _recovery_payment_fields_from_sources(pm_export, pci_envelope)

    out: Dict[str, Any] = {
        "currentEpicAccountEmail": current_email,
        "currentEpicDisplayName": display_name,
        "epicAccountId": account_data.get("id") or "",
        "originalAccountEmail": original_account_email,
        "isEmailChangedByUser": _recovery_bool_str(has_email_change),
        "isUserHaveConnectedAccounts": _recovery_bool_str(len(external_auths) > 0),
        "connectedAccounts": _recovery_connected_accounts(external_auths),
        "restrictionRelink": recovery_normalize_restriction_relink(
            restriction_availability,
            external_auths if isinstance(external_auths, list) else [],
        ),
        "userFirstName": fn,
        "userLastName": ln,
        "userCountry": _recovery_country_label(user_country_code or None),
        "userState": addr.get("region") if addr else None,
        "userCity": (addr.get("city") or "") if addr else "",
        "isDisplayNameChangedByUser": _recovery_bool_str(is_dn_changed_by_user),
        "originalDisplayName": original_display_name,
        "isUserHavePurchase": _recovery_bool_str(order_count > 0),
        "paymentMethodsOrderHistory": pay["order_lines"],
        "savedPaymentMethodsPci": pay["pci_lines"],
        "payPalEmail": pay["paypal_email"],
        "payPalEmails": pay["paypal_emails"],
        "paymentCards": pay["payment_cards"],
        "deviceAuths": recovery_normalize_device_auths(device_auths),
    }
    return out


def _recovery_format_value_for_txt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value).strip()
    if not s:
        return "—"
    low = s.lower()
    if low == "true":
        return "Yes"
    if low == "false":
        return "No"
    return s


def _recovery_txt_pretty_line(label: str, value: Any) -> str:
    return f"{label}: {_recovery_format_value_for_txt(value)}"


def format_recovery_fields_document_as_txt(doc: Dict[str, Any]) -> str:
    """Readable ``.txt``: section order and labels match product export layout."""
    lines: List[str] = []

    lines.append("════════ Account ════════")
    lines.append(
        _recovery_txt_pretty_line("Epic account ID", doc.get("epicAccountId"))
    )
    lines.append(
        _recovery_txt_pretty_line("Current Epic email", doc.get("currentEpicAccountEmail"))
    )
    lines.append(
        _recovery_txt_pretty_line("Original account email", doc.get("originalAccountEmail"))
    )
    lines.append(
        _recovery_txt_pretty_line("Email changed by user", doc.get("isEmailChangedByUser"))
    )
    lines.append("")
    lines.append(
        _recovery_txt_pretty_line(
            "Has connected accounts", doc.get("isUserHaveConnectedAccounts")
        )
    )

    lines.append("")
    lines.append("════════ Displaynames ════════")
    lines.append(
        _recovery_txt_pretty_line("Current display name", doc.get("currentEpicDisplayName"))
    )
    lines.append(
        _recovery_txt_pretty_line(
            "Display name changed by user", doc.get("isDisplayNameChangedByUser")
        )
    )
    lines.append(
        _recovery_txt_pretty_line("Original display name", doc.get("originalDisplayName"))
    )

    lines.append("")
    lines.append("════════ Connected accounts ════════")
    ca = doc.get("connectedAccounts") or []
    if not ca:
        lines.append("  (none)")
    else:
        for a in ca:
            if not isinstance(a, dict):
                continue
            t = a.get("type") or {}
            lab = t.get("label", "?") if isinstance(t, dict) else "?"
            dn = a.get("displayName") or "—"
            cd = a.get("connectionDate") or ""
            if cd:
                lines.append(f"  · {lab}: {dn}  (linked {cd})")
            else:
                lines.append(f"  · {lab}: {dn}")

    lines.append("")
    lines.append("════════ Restriction / relink (Help API) ════════")
    rr = doc.get("restrictionRelink") or {}
    av = rr.get("available") or []
    un = rr.get("unavailable") or []
    lines.append("Available for relink:")
    if not av:
        lines.append("  (none)")
    else:
        for r in av:
            if not isinstance(r, dict):
                continue
            lab = r.get("typeLabel") or r.get("type") or "—"
            dn = r.get("displayName") or "—"
            cd = (r.get("createdDisplay") or "").strip()
            if cd:
                lines.append(f"  · {lab} - {dn} · Created: {cd}")
            else:
                lines.append(f"  · {lab} - {dn}")
    lines.append("")
    lines.append("Unavailable for relink:")
    if not un:
        lines.append("  (none)")
    else:
        for r in un:
            if not isinstance(r, dict):
                continue
            lab = r.get("typeLabel") or r.get("type") or "—"
            dn = r.get("displayName") or "—"
            cd = (r.get("createdDisplay") or "").strip()
            nxt = (r.get("nextAvailable") or "").strip()
            if cd:
                lines.append(f"  · {lab} - {dn} · Created: {cd}")
            else:
                lines.append(f"  · {lab} - {dn}")
            if nxt:
                lines.append(f"      Next available: {nxt}")

    lines.append("")
    lines.append("════════ Profile / location ════════")
    lines.append(_recovery_txt_pretty_line("First name", doc.get("userFirstName")))
    lines.append(_recovery_txt_pretty_line("Last name", doc.get("userLastName")))
    lines.append(_recovery_txt_pretty_line("Country", doc.get("userCountry")))
    lines.append(_recovery_txt_pretty_line("State / region", doc.get("userState")))
    lines.append(_recovery_txt_pretty_line("City", doc.get("userCity")))
    lines.append("")
    lines.append(
        _recovery_txt_pretty_line(
            "Has purchases (order history)", doc.get("isUserHavePurchase")
        )
    )

    lines.append("")
    lines.append("════════ Payment methods (order history) ════════")
    ol = doc.get("paymentMethodsOrderHistory") or []
    if not ol:
        lines.append("  (none)")
    else:
        for row in ol:
            lines.append(f"  · {row}")

    lines.append("")
    lines.append("════════ Saved payment methods (PCI) ════════")
    pl = doc.get("savedPaymentMethodsPci") or []
    if not pl:
        lines.append("  (none)")
    else:
        for row in pl:
            lines.append(f"  · {row}")

    lines.append("")
    lines.append("════════ Wallet ════════")
    cards = doc.get("paymentCards") or []
    pemails = doc.get("payPalEmails")
    if not isinstance(pemails, list):
        pemails = [
            x.strip()
            for x in str(doc.get("payPalEmail") or "").split(",")
            if x.strip()
        ]
    if not pemails:
        lines.append(_recovery_txt_pretty_line("PayPal email", None))
    elif len(pemails) == 1:
        lines.append(_recovery_txt_pretty_line("PayPal email", pemails[0]))
    else:
        lines.append("PayPal emails:")
        for e in pemails:
            lines.append(f"  · {e}")
    if cards:
        lines.append(
            "Saved card last digits: " + ", ".join(str(c) for c in cards)
        )
    else:
        lines.append("Saved card last digits: —")

    lines.append("")
    lines.append("════════ Device auth ════════")
    devs = doc.get("deviceAuths") or []
    if not devs:
        lines.append("  (none)")
    else:
        for i, d in enumerate(devs, 1):
            if not isinstance(d, dict):
                continue
            lines.append(f"  [{i}] Device ID: {_recovery_format_value_for_txt(d.get('deviceId'))}")
            lines.append(
                f"      Account ID: {_recovery_format_value_for_txt(d.get('accountId'))}"
            )
            ua = (d.get("userAgent") or "").strip() or "—"
            lines.append(f"      User-Agent: {ua}")
            lines.append(f"      IP: {_recovery_format_value_for_txt(d.get('ip'))}")
            lines.append(
                f"      Location: {_recovery_format_value_for_txt(d.get('location'))}"
            )
            lines.append(
                f"      Created: {_recovery_format_value_for_txt(d.get('created'))}"
            )
            if "lastAccess" in d:
                lines.append(
                    f"      Last access IP: {_recovery_format_value_for_txt(d.get('lastAccessIp'))}"
                )
                lines.append(
                    f"      Last access location: {_recovery_format_value_for_txt(d.get('lastAccessLocation'))}"
                )
                lines.append(
                    f"      Last access: {_recovery_format_value_for_txt(d.get('lastAccess'))}"
                )

    return "\n".join(lines) + "\n"


# --- Telegram ---

# https://core.telegram.org/bots/api#sendmessage — max 4096 characters
TELEGRAM_MESSAGE_MAX_LENGTH = 4096
# Leave margin for optional "<i>1/N</i>\n" continuation prefix when splitting
TELEGRAM_MESSAGE_SAFE_CHUNK = 3800


def split_telegram_text_chunks(text: str, max_len: int = TELEGRAM_MESSAGE_SAFE_CHUNK) -> List[str]:
    """Split long text for ``sendMessage``. Prefers blank-line breaks, then single newlines."""
    if max_len < 512:
        max_len = TELEGRAM_MESSAGE_SAFE_CHUNK
    if max_len > TELEGRAM_MESSAGE_MAX_LENGTH:
        max_len = TELEGRAM_MESSAGE_MAX_LENGTH
    if not text:
        return [""]
    if len(text) <= max_len:
        return [text]
    chunks: List[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        window = rest[:max_len]
        cut = window.rfind("\n\n")
        if cut < max_len // 3:
            cut = window.rfind("\n")
        if cut < max_len // 3:
            cut = max_len
        if cut <= 0:
            cut = max_len
        chunk = rest[:cut].rstrip()
        if not chunk:
            chunk = rest[:max_len]
            cut = max_len
        chunks.append(chunk)
        rest = rest[cut:].lstrip()
    return chunks


def _balance_telegram_html_chunks(parts: List[str]) -> List[str]:
    """
    Telegram HTML parse mode requires well-formed markup per message.
    When we split long HTML text, we can accidentally cut inside <code>…</code> or <pre>…</pre>.
    This helper re-wraps chunks so each chunk is valid.
    """
    out: List[str] = []
    open_code = False
    open_pre = False
    for part in parts:
        body = part
        if open_pre:
            body = "<pre>" + body
        if open_code:
            body = "<code>" + body

        code_open = body.count("<code>")
        code_close = body.count("</code>")
        pre_open = body.count("<pre>")
        pre_close = body.count("</pre>")

        if code_open > code_close:
            body = body + "</code>"
            open_code = True
        else:
            open_code = False

        if pre_open > pre_close:
            body = body + "</pre>"
            open_pre = True
        else:
            open_pre = False

        out.append(body)
    return out


def fetch_fortnite_item_shop_html(language: str = "en") -> str:
    """
    Today's BR item shop from ``fortnite-api.com`` (public). Returns HTML fragment for Telegram.
    """
    try:
        r = curl_requests.get(
            "https://fortnite-api.com/v2/shop",
            params={"language": language},
            timeout=35,
        )
        epic_log_from_response("fortnite_api_v2_shop", "GET", r.url, r)
    except OSError as e:
        return (
            "<b>🛒 Item Shop</b>\n\n"
            f"<i>Could not reach API: {escape_html_telegram(str(e))}</i>"
        )
    if r.status_code != 200:
        return (
            "<b>🛒 Item Shop</b>\n\n"
            f"<i>HTTP {escape_html_telegram(str(r.status_code))}</i>"
        )
    try:
        payload = r.json()
    except ValueError:
        return "<b>🛒 Item Shop</b>\n\n<i>Invalid JSON.</i>"
    data = payload.get("data")
    if not isinstance(data, dict):
        return "<b>🛒 Item Shop</b>\n\n<i>No shop data.</i>"
    date_s = escape_html_telegram(str(data.get("date") or "?"))
    lines: List[str] = [
        "<b>🛒 Item Shop</b>",
        f"<i>Date (UTC): {date_s}</i>",
        "",
    ]
    entries = data.get("entries") or []
    if not isinstance(entries, list):
        entries = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        price = ent.get("finalPrice")
        names: List[str] = []
        br = ent.get("brItems")
        if isinstance(br, list):
            for it in br:
                if isinstance(it, dict) and (it.get("name") or "").strip():
                    names.append(str(it.get("name")).strip())
        if not names:
            lay = ent.get("layout") or {}
            if isinstance(lay, dict) and (lay.get("name") or "").strip():
                names.append(str(lay.get("name")).strip())
            elif (ent.get("devName") or "").strip():
                names.append(str(ent.get("devName")).strip())
        title = ", ".join(names) if names else "Bundle / offer"
        lines.append(
            f"• {escape_html_telegram(title)} — <b>{escape_html_telegram(str(price))}</b> V-Bucks"
        )
    if len(lines) <= 3:
        lines.append("<i>No entries in this shop response.</i>")
    return "\n".join(lines)


def send_telegram_message_chunks(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    max_chunk: int = TELEGRAM_MESSAGE_SAFE_CHUNK,
    parse_mode: Optional[str] = "HTML",
    reply_markup: Any = None,
    reply_to_message_id: Optional[int] = None,
    disable_notification: Optional[bool] = None,
) -> None:
    """
    Send a message, splitting into several ``sendMessage`` calls if text exceeds Telegram limits.
    ``reply_markup`` is attached only to the last part. ``reply_to_message_id`` only to the first.
    """
    parts = split_telegram_text_chunks(text, max_chunk)
    if parse_mode == "HTML":
        parts = _balance_telegram_html_chunks(parts)
    n = len(parts)
    for i, part in enumerate(parts):
        body = part
        if n > 1:
            if parse_mode == "HTML":
                body = f"<i>{i + 1}/{n}</i>\n{part}"
            else:
                body = f"({i + 1}/{n})\n{part}"
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": body}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if disable_notification is not None:
            payload["disable_notification"] = disable_notification
        if reply_to_message_id is not None and i == 0:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None and i == n - 1:
            payload["reply_markup"] = reply_markup
        try:
            bot.send_message(**payload)
        except Exception as e:
            # If Telegram rejects HTML entities, retry as plain text (strip tags).
            msg = str(e)
            if parse_mode == "HTML" and ("can't parse entities" in msg or "CantParseEntities" in msg):
                raw = payload.get("text") or ""
                raw = re.sub(r"<[^>]*>", "", raw)
                raw = raw.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                payload2 = dict(payload)
                payload2["text"] = raw
                payload2.pop("parse_mode", None)
                bot.send_message(**payload2)
            else:
                raise


def escape_html_telegram(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_code(text: str | None) -> str:
    """Monospace copy-on-tap (HTML parse mode)."""
    return f"<code>{escape_html_telegram(text)}</code>"


def format_epic_oauth_tokens_telegram_html(user: Any) -> str:
    """
    All OAuth secrets from :class:`~epic_auth.EpicUser` for copy/paste (sensitive).
    Includes both access-token paths (exchange vs device_code) and refresh / id_token if present.
    """
    at = (getattr(user, "access_token", None) or "").strip()
    dc = (getattr(user, "device_code_flow_access_token", None) or "").strip()
    rt = (getattr(user, "refresh_token", None) or "").strip()
    raw = getattr(user, "raw", None)
    if not isinstance(raw, dict):
        raw = {}
    id_tok = (raw.get("id_token") or "").strip()

    lines: List[str] = [
        "<b>━━━━━━━━━━━</b>",
        "<b>🔑 OAuth tokens</b> <i>(sensitive)</i>",
        "<b>━━━━━━━━━━━</b>",
    ]
    tt = (getattr(user, "token_type", None) or "").strip()
    if tt:
        lines.append(f"<b>Token type</b> {escape_html_telegram(tt)}")
    ei = getattr(user, "expires_in", None)
    if ei is not None:
        lines.append(f"<b>expires_in</b> {escape_html_telegram(str(ei))}")
    ea = (getattr(user, "expires_at", None) or "").strip()
    if ea:
        lines.append(f"<b>expires_at</b> {tg_code(ea)}")
    rfa = (getattr(user, "refresh_expires_at", None) or "").strip()
    if rfa:
        lines.append(f"<b>refresh_expires_at</b> {tg_code(rfa)}")
    if len(lines) > 3:
        lines.append("")

    lines.append("<b>Access token</b> <i>(account APIs)</i>")
    lines.append(tg_code(at) if at else "<i>—</i>")
    lines.append("")
    lines.append("<b>Access token</b> <i>(prod-fn)</i>")
    if dc and at and dc == at:
        lines.append("<i>Same value as exchange access token above.</i>")
    else:
        # Don't display launcher eg1 tokens (very long).
        if dc.startswith("eg1~"):
            lines.append("<i>Hidden</i>")
        else:
            lines.append(tg_code(dc) if dc else "<i>—</i>")
    lines.append("")
    lines.append("<b>Refresh token</b>")
    lines.append(tg_code(rt) if rt else "<i>—</i>")
    if id_tok:
        lines.append("")
        lines.append("<b>ID token</b> <i>(if returned by OAuth)</i>")
        lines.append(tg_code(id_tok))

    return "\n".join(lines)


def escape_markdown(text: str) -> str:
    escape_chars = [
        "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!",
    ]
    for char in escape_chars:
        text = text.replace(char, f"\\{char}")
    return text
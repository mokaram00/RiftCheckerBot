"""Locker image generation. Color palettes from ``thems.json``; layout under ``themes/``."""
import argparse
import colorsys
import json
import math
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from utils import format_local_now_dual

GRADIENT_SMOOTHNESS = 1.0
SHADOW_INTENSITY = 120
BACKGROUND_FADE = 0.5
ACCENT_SATURATION = 1.0

PROJECT_ROOT = Path(__file__).resolve().parent
ATHENA_PATH = PROJECT_ROOT

_THEMS_CONFIG: Optional[Dict[str, Any]] = None


def _thems_json_path() -> Path:
    return PROJECT_ROOT / "thems.json"


def load_thems_config() -> Dict[str, Any]:
    """Load ``THEMES`` (tuples) and ``THEME_ORDER`` from ``thems.json``."""
    global _THEMS_CONFIG
    if _THEMS_CONFIG is None:
        with open(_thems_json_path(), encoding="utf-8") as f:
            raw = json.load(f)
        themes_in = raw["THEMES"]
        themes: Dict[str, Dict[str, Tuple]] = {}
        for name, pal in themes_in.items():
            themes[name] = {
                "username_start": tuple(pal["username_start"]),
                "username_end": tuple(pal["username_end"]),
                "background_start": tuple(pal["background_start"]),
                "background_end": tuple(pal["background_end"]),
                "shadow": tuple(pal["shadow"]),
                "accent": tuple(pal["accent"]),
            }
        order = raw.get("THEME_ORDER") or list(themes.keys())
        _THEMS_CONFIG = {"THEMES": themes, "THEME_ORDER": order}
    return _THEMS_CONFIG


def get_theme_palette(theme_key: str) -> Dict[str, Tuple]:
    cfg = load_thems_config()
    themes = cfg["THEMES"]
    if theme_key in themes:
        return themes[theme_key]
    if "inferno" in themes:
        return themes["inferno"]
    return next(iter(themes.values()))


def get_theme_order() -> List[str]:
    return list(load_thems_config()["THEME_ORDER"])

# Error logging setup
def log_error(error_message):
    """Log error to error.txt file with timestamp"""
    timestamp = format_local_now_dual(with_time=True)
    log_entry = f"[{timestamp}] {error_message}\n"
    with open("error.txt", "a", encoding="utf-8") as f:
        f.write(log_entry)

TYPE_TO_JSON = {
    "skins": "outfit.json",
    "pickaxes": "pickaxe.json",
    "gliders": "glider.json",
    "backpacks": "backpack.json",
    "emotes": "emote.json",
    "auras": "aura.json",
    "cars": "cars.json",
    "contrails": "contrail.json",
    "emoji": "emoji.json",
    "music": "music.json",
    "pets": "pet.json",
    "petcarriers": "petcarrier.json",
    "shoes": "shoe.json",
    "sidekicks": "sidekick.json",
    "sprays": "spray.json",
    "toys": "toy.json",
    "tracks": "tracks.json",
    "wraps": "wrap.json",
}

TYPE_TO_FOLDER = {
    "skins": "Outfit",
    "pickaxes": "Pickaxe",
    "gliders": "Glider",
    "backpacks": "Backpack",
    "emotes": "Emote",
    "auras": "Aura",
    "cars": "Cars",
    "contrails": "Contrail",
    "emoji": "Emoji",
    "music": "Music",
    "pets": "Pet",
    "petcarriers": "PetCarrier",
    "shoes": "Shoe",
    "sidekicks": "Sidekick",
    "sprays": "Spray",
    "toys": "Toy",
    "tracks": "Tracks",
    "wraps": "Wrap",
}


class CosmeticItem:
    def __init__(self, data: Dict[str, Any]):
        self.id = data.get("id", "")
        self.name = data.get("name", "")
        self.type = data.get("type", {}).get("value", "")
        self.images = data.get("images", {}).get("icon", "")
        self.rarity = data.get("rarity", {}).get("value", "common")
        # Extract series value for cars
        # For cars, the type value is "body", not "cars"
        self.series = (
            data.get("series", {}).get("value", "")
            if self.type.lower() == "body"
            else ""
        )
        self.rarity_value = self.rarity
        self.cosmetic_id = self.id
        self.small_icon = self.images
        self.is_banner = False  # Default value, adjust as needed
        # File path for local image loading
        self.file_path = None


class ProcessedImage:
    def __init__(self, image, rarity: str, series: str, name: str, item_id: str):
        self.image = image
        self.rarity = rarity
        self.series = series
        self.name = name
        self.item_id = item_id


class ImageGenRequest:
    def __init__(self, data: Dict[str, Any]):
        self.account_id = data.get("account_id", "")
        self.locker_data = data.get("locker_data", {})
        self.data_dir = data.get("data_dir", "")
        self.style = data.get("style", "")  # Style name from command line
        self.style_id = data.get("style_id", 0)


def load_cosmetic_data(cosmetic_type: str, data_dir: str) -> Dict[str, CosmeticItem]:
    json_file = TYPE_TO_JSON.get(cosmetic_type, "")
    if not json_file:
        raise ValueError(f"Unknown cosmetic type: {cosmetic_type}")

    file_path = Path(data_dir) / "Athena" / json_file
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise Exception(f"Failed to read {file_path}: {str(e)}")

    result = {}
    if isinstance(data, list):
        for item in data:
            cosmetic_item = CosmeticItem(item)
            if cosmetic_item.id:
                result[cosmetic_item.id.lower()] = cosmetic_item
    return result


# Style mappings for special skin variants (same as Go version)
STYLE_MAPPINGS = {
    "cid_029_athena_commando_f_halloween": {
        "Mat3": "Pink Ghoul Trooper",
    },
    "cid_030_athena_commando_m_halloween": {
        "Mat1": "Purple Skull Trooper",
    },
    "cid_017_athena_commando_m": {
        "Stage2": "Aerial Assault Trooper Black & Gold",
    },
    "cid_028_athena_commando_f": {
        "Mat3": "Renegade Raider Black & Gold",
    },
    "cid_116_athena_commando_m_carbideblack": {
        "Stage5": "Stage 5 Omega",
    },
    "cid_694_athena_commando_m_catburglar": {
        "Stage4": "Golden Midas",
    },
    "cid_693_athena_commando_m_buffcat": {
        "Stage4": "Golden Meowscles",
    },
    "cid_691_athena_commando_f_tntina": {
        "Stage7": "Golden TNTina",
    },
    "cid_690_athena_commando_f_photographer": {
        "Stage4": "Golden Skye",
    },
    "cid_701_athena_commando_m_bananaagent": {
        "Stage4": "Golden Agent Peely",
    },
    "cid_315_athena_commando_m_teriyakifish": {
        "Stage3": "World Cup Fishtick",
    },
    "cid_971_athena_commando_m_jupiter_s0z6m": {
        "Mat2": "Mate Black Masterchief",
    },
}


def get_style_name(cosmetic_id, variant_type, variant_value):
    """Map API stage/material keys to display names (case-insensitive keys)."""
    cid = cosmetic_id.lower()
    if cid not in STYLE_MAPPINGS:
        return variant_value
    styles = STYLE_MAPPINGS[cid]
    if variant_value in styles:
        return styles[variant_value]
    vv = str(variant_value)
    for k, v in styles.items():
        if k.lower() == vv.lower():
            return v
    return variant_value


def resolve_cosmetic_display_name(cosmetic: Any) -> str:
    """Prefer STYLE_MAPPINGS label when the account owns that variant (e.g. Golden Midas)."""
    cid = getattr(cosmetic, "cosmetic_id", None) or getattr(cosmetic, "id", None) or ""
    cid = str(cid).strip()
    base = getattr(cosmetic, "name", None) or ""
    if not cid:
        return base
    cid_l = cid.lower()
    if cid_l not in STYLE_MAPPINGS:
        return base
    unlocked = getattr(cosmetic, "unlocked_styles", None)
    if not unlocked:
        return base
    if not isinstance(unlocked, (list, tuple, set)):
        unlocked = [unlocked]
    unlocked_norm = {str(u) for u in unlocked}
    unlocked_lower = {str(u).lower() for u in unlocked}
    for variant_key, label in STYLE_MAPPINGS[cid_l].items():
        if variant_key in unlocked_norm or variant_key.lower() in unlocked_lower:
            return label
    return base

def load_exclusive_items() -> List[str]:
    for base in (ATHENA_PATH, PROJECT_ROOT):
        exclusive_path = Path(base) / "exclusive.txt"
        if exclusive_path.is_file():
            try:
                with open(exclusive_path, "r", encoding="utf-8") as f:
                    return [line.strip() for line in f.readlines() if line.strip()]
            except OSError:
                continue
    return []


def load_most_wanted_items() -> List[str]:
    for base in (ATHENA_PATH, PROJECT_ROOT):
        most_wanted_path = Path(base) / "most_wanted.txt"
        if most_wanted_path.is_file():
            try:
                with open(most_wanted_path, "r", encoding="utf-8") as f:
                    return [line.strip() for line in f.readlines() if line.strip()]
            except OSError:
                continue
    return []


def draw_gradient_text(
    gradient_type,
    draw,
    position,
    text,
    font,
    fill=(255, 255, 255),
    username_start=None,
    username_end=None,
):
    """
    Draw text with smooth gradient that flows across the entire text,
    not coloring each character individually.
    Now using just two colors with smooth transition.
    """
    x, y = position
    text_width = font.getbbox(text)[2] - font.getbbox(text)[0]
    
    if gradient_type == 0:
        # white text (no gradient)
        draw.text((x, y), text, font=font, fill=(255, 255, 255))
        
    elif gradient_type == 1:
        # Two-color rainbow gradient - smooth transition across text
        # Define start and end colors for a more controlled gradient
        start_hue = 0.0    # Red
        end_hue = 0.33     # Green
        char_positions = []
        current_x = 0
        for char in text:
            char_width = font.getbbox(char)[2] - font.getbbox(char)[0]
            char_positions.append((char, current_x, char_width))
            current_x += char_width
        
        # Draw each character with interpolated color between start and end hues
        for char, char_x, char_width in char_positions:
            # Calculate position in overall text (0.0 to 1.0)
            char_center = char_x + char_width / 2
            pos = char_center / text_width if text_width > 0 else 0
            # Interpolate between start and end hue
            hue = start_hue + (end_hue - start_hue) * pos
            rgb = colorsys.hsv_to_rgb(hue, 1.0, 1.0)  # Full saturation and brightness
            color = tuple(int(c * 255) for c in rgb)
            draw.text((x + char_x, y), char, font=font, fill=color)
                
    elif gradient_type == 2:
        # Two-color golden gradient - from light to dark gold
        start_rgb = (255, 215, 0)  # Gold
        end_rgb = (184, 134, 11)   # Dark goldenrod
        char_positions = []
        current_x = 0
        for char in text:
            char_width = font.getbbox(char)[2] - font.getbbox(char)[0]
            char_positions.append((char, current_x, char_width))
            current_x += char_width
        
        # Draw each character with interpolated golden color
        for char, char_x, char_width in char_positions:
            # Calculate position in overall text (0.0 to 1.0)
            char_center = char_x + char_width / 2
            pos = char_center / text_width if text_width > 0 else 0
            # Interpolate between start and end colors
            r = int(start_rgb[0] + (end_rgb[0] - start_rgb[0]) * pos)
            g = int(start_rgb[1] + (end_rgb[1] - start_rgb[1]) * pos)
            b = int(start_rgb[2] + (end_rgb[2] - start_rgb[2]) * pos)
            color = (r, g, b)
            draw.text((x + char_x, y), char, font=font, fill=color)
            
    elif gradient_type == 3:
        start = username_start if username_start is not None else (255, 255, 255)
        end = username_end if username_end is not None else (220, 220, 220)

        char_positions = []
        current_x = 0

        for char in text:
            bbox = font.getbbox(char)
            char_width = bbox[2] - bbox[0]
            char_positions.append((char, current_x, char_width))
            current_x += char_width

        for char, char_x, char_width in char_positions:
            char_center = char_x + char_width / 2
            pos = char_center / text_width if text_width > 0 else 0

            r = int(start[0] + (end[0] - start[0]) * pos)
            g = int(start[1] + (end[1] - start[1]) * pos)
            b = int(start[2] + (end[2] - start[2]) * pos)

            draw.text(
                (x + char_x, y),
                char,
                font=font,
                fill=(r, g, b)
            )




def get_rarity_image_path(style_name, cosmetic):
    """
    Get the appropriate rarity image path for a cosmetic item.

    :param style_name: Layout folder under ``themes/`` (e.g. ``raika``).
    :param cosmetic: The cosmetic item object
    :return: Path to the rarity image
    """
    ctype = (getattr(cosmetic, "type", None) or "").lower()
    # For cars, rarity is series-based
    if ctype == "body":
        # For cars, use the series-based naming convention
        series_val = getattr(cosmetic, "series", None)
        if series_val:
            # If series exists, use it for the filename
            # Convert "Star Wars Series" to "Star_Wars_Series"
            series_parts = series_val.split()
            if series_parts and series_parts[-1].lower() == "series":
                # Remove the last "Series" word for cleaner naming
                series_parts = series_parts[:-1]
            formatted_series = "_".join(series_parts)
        else:
            # If no series, fallback to rarity value
            formatted_series = cosmetic.rarity_value.capitalize()

        image_name = f"{formatted_series}_Series_-_Rarity_-_Fortnite.png"
        return str(ATHENA_PATH / "themes" / style_name / "cars" / image_name)
    else:
        # For other cosmetics, use the standard rarity values
        return str(ATHENA_PATH / "themes" / style_name / "rarity" / f"{cosmetic.rarity_value.lower()}.png")


def _cache_special_file(filename: str) -> str:
    """Resolve special skin icon under project ``cache/``; try alternate filenames."""
    primary = PROJECT_ROOT / "cache" / filename
    if primary.is_file():
        return str(primary)
    alternates = {
        "omega_stage5.png": ("omega_max.png",),
        "jupiter.png": ("black_masterchief.png",),
    }
    for alt in alternates.get(filename, ()):
        ap = PROJECT_ROOT / "cache" / alt
        if ap.is_file():
            return str(ap)
    return str(primary)


_RARITY_RGBA_CACHE: Dict[str, Image.Image] = {}


def _cached_rarity_rgba(path: str) -> Image.Image:
    """Load each rarity frame PNG once; ``render_raika_style`` opens it per cosmetic otherwise."""
    img = _RARITY_RGBA_CACHE.get(path)
    if img is None:
        img = Image.open(path).convert("RGBA")
        _RARITY_RGBA_CACHE[path] = img
    return img


def _background_gradient_overlay_rgba(
    image_width: int,
    image_height: int,
    bg_s: Tuple[int, int, int],
    bg_e: Tuple[int, int, int],
) -> Image.Image:
    """Vertical RGB + alpha fade — same intent as the old per-row ``ImageDraw.line`` loop, much faster."""
    g = Image.linear_gradient("L").transpose(Image.ROTATE_90)
    g = g.resize((image_width, image_height), Image.Resampling.BILINEAR)
    rgb = ImageOps.colorize(g, bg_s, bg_e)

    def _alpha_from_l(l: int) -> int:
        pos = l / 255.0
        return int(40 * (1 - pos * 0.5))

    a_band = g.point(_alpha_from_l, mode="L")
    return Image.merge("RGBA", (*rgb.split(), a_band))


def render_raika_style(
    header: str,
    user_data: Any,
    arr: list,
    nametosave: str,
    data_dir: str = None,
    file_map: dict = None,
    cache=None,
    theme_key: str = "inferno",
) -> None:
    # calculating cosmetics per row
    cosmetic_per_row = 6
    total_cosmetics = len(arr)
    num_rows = math.ceil(total_cosmetics / cosmetic_per_row)
    if total_cosmetics > 30:
        num_rows = int(math.sqrt(total_cosmetics))
        cosmetic_per_row = math.ceil(total_cosmetics / num_rows)

        while cosmetic_per_row * num_rows < total_cosmetics:
            num_rows += 1
            cosmetic_per_row = math.ceil(total_cosmetics / num_rows)

    # setup for our image, thumbnails
    padding = 30
    thumbnail_width = 128
    thumbnail_height = 128
    image_width = int(cosmetic_per_row * thumbnail_width)
    image_height = int(thumbnail_height + 5 + thumbnail_width * num_rows + 180)
    pal = get_theme_palette(theme_key)
    bg_s = pal["background_start"]
    bg_e = pal["background_end"]
    un_s = pal["username_start"]
    un_e = pal["username_end"]
    shadow_list = list(pal["shadow"])
    shadow_color = tuple(shadow_list[:3] + [SHADOW_INTENSITY])
    accent = pal["accent"]
    font_path = str(ATHENA_PATH / "themes" / "raika" / "font.ttf")
    font_size = 16
    font = ImageFont.truetype(font_path, font_size)
    image = Image.new("RGB", (image_width, image_height), (255, 240, 245))  # Light pink background
    
    # Create background with customizable gradient colors
    image = Image.new("RGB", (image_width, image_height), bg_s)  # Start with theme color

    background_overlay = _background_gradient_overlay_rgba(
        image_width, image_height, bg_s, bg_e
    )
    image.paste(background_overlay, (0, 0), background_overlay)

    # custom background
    custom_background_path = f"users/backgrounds/{user_data['ID']}.png"
    if Path(custom_background_path).is_file():
        custom_background = Image.open(custom_background_path).resize(
            (image_width, image_height), Image.Resampling.BILINEAR
        )
        image.paste(custom_background, (0, 0))

    current_row = 0
    current_column = 0
    sortarray = [
        "mythic",
        "legendary",
        "dark",
        "slurp",
        "starwars",
        "marvel",
        "lava",
        "frozen",
        "gaminglegends",
        "shadow",
        "icon",
        "dc",
        "epic",
        "rare",
        "uncommon",
        "common",
    ]
    arr.sort(key=lambda x: sortarray.index(x.rarity_value))

    # had some issues with exclusives rendering in wrong order, so i'm sorting them
    exclusive_cosmetics: List[str] = []
    popular_cosmetics: List[str] = []
    for base in (ATHENA_PATH, PROJECT_ROOT):
        ep = Path(base) / "exclusive.txt"
        if ep.is_file():
            with open(ep, "r", encoding="utf-8") as f:
                exclusive_cosmetics = [i.strip().lower() for i in f.readlines() if i.strip()]
            break
    for base in (ATHENA_PATH, PROJECT_ROOT):
        mw = Path(base) / "most_wanted.txt"
        if mw.is_file():
            with open(mw, "r", encoding="utf-8") as f:
                popular_cosmetics = [i.strip().lower() for i in f.readlines() if i.strip()]
            break

    special_items = {
        "CID_029_Athena_Commando_F_Halloween": _cache_special_file("pink_ghoul.png"),
        "CID_030_Athena_Commando_M_Halloween": _cache_special_file("purple_skull.png"),
        "CID_116_Athena_Commando_M_CarbideBlack": _cache_special_file("omega_stage5.png"),
        "CID_694_Athena_Commando_M_CatBurglar": _cache_special_file("gold_midas.png"),
        "CID_693_Athena_Commando_M_BuffCat": _cache_special_file("gold_cat.png"),
        "CID_691_Athena_Commando_F_TNTina": _cache_special_file("gold_tntina.png"),
        "CID_690_Athena_Commando_F_Photographer": _cache_special_file("gold_skye.png"),
        "CID_701_Athena_Commando_M_BananaAgent": _cache_special_file("gold_peely.png"),
        "CID_315_Athena_Commando_M_TeriyakiFish": _cache_special_file("worldcup_fish.png"),
        "CID_971_Athena_Commando_M_Jupiter_S0Z6M": _cache_special_file("jupiter.png"),
        "Golden Meowscles": _cache_special_file("gold_cat.png"),
        "Golden Midas": _cache_special_file("gold_midas.png"),
        "Golden TNTina": _cache_special_file("gold_tntina.png"),
        "Golden Skye": _cache_special_file("gold_skye.png"),
        "Golden Peely": _cache_special_file("gold_peely.png"),
        "CID_028_Athena_Commando_F": _cache_special_file("og_rene.png"),
        "CID_017_Athena_Commando_M": _cache_special_file("og_aat.png"),
    }

    # Bucket mythic vs popular vs regular. Mythic/exclusive display and STYLE_MAPPINGS
    # are already resolved in main() via rarity_value; do not re-derive from cosmetic_id
    # here (suffixes like _Stage1 on exclusive bases would wrongly mythic every row).
    def _exclusive_list_order(c: Any) -> float:
        cid = c.cosmetic_id.lower()
        if cid in exclusive_cosmetics:
            return exclusive_cosmetics.index(cid)
        if "_" in c.cosmetic_id:
            base_id = "_".join(c.cosmetic_id.split("_")[:-1]).lower()
            if base_id in exclusive_cosmetics:
                return exclusive_cosmetics.index(base_id)
        return float("inf")

    exclusive_items = []
    popular_items = []
    regular_items = []

    for cosmetic in arr:
        make_mythic = cosmetic.rarity_value.lower() == "mythic"
        is_popular = cosmetic.cosmetic_id.lower() in popular_cosmetics

        if make_mythic:
            exclusive_items.append(cosmetic)
        elif is_popular:
            popular_items.append(cosmetic)
        else:
            regular_items.append(cosmetic)

    exclusive_items.sort(key=_exclusive_list_order)
    
    # Sort popular/most-wanted items — always show them right after exclusives
    if popular_items:
        popular_items.sort(
            key=lambda cosmetic: popular_cosmetics.index(cosmetic.cosmetic_id.lower())
            if cosmetic.cosmetic_id.lower() in popular_cosmetics else float('inf')
        )
    
    arr = exclusive_items + popular_items + regular_items
    draw = ImageDraw.Draw(image)

    font_header_large = ImageFont.truetype(font_path, 70)
    font_header_title = ImageFont.truetype(font_path, 40)
    wanted_star_rgba = (
        Image.open(ATHENA_PATH / "cosmetic_icons" / "WantedStar.png")
        .resize((128, 128), Image.BILINEAR)
        .convert("RGBA")
    )
    font_footer_date = ImageFont.truetype(font_path, 28)
    font_footer_user = ImageFont.truetype(font_path, 36)
    font_footer_info = ImageFont.truetype(font_path, 16)
    font_by_size: Dict[int, Any] = {}

    # top
    # Map "Skins" header to "Outfits" file and handle other plural/singular mappings
    icon_mapping = {
        "Skins": "Outfits",
        "Sprays": "Spray",
        "Emotes": "Emote",
        "Gliders": "Glider",
        "Pickaxes": "Pickaxe",
        "Backpacks": "Backpack",
        "Pets": "Pet",
        "PetCarriers": "PetCarrier",
        "Auras": "Aura",
        "Cars": "Car",
        "Shoes": "Shoe",
        "Sidekicks": "Sidekick",
        "Music": "Music",
        "Contrails": "Contrail",
        "Wraps": "Wrap",
        "Toys": "Toy",
        "Tracks": "Track",
    }
    icon_file = icon_mapping.get(header, header)
    icon_logo = Image.open(ATHENA_PATH / "cosmetic_icons" / f"{icon_file}.png")
    icon_logo.thumbnail((thumbnail_width, thumbnail_height))
    image.paste(icon_logo, (5, 0), mask=icon_logo)
    draw.text(
        (thumbnail_width + 12, 14),
        "{}".format(len(arr)),
        font=font_header_large,
        fill=(0, 0, 0, 200),  # Darker shadow for better contrast
    )  # shadow
    draw.text(
        (thumbnail_width + 12, 82),
        "{}".format(header),
        font=font_header_title,
        fill=(0, 0, 0, 200),  # Darker shadow
    )  # shadow
    draw.text(
        (thumbnail_width + 8, 10),
        "{}".format(len(arr)),
        font=font_header_large,
        fill=(255, 255, 255),  # White count for better visibility
    )
    draw.text(
        (thumbnail_width + 8, 78),
        "{}".format(header),
        font=font_header_title,
        fill=(255, 255, 255),  # White header text for better visibility
    )

    # Progress tracking for large datasets
    processed_count = 0
    total_items = len(arr)

    for cosmetic in arr:
        # Increment counter and log progress
        processed_count += 1
        special_icon = False
        is_banner = cosmetic.is_banner
        photo = None
        
        # Check for special icon by cosmetic ID or style name
        special_key = None
        if cosmetic.cosmetic_id in special_items:
            special_key = cosmetic.cosmetic_id
        elif cosmetic.name in special_items:
            special_key = cosmetic.name
        
        if (
            cosmetic.rarity_value.lower() == "mythic" and special_key
        ):
            special_icon = True
            icon_path = special_items[special_key]
            if Path(icon_path).exists():
                try:
                    photo = Image.open(icon_path)
                except Exception as e:
                    special_icon = False
            else:
                special_icon = False
        else:
            photo = None
            fp = getattr(cosmetic, "file_path", None)
            if fp and Path(fp).exists():
                try:
                    photo = Image.open(fp)
                except Exception as e:
                    log_error(f"Error loading image for {cosmetic.cosmetic_id}: {str(e)}")
            elif cache is not None:
                small = getattr(cosmetic, "small_icon", None) or ""
                cid = getattr(cosmetic, "cosmetic_id", None) or getattr(cosmetic, "id", "")
                photo = cache.get_cosmetic_icon_from_cache(small, cid)
        # Handle case where photo is None by creating a placeholder
        if photo is None:
            photo = Image.new(
                "RGBA", (thumbnail_width, thumbnail_height), (128, 128, 128, 255)
            )

        if is_banner:
            scaled_width = int(photo.width * 1.5)
            scaled_height = int(photo.height * 1.5)
            photo = photo.resize(
                (scaled_width, scaled_height), Image.Resampling.LANCZOS
            )
            x_offset = 32
            y_offset = 10

            rarity_image_path = get_rarity_image_path("raika", cosmetic)
            new_img = _cached_rarity_rgba(rarity_image_path).copy()
            new_img.paste(photo, (x_offset, y_offset), mask=photo)
            photo = new_img
            photo.thumbnail((thumbnail_width, thumbnail_height))
        else:
            rarity_image_path = get_rarity_image_path("raika", cosmetic)
            new_img = _cached_rarity_rgba(rarity_image_path).resize(photo.size)
            # Convert photo to RGBA if needed to fix transparency issues
            if photo.mode != "RGBA":
                photo = photo.convert("RGBA")
            new_img.paste(photo, mask=photo)
            photo = new_img
            photo.thumbnail(
                (thumbnail_width, thumbnail_height)
            )  # black box for cosmetic name
        box = Image.new("RGBA", (128, 28), (0, 0, 0, 100))
        photo.paste(box, (0, new_img.size[1] - 28), mask=box)

        if header != "Exclusives" and cosmetic.cosmetic_id.lower() in popular_cosmetics:
            photo.paste(wanted_star_rgba, (0, 0), wanted_star_rgba)

        x = thumbnail_width * current_column
        y = thumbnail_width + thumbnail_height * current_row
        image.paste(photo, (x, y))

        name = resolve_cosmetic_display_name(cosmetic).upper()
        max_text_width = thumbnail_width - 10
        max_text_height = 20

        # fixed font size
        font_size = 16
        offset = 6
        while True:
            font = font_by_size.get(font_size)
            if font is None:
                font = ImageFont.truetype(font_path, font_size)
                font_by_size[font_size] = font
            bbox = draw.textbbox((0, 0), name, font=font)
            name_width = bbox[2] - bbox[0]
            name_height = bbox[3] - bbox[1]

            if name_width > max_text_width or name_height > max_text_height:
                font_size -= 1
                offset += 0.5
            else:
                break

        # cosmetic name
        bbox = draw.textbbox((0, 0), name, font=font)
        name_width = bbox[2] - bbox[0]
        draw.text(
            (
                x + (thumbnail_width - name_width) // 2,
                y + (thumbnail_height - padding + offset),
            ),
            name,
            font=font,
            fill=(255, 255, 255),
        )

        # make the cosmetics show ordered in rows(cosmetic_per_row is hardcoded)
        current_column += 1
        if current_column >= cosmetic_per_row:
            current_row += 1
            current_column = 0

    # Modern footer design with gradient theme
    footer_height = 180
    footer_y = image_height - footer_height
    
    # Create modern gradient background for footer using theme colors
    for i in range(footer_height):
        # Create smooth gradient using theme colors
        pos = i / footer_height
        if pos < 0.5:
            # Light to medium transition
            ratio = pos / 0.5
            r = int(bg_s[0] + ((bg_s[0] + bg_e[0]) // 2 - bg_s[0]) * ratio)
            g = int(bg_s[1] + ((bg_s[1] + bg_e[1]) // 2 - bg_s[1]) * ratio)
            b = int(bg_s[2] + ((bg_s[2] + bg_e[2]) // 2 - bg_s[2]) * ratio)
        else:
            # Medium to dark transition
            ratio = (pos - 0.5) / 0.5
            mid_r = (bg_s[0] + bg_e[0]) // 2
            mid_g = (bg_s[1] + bg_e[1]) // 2
            mid_b = (bg_s[2] + bg_e[2]) // 2
            r = int(mid_r + (bg_e[0] - mid_r) * ratio)
            g = int(mid_g + (bg_e[1] - mid_g) * ratio)
            b = int(mid_b + (bg_e[2] - mid_b) * ratio)
        
        alpha = int(30 * (1 - pos * 0.3))  # Subtle fade effect
        color = (r, g, b, alpha)
        draw.rectangle([(0, footer_y + i), (image_width, footer_y + i + 1)], fill=color)
    
    # Add modern separator line with theme accent color
    draw.line([(20, footer_y), (image_width - 20, footer_y)], fill=accent + (180,), width=2)
    
    # Custom logo with better positioning
    custom_logo_path = f"users/logos/{user_data['ID']}.png"
    logo_size = 120
    
    if Path(custom_logo_path).is_file():
        custom_logo = Image.open(custom_logo_path).resize(
            (logo_size, logo_size), Image.Resampling.LANCZOS
        )
        # Add logo shadow effect
        shadow = Image.new("RGBA", (logo_size + 4, logo_size + 4), (0, 0, 0, 50))
        shadow.paste(custom_logo, (2, 2), mask=custom_logo)
        image.paste(shadow, (30, footer_y + 25), mask=shadow)
        image.paste(custom_logo, (30, footer_y + 25), mask=custom_logo)
    else:
        # Original logo with shadow
        logo = Image.open(ATHENA_PATH / "img" / "logo.png").resize((logo_size, logo_size), Image.Resampling.LANCZOS)
        shadow = Image.new("RGBA", (logo_size + 4, logo_size + 4), (0, 0, 0, 50))
        shadow.paste(logo, (2, 2), mask=logo)
        image.paste(shadow, (30, footer_y + 25), mask=shadow)
        image.paste(logo, (30, footer_y + 25), mask=logo)
    
    # Date with modern styling
    current_date = format_local_now_dual(with_time=False)
    date_x = logo_size + 60
    
    # Date shadow
    draw.text(
        (date_x + 2, footer_y + 35),
        current_date,
        font=font_footer_date,
        fill=(0, 0, 0, 128),
    )
    # Date text
    draw.text(
        (date_x, footer_y + 33),
        current_date,
        font=font_footer_date,
        fill=(200, 200, 200),
    )
    
    # Username with gradient effect
    username_text = f"@{user_data['username']}"
    username_y = footer_y + 75
    
    # Username shadow with customizable color
    draw.text(
        (date_x + 2, username_y + 2),
        username_text,
        font=font_footer_user,
        fill=shadow_color,  # Customizable shadow color from theme
    )
    # Username gradient text
    draw_gradient_text(
        user_data["gradient_type"],
        draw,
        (date_x, username_y),
        username_text,
        font=font_footer_user,
        username_start=un_s,
        username_end=un_e,
    )
    
    # Add decorative elements with theme accent color
    # Small dots pattern using accent color
    for i in range(5):
        x = date_x + i * 15
        y = footer_y + 120
        # Use theme accent color for dots
        draw.ellipse([(x, y), (x + 6, y + 6)], fill=accent + (200,))
    
    # Stats/Info text with theme accent color
    info_text = "bltnm.store"
    draw.text(
        (date_x, footer_y + 140),
        info_text,
        font=font_footer_info,
        fill=accent + (220,),  # Accent color text
    )
    
    image.save(nametosave)
# Style renderers mapping
STYLE_RENDERERS = {
    0: render_raika_style,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request_json", help="Path to locker request json")
    parser.add_argument("--style", type=int, default=0)
    parser.add_argument("--theme", default="inferno", help="Palette key from thems.json")
    parser.add_argument("--output", default="output.png")
    args = parser.parse_args()

    # ---- load request json ----
    request_json_path = Path(args.request_json)
    if not request_json_path.is_file():
        print(f"[X] Request JSON not found:\n{args.request_json}")
        return

    with open(request_json_path, "r", encoding="utf-8") as f:
        request_data = json.load(f)

    # Get style from JSON if available, otherwise use command line arg
    style = request_data.get("style", args.style)
    if isinstance(style, str):
        style = int(style) if style.isdigit() else args.style

    theme_key = request_data.get("theme_key", args.theme)
    if not isinstance(theme_key, str):
        theme_key = str(theme_key)

    # Get generation options from JSON
    generate_exclusives = request_data.get("generate_exclusives", True)
    generate_most_wanted = request_data.get("generate_most_wanted", True)

    # ---- user data ----
    user_data = request_data.get(
        "user_data",
        {
            "ID": request_data.get("account_id", "unknown"),
            "username": "BLTNM",
            "gradient_type": 3,
            "epic_badge_active": False,
            "epic_badge": False,
            "alpha_tester_3_badge_active": False,
            "alpha_tester_3_badge": False,
            "alpha_tester_2_badge_active": False,
            "alpha_tester_2_badge": False,
            "alpha_tester_1_badge_active": False,
            "alpha_tester_1_badge": False,
        },
    )

    # Extract account ID and data directory from request
    account_id = request_data.get("account_id", "unknown")
    data_dir = request_data.get("data_dir", ".")

    # Get locker data which contains the types and item IDs
    locker_data = request_data.get("locker_data", {})

    # Create account directory like Go version does
    account_dir = Path(data_dir) / "accounts" / "hits" / account_id
    account_dir.mkdir(parents=True, exist_ok=True)

    # Load exclusive items for filtering
    exclusive_items = load_exclusive_items()

    # Load most wanted items for filtering
    most_wanted_items = load_most_wanted_items()

    # Collect all exclusive items across all cosmetic types
    all_exclusive_items = []

    # Collect all most wanted items across all cosmetic types
    all_most_wanted_items = []

    # Process each cosmetic type to find exclusive and most wanted items
    for cosmetic_type, item_ids in locker_data.items():
        if not item_ids:  # Skip empty types
            continue

        # Get the JSON file for this type
        json_file = TYPE_TO_JSON.get(cosmetic_type)
        if not json_file:
            continue

        # Load cosmetics for this type
        json_path = ATHENA_PATH / json_file
        if not json_path.is_file():
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            continue

        # Build file map for this cosmetic type (like in Go version)
        folder = TYPE_TO_FOLDER.get(cosmetic_type)
        if not folder:
            continue

        folder_path = ATHENA_PATH / folder
        file_map = {}

        if folder_path.is_dir():
            try:
                for png in folder_path.iterdir():
                    if png.suffix.lower() == ".png":
                        file_id = png.stem.lower()
                        file_map[file_id] = str(png)
            except Exception as e:
                pass
        else:
            continue

        # Filter items to only those in the locker AND in exclusive list
        exclusive_cosmetics = []
        # Filter items to only those in the locker AND in most wanted list
        most_wanted_cosmetics = []
        # Extract the ID part after the colon if present (e.g., "AthenaCharacter:cid_xxx" -> "cid_xxx")
        item_id_set = {item_id.split(':')[-1].lower() for item_id in item_ids}
        exclusive_id_set = {item_id.lower() for item_id in exclusive_items}
        most_wanted_id_set = {item_id.lower() for item_id in most_wanted_items}

        for item in data:
            cosmetic = CosmeticItem(item)
            if cosmetic.id and cosmetic.id.lower() in item_id_set:
                # Debug logging to check series value
                if cosmetic.series:
                    print(f"Processing car {cosmetic.id} with series: {cosmetic.series}")
                # Set file path for local image loading
                cosmetic.file_path = file_map.get(cosmetic.id.lower())

                # Check if item is exclusive with new logic (same as epic_auth.py)
                if cosmetic.id.lower() in exclusive_id_set:
                    make_mythic = True
                    
                    # Check if this character requires specific style validation
                    if cosmetic.id.lower() in STYLE_MAPPINGS:
                        make_mythic = False  # Reset to False, will only be True if valid style found
                        
                        # Get required styles for this character
                        required_styles = STYLE_MAPPINGS[cosmetic.id.lower()]
                        
                        # Check if character has any styles in locker_data
                        has_styles = "styles" in locker_data and cosmetic.id.lower() in locker_data["styles"]
                        
                        if has_styles:
                            style_info = locker_data["styles"][cosmetic.id.lower()]
                            owned_styles = set()
                            
                            # Collect all owned styles
                            if "stage_styles" in style_info:
                                owned_styles.update(key.lower() for key in style_info["stage_styles"].keys())
                            if "variant_styles" in style_info:
                                owned_styles.update(key.lower() for key in style_info["variant_styles"].keys())
                            
                            # Check if any owned style matches required styles
                            for required_style in required_styles.keys():
                                if required_style.lower() in owned_styles:
                                    make_mythic = True
                                    
                                    # Create separate cosmetic items for each valid special style
                                    if "stage_styles" in style_info:
                                        # Find the actual key in stage_styles (case-insensitive)
                                        actual_key = None
                                        for k in style_info["stage_styles"].keys():
                                            if k.lower() == required_style.lower():
                                                actual_key = k
                                                break
                                        
                                        if actual_key is not None:
                                            style_name = style_info["stage_styles"][actual_key]
                                            style_cosmetic = CosmeticItem(item)
                                            style_cosmetic.name = get_style_name(
                                                cosmetic.id, "stage", actual_key
                                            ) or style_name
                                            style_cosmetic.cosmetic_id = f"{cosmetic.id}_{required_style}"  # Unique ID for style
                                            
                                            # Set file path for special item image using existing special_items mapping
                                            # Create the same mapping as in render_raika_style
                                            _cache_dir = PROJECT_ROOT / "cache"
                                            special_items_mapping = {
                                                "CID_029_Athena_Commando_F_Halloween": str(_cache_dir / "pink_ghoul.png"),
                                                "CID_030_Athena_Commando_M_Halloween": str(_cache_dir / "purple_skull.png"),
                                                "CID_116_Athena_Commando_M_CarbideBlack": str(_cache_dir / "omega_stage5.png"),
                                                "CID_694_Athena_Commando_M_CatBurglar": str(_cache_dir / "gold_midas.png"),
                                                "CID_693_Athena_Commando_M_BuffCat": str(_cache_dir / "gold_cat.png"),
                                                "CID_691_Athena_Commando_F_TNTina": str(_cache_dir / "gold_tntina.png"),
                                                "CID_690_Athena_Commando_F_Photographer": str(_cache_dir / "gold_skye.png"),
                                                "CID_701_Athena_Commando_M_BananaAgent": str(_cache_dir / "gold_peely.png"),
                                                "CID_315_Athena_Commando_M_TeriyakiFish": str(_cache_dir / "worldcup_fish.png"),
                                                "CID_971_Athena_Commando_M_Jupiter_S0Z6M": str(_cache_dir / "black_masterchief.png"),
                                                "CID_028_Athena_Commando_F": str(_cache_dir / "og_rene.png"),
                                                "CID_017_Athena_Commando_M": str(_cache_dir / "og_aat.png"),
                                            }
                                            
                                            # Get the special image path using the cosmetic ID
                                            special_image_path = special_items_mapping.get(cosmetic.id)
                                            if special_image_path and Path(special_image_path).exists():
                                                style_cosmetic.file_path = special_image_path
                                                print(f"Using special image for {style_name}: {special_image_path}")
                                            else:
                                                # Fallback to regular image
                                                style_cosmetic.file_path = file_map.get(cosmetic.id.lower())
                                            
                                            style_cosmetic.rarity_value = "mythic"
                                            exclusive_cosmetics.append(style_cosmetic)
                                    
                                    # Skip adding base cosmetic if we have valid style
                                    make_mythic = False
                                    break
                    
                    # Apply mythic rarity if conditions are met
                    if make_mythic:
                        cosmetic.rarity_value = "mythic"
                        exclusive_cosmetics.append(cosmetic)

                # Check if item is most wanted
                if cosmetic.id.lower() in most_wanted_id_set:
                    most_wanted_cosmetics.append(cosmetic)
                
                # For regular processing, also apply mythic rarity to exclusives
                if cosmetic.id.lower() in exclusive_id_set:
                    cosmetic.rarity_value = "mythic"

        all_exclusive_items.extend(exclusive_cosmetics)
        all_most_wanted_items.extend(most_wanted_cosmetics)

    # Generate separate exclusive image if we have exclusive items and it's enabled
    if generate_exclusives and all_exclusive_items:
        # Save in account directory with Go-style naming
        output_filename = str(account_dir / f"{account_id}_exclusives.png")

        # Use the appropriate renderer
        renderer = STYLE_RENDERERS.get(style, render_raika_style)

        try:
            renderer(
                header="Exclusives",
                user_data=user_data,
                arr=all_exclusive_items,
                nametosave=output_filename,
                theme_key=theme_key,
            )
        except Exception as e:
            log_error(f"Error during exclusive image generation: {str(e)}")
    elif not generate_exclusives:
        pass
    else:
        pass

    # Generate separate most wanted image if we have most wanted items and it's enabled
    if generate_most_wanted and all_most_wanted_items:
        # Save in account directory with Go-style naming
        output_filename = str(account_dir / f"{account_id}_most_wanted.png")

        # Use the appropriate renderer
        renderer = STYLE_RENDERERS.get(style, render_raika_style)

        try:
            renderer(
                header="Popular",
                user_data=user_data,
                arr=all_most_wanted_items,
                nametosave=output_filename,
            )
            print(f"[✓] Most wanted image generated: {output_filename}")
        except Exception as e:
            log_error(f"Error during most wanted image generation: {str(e)}")
    elif not generate_most_wanted:
        pass
    else:
        pass
    # Process each cosmetic type separately for regular images
    processed_types = 0
    for cosmetic_type, item_ids in locker_data.items():
        if not item_ids:  # Skip empty types
            continue

        # Get the JSON file for this type
        json_file = TYPE_TO_JSON.get(cosmetic_type)
        if not json_file:
            continue

        # Load cosmetics for this type
        json_path = ATHENA_PATH / json_file
        if not json_path.is_file():
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            continue

        # Build file map for this cosmetic type (like in Go version)
        folder = TYPE_TO_FOLDER.get(cosmetic_type)
        if not folder:
            continue

        folder_path = ATHENA_PATH / folder
        file_map = {}

        if folder_path.is_dir():
            try:
                for png in folder_path.iterdir():
                    if png.suffix.lower() == ".png":
                        file_id = png.stem.lower()
                        file_map[file_id] = str(png)
            except Exception as e:
                pass
        else:
            continue

        # Filter items to only those in the locker
        render_items = []
        # Extract the ID part after the colon if present (e.g., "AthenaCharacter:cid_xxx" -> "cid_xxx")
        item_id_set = {item_id.split(':')[-1].lower() for item_id in item_ids}

        for item in data:
            cosmetic = CosmeticItem(item)
            if cosmetic.id and cosmetic.id.lower() in item_id_set:
                # Debug logging to check series value
                if cosmetic.series:
                    print(f"Processing car {cosmetic.id} with series: {cosmetic.series}")
                
                # Check if this is a skin with special styles
                if cosmetic_type == "skins" and "styles" in locker_data:
                    styles_data = locker_data["styles"]
                    if cosmetic.id.lower() in styles_data:
                        style_info = styles_data[cosmetic.id.lower()]
                        if "stage_styles" in style_info:
                            stage_styles = style_info["stage_styles"]
                            # Create separate cosmetic items for each special style
                            for stage_key, style_name in stage_styles.items():
                                # Clone the cosmetic item
                                style_cosmetic = CosmeticItem(item)
                                style_cosmetic.name = style_name  # Use style name instead of base name
                                style_cosmetic.cosmetic_id = f"{cosmetic.id}_{stage_key}"  # Unique ID for style
                                
                                # Set file path for special item image using existing special_items mapping
                                # Create the same mapping as in render_raika_style
                                _cache_dir = PROJECT_ROOT / "cache"
                                special_items_mapping = {
                                    "CID_029_Athena_Commando_F_Halloween": str(_cache_dir / "pink_ghoul.png"),
                                    "CID_030_Athena_Commando_M_Halloween": str(_cache_dir / "purple_skull.png"),
                                    "CID_116_Athena_Commando_M_CarbideBlack": str(_cache_dir / "omega_stage5.png"),
                                    "CID_694_Athena_Commando_M_CatBurglar": str(_cache_dir / "gold_midas.png"),
                                    "CID_693_Athena_Commando_M_BuffCat": str(_cache_dir / "gold_cat.png"),
                                    "CID_691_Athena_Commando_F_TNTina": str(_cache_dir / "gold_tntina.png"),
                                    "CID_690_Athena_Commando_F_Photographer": str(_cache_dir / "gold_skye.png"),
                                    "CID_701_Athena_Commando_M_BananaAgent": str(_cache_dir / "gold_peely.png"),
                                    "CID_315_Athena_Commando_M_TeriyakiFish": str(_cache_dir / "worldcup_fish.png"),
                                    "CID_971_Athena_Commando_M_Jupiter_S0Z6M": str(_cache_dir / "black_masterchief.png"),
                                    "CID_028_Athena_Commando_F": str(_cache_dir / "og_rene.png"),
                                    "CID_017_Athena_Commando_M": str(_cache_dir / "og_aat.png"),
                                }
                                
                                # Get the special image path using the cosmetic ID
                                special_image_path = special_items_mapping.get(cosmetic.id)
                                if special_image_path and Path(special_image_path).exists():
                                    style_cosmetic.file_path = special_image_path
                                    print(f"Using special image for {style_name}: {special_image_path}")
                                else:
                                    # Fallback to regular image
                                    style_cosmetic.file_path = file_map.get(cosmetic.id.lower())
                                
                                # Apply mythic rarity to exclusive styles with new logic
                                if cosmetic.id.lower() in exclusive_id_set:
                                    make_mythic = True
                                    
                                    # Check if this character requires specific style validation
                                    if cosmetic.id.lower() in STYLE_MAPPINGS:
                                        make_mythic = False  # Reset to False, will only be True if valid style found
                                        
                                        # Get required styles for this character
                                        required_styles = STYLE_MAPPINGS[cosmetic.id.lower()]
                                        
                                        # Check if the current style matches required styles
                                        if stage_key.lower() in required_styles.keys():
                                            make_mythic = True
                                    
                                    # Apply mythic rarity if conditions are met
                                    if make_mythic:
                                        style_cosmetic.rarity_value = "mythic"
                                
                                render_items.append(style_cosmetic)
                            continue  # Skip adding the base cosmetic if we have styles
                
                # Set file path for local image loading
                cosmetic.file_path = file_map.get(cosmetic.id.lower())
                
                # Apply mythic rarity to exclusive items with new logic (same as epic_auth.py)
                if cosmetic.id.lower() in exclusive_id_set:
                    make_mythic = True
                    
                    # Check if this character requires specific style validation
                    if cosmetic.id.lower() in STYLE_MAPPINGS:
                        make_mythic = False  # Reset to False, will only be True if valid style found
                        
                        # Get required styles for this character
                        required_styles = STYLE_MAPPINGS[cosmetic.id.lower()]
                        
                        # Check if character has any styles in locker_data
                        has_styles = "styles" in locker_data and cosmetic.id.lower() in locker_data["styles"]
                        
                        if has_styles:
                            style_info = locker_data["styles"][cosmetic.id.lower()]
                            owned_styles = set()
                            
                            # Collect all owned styles
                            if "stage_styles" in style_info:
                                owned_styles.update(key.lower() for key in style_info["stage_styles"].keys())
                            if "variant_styles" in style_info:
                                owned_styles.update(key.lower() for key in style_info["variant_styles"].keys())
                            
                            # Check if any owned style matches required styles
                            for required_style in required_styles.keys():
                                if required_style.lower() in owned_styles:
                                    make_mythic = True
                                    break
                    
                    # Apply mythic rarity if conditions are met
                    if make_mythic:
                        cosmetic.rarity_value = "mythic"
                
                render_items.append(cosmetic)
        
        if not render_items:
            continue

        # Generate output filename for this type - save in account directory with Go-style naming
        output_filename = str(account_dir / f"{account_id}_{cosmetic_type}_locker.png")

        # Use the appropriate renderer
        renderer = STYLE_RENDERERS.get(style, render_raika_style)

        try:
            renderer(
                header=cosmetic_type.capitalize(),
                user_data=user_data,
                arr=render_items,
                nametosave=output_filename,
                theme_key=theme_key,
            )
            processed_types += 1
        except Exception as e:
            log_error(f"Error during image generation for {cosmetic_type}: {str(e)}")

    print(f"Completed processing {processed_types} cosmetic types")


if __name__ == "__main__":
    main()
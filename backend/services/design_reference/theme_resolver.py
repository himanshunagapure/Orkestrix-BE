"""
In-process theme resolution via the vendored ui-ux-pro-max design engine.

Pinned release: v2.11.0 (see backend/design/design_reference/VENDORED_FROM.md).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from services.gemini_service import GeminiGenerateConfig, sync_generate

logger = logging.getLogger(__name__)

VENDORED_TAG = "v2.11.0"
VENDOR_ROOT = (
    Path(__file__).resolve().parents[2] / "design" / "design_reference"
)
_ENGINE_DIR = VENDOR_ROOT / "engine"
_THEME_SYNTH_MODEL = "gemini-3.5-flash-lite"

# Phrases that mean "use our brand / skip automated palette lookup"
_BRAND_SIGNAL_RE = re.compile(
    r"\b("
    r"match\s+our\s+brand|"
    r"use\s+our\s+brand|"
    r"our\s+brand\s+colou?rs?|"
    r"brand\s+guidelines?|"
    r"company\s+brand|"
    r"existing\s+brand\s+(?:palette|colors?|colours?)"
    r")\b",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b")

# Generic reasoning fallback markers from design_system._apply_reasoning
_GENERIC_PATTERN = "Hero + Features + CTA"


def _ensure_engine_on_path() -> None:
    """Vendored scripts use ``from core import ...`` — keep engine dir on sys.path."""
    engine_path = str(_ENGINE_DIR)
    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)


def _load_generator_class():
    _ensure_engine_on_path()
    # Import after path inject; keep local to avoid import-time side effects when unused.
    from design_system import DesignSystemGenerator  # type: ignore

    return DesignSystemGenerator


def _expand_short_hex(value: str) -> str:
    """Normalize #RGB → #RRGGBB."""
    if re.fullmatch(r"#[0-9A-Fa-f]{3}", value):
        return "#" + "".join(ch * 2 for ch in value[1:])
    return value


def detect_explicit_theme_signal(prompt: str) -> Optional[Dict[str, Any]]:
    """
    Short-circuit when the user states an exact hex or asks to match brand.

    Returns a theme_preferences dict if lookup should be skipped, else None.
    """
    text = prompt or ""
    hex_match = _HEX_RE.search(text)
    brand_match = _BRAND_SIGNAL_RE.search(text)

    if hex_match:
        primary = _expand_short_hex(hex_match.group(0))
        # Prefer the first hex as primary; if a second hex exists, treat as accent.
        all_hexes = [_expand_short_hex(m.group(0)) for m in _HEX_RE.finditer(text)]
        prefs: Dict[str, Any] = {
            "primary": primary,
            "source": "explicit_hex",
            "antiPatterns": [],
            "usedGenericReasoningFallback": False,
        }
        if len(all_hexes) > 1:
            prefs["accent"] = all_hexes[1]
        if brand_match:
            prefs["source"] = "explicit_hex_and_brand"
        return prefs

    if brand_match:
        return {
            "source": "explicit_brand",
            "antiPatterns": [],
            "usedGenericReasoningFallback": False,
            # No invented palette — IR generation must respect brand without a lookup.
        }

    return None


# ---------------------------------------------------------------------------
# Rich style vocabulary detector
# ---------------------------------------------------------------------------
# Maps keywords/phrases that appear in user prompts → canonical direction names.
# When a user explicitly describes a visual style, we trust them over BM25.
# Each entry: (regex_pattern, canonical_direction, confidence 1-3)
# confidence 3 = almost certain (exact style name), 1 = weak hint.
_STYLE_VOCAB: List[Tuple[str, str, int]] = [
    # Exact direction names or close synonyms
    (r"\bsketch[- ](?:style|based|inspired|ui|design|look)\b", "Sketch Hand-Drawn", 3),
    (r"\bhand[- ]drawn\b", "Sketch Hand-Drawn", 3),
    (r"\bsketchbook\b", "Sketch Hand-Drawn", 3),
    (r"\bglassmorphi(?:sm|c)\b", "Glassmorphism", 3),
    (r"\bneumorphi(?:sm|c)\b", "Neumorphism", 3),
    (r"\bsoft[-\s]ui\b", "Neumorphism", 2),
    (r"\bclaymorphi(?:sm|c)\b", "Claymorphism", 3),
    (r"\b(?:neo[-\s])?brutali(?:sm|st)\b", "Neo Brutalism", 3),
    (r"\bbrutalism\b", "Brutalism", 3),
    (r"\bbento[-\s](?:box|grid|layout)\b", "Bento Box Grid", 3),
    (r"\bbento\b", "Bento Box Grid", 2),
    (r"\bglassmorphi?sm\b", "Glassmorphism", 3),
    (r"\bvaporwave\b", "Vaporwave", 3),
    (r"\bcyberpunk\b", "Cyberpunk UI", 3),
    (r"\bsci[-\s]fi[-\s](?:ui|hud|interface)\b", "HUD / Sci-Fi FUI", 3),
    (r"\bhud[-\s](?:style|ui)\b", "HUD / Sci-Fi FUI", 3),
    (r"\bmaterial[-\s](?:you|design|3|md3)\b", "Material You (MD3)", 3),
    (r"\bretro[-\s]futuri(?:sm|st|stic)\b", "Retro-Futurism", 3),
    (r"\bvintage\b", "Vintage Analog / Retro Film", 2),
    (r"\bfilm[-\s](?:grain|look|style)\b", "Vintage Analog / Retro Film", 3),
    (r"\banalog[-\s](?:style|look)\b", "Vintage Analog / Retro Film", 2),
    (r"\bpixel[-\s]art\b", "Pixel Art", 3),
    (r"\b8[-\s]bit\b", "Pixel Art", 2),
    (r"\bauror[ai]\b", "Gradient Mesh / Aurora Evolved", 2),
    (r"\bgradient[-\s]mesh\b", "Gradient Mesh / Aurora Evolved", 3),
    (r"\beditori(?:al|al[-\s]grid)\b", "Editorial Grid / Magazine", 3),
    (r"\bmagazine[-\s](?:style|layout|look)\b", "Editorial Grid / Magazine", 3),
    (r"\bswiss[-\s](?:style|design|modernism)\b", "Swiss Modernism 2.0", 3),
    (r"\bbauhaus\b", "Bauhaus (包豪斯)", 3),
    (r"\bbiomimetic\b", "Biomimetic / Organic 2.0", 3),
    (r"\borganic[-\s](?:design|ui|shapes)\b", "Biomimetic / Organic 2.0", 2),
    (r"\bbiophilic\b", "Organic Biophilic", 3),
    (r"\bparallax[-\s](?:scroll|story)\b", "Parallax Storytelling", 3),
    (r"\bkinetic[-\s]typ(?:e|ography)\b", "Kinetic Typography", 3),
    (r"\bgen[-\s]?z[-\s](?:chaos|maximal)\b", "Gen Z Chaos / Maximalism", 3),
    (r"\bmaximali(?:sm|st)\b", "Gen Z Chaos / Maximalism", 2),
    (r"\bmemphis[-\s](?:design|style)\b", "Memphis Design", 3),
    (r"\bdark[-\s]mode\b", "Dark Mode (OLED)", 2),
    (r"\boled[-\s](?:dark|black)\b", "Dark Mode (OLED)", 3),
    (r"\bminimali(?:sm|st|stic)\b", "Minimalism & Swiss Style", 2),
    (r"\bclean[-\s]minimal\b", "Minimalism & Swiss Style", 2),
    (r"\bflat[-\s]design\b", "Flat Design", 3),
    (r"\bspatial[-\s](?:ui|design|computing)\b", "Spatial UI (VisionOS)", 3),
    (r"\bvision[-\s]os\b", "Spatial UI (VisionOS)", 3),
    (r"\be[-\s]ink\b", "E-Ink / Paper", 3),
    (r"\bpaper[-\s](?:texture|like|style|look)\b", "E-Ink / Paper", 2),
    (r"\btactile[-\s](?:ui|digital)\b", "Tactile Digital / Deformable UI", 3),
    (r"\bdeformable[-\s]ui\b", "Tactile Digital / Deformable UI", 3),
    (r"\bskeuomorph(?:ic|ism)\b", "Skeuomorphism", 3),
    (r"\bliquid[-\s]glass\b", "Liquid Glass", 3),
    (r"\b3d[-\s](?:product|preview)\b", "3D Product Preview", 3),
    (r"\bhyperreali(?:sm|st|stic)\b", "3D & Hyperrealism", 3),
    (r"\bmotion[-\s]driven\b", "Motion-Driven", 3),
    (r"\bvoice[-\s]first\b", "Voice-First Multimodal", 3),
    (r"\bdata[-\s]dense\b", "Data-Dense Dashboard", 2),
    (r"\bheatmap[-\s](?:style|ui)\b", "Heat Map & Heatmap Style", 3),
    (r"\bexecutive[-\s]dashboard\b", "Executive Dashboard", 3),
    (r"\bpredictive[-\s]analytics\b", "Predictive Analytics", 3),
    (r"\breal[-\s]time[-\s]monitor\b", "Real-Time Monitoring", 3),
    (r"\bfinancial[-\s]dashboard\b", "Financial Dashboard", 3),
    (r"\bsales[-\s]intelligence\b", "Sales Intelligence Dashboard", 3),
    (r"\bai[-\s]native[-\s]ui\b", "AI-Native UI", 3),
    (r"\bstorytelling[-\s]driven\b", "Storytelling-Driven", 3),
    (r"\bconversion[-\s]optim\b", "Conversion-Optimized", 2),
    (r"\bterm(?:inal|inal[-\s]ui|inal[-\s]style)\b", "Terminal CLI", 3),
    (r"\bcli[-\s](?:style|look|ui)\b", "Terminal CLI", 3),
    (r"\bmonosp(?:ace|aced)[-\s](?:ui|terminal)\b", "Terminal CLI", 2),
    (r"\bdefi[-\s](?:ui|dashboard)\b", "Bitcoin DeFi", 2),
    (r"\bcrypto[-\s](?:ui|dashboard|wallet)\b", "Bitcoin DeFi", 2),
    (r"\bvibrant[-\s](?:block|color|gradient)\b", "Vibrant & Block-based", 2),
]

# Compiled patterns: (compiled_re, direction, confidence)
_STYLE_VOCAB_COMPILED: Optional[List[Tuple[Any, str, int]]] = None


def _get_compiled_style_vocab():
    global _STYLE_VOCAB_COMPILED
    if _STYLE_VOCAB_COMPILED is None:
        _STYLE_VOCAB_COMPILED = [
            (re.compile(pat, re.IGNORECASE), direction, conf)
            for pat, direction, conf in _STYLE_VOCAB
        ]
    return _STYLE_VOCAB_COMPILED


def detect_rich_style_intent(prompt: str) -> Optional[Dict[str, Any]]:
    """
    Detect when the user explicitly describes a visual/design style.

    Returns a partial theme_preferences dict (source="style_vocabulary") when
    one or more high-confidence style terms are found, so the design engine can
    be skipped.  Returns None when no clear style intent is found.

    Only triggers at confidence >= 2 (avoids weak hints like "mood board" or
    "vintage" when they appear incidentally).
    """
    text = prompt or ""
    best_direction: Optional[str] = None
    best_conf = 0

    for pattern, direction, conf in _get_compiled_style_vocab():
        if pattern.search(text):
            if conf > best_conf:
                best_conf = conf
                best_direction = direction

    if best_conf >= 2 and best_direction:
        logger.info(
            "Rich style intent detected in prompt → direction=%s (conf=%d)",
            best_direction,
            best_conf,
        )
        return {
            "source": "style_vocabulary",
            "direction": best_direction,
            "antiPatterns": [],
            "usedGenericReasoningFallback": False,
        }
    return None


def _extract_hex_colors(text: str) -> List[str]:
    """Pull #RGB / #RRGGBB tokens from a styles.csv color field."""
    if not text:
        return []
    found: List[str] = []
    for m in re.finditer(r"#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})\b", text):
        found.append(_expand_short_hex(m.group(0)))
    return found


def _parse_fonts_from_ai_keywords(ai_keywords: str) -> Optional[Dict[str, str]]:
    """
    Best-effort typography parse from styles.csv ``AI Prompt Keywords``.

    Example: "Typography: Kalam Bold for headings (h1)... Caveat for body"
    """
    if not ai_keywords:
        return None
    display = None
    body = None
    # "X for headings" / "X for heading" / "heading font X"
    m = re.search(
        r"([A-Z][A-Za-z0-9][A-Za-z0-9 \-]{1,40}?)\s+(?:Bold|Regular|Medium|Light)?\s*"
        r"for\s+headings?",
        ai_keywords,
        re.IGNORECASE,
    )
    if m:
        display = re.sub(r"\s+(Bold|Regular|Medium|Light)$", "", m.group(1).strip(), flags=re.I)
    m = re.search(
        r"([A-Z][A-Za-z0-9][A-Za-z0-9 \-]{1,40}?)\s+(?:Bold|Regular|Medium|Light)?\s*"
        r"for\s+body",
        ai_keywords,
        re.IGNORECASE,
    )
    if m:
        body = re.sub(r"\s+(Bold|Regular|Medium|Light)$", "", m.group(1).strip(), flags=re.I)
    if display or body:
        return {"display": display or body or "", "body": body or display or ""}
    return None


def _find_style_csv_row(direction: str) -> Optional[Dict[str, str]]:
    """
    Find the styles.csv row for a locked direction name.

    Matches exact enum name, case-insensitive, CSV ``(Mobile)`` variants, and
    reverse direction aliases (enum name → engine CSV name).
    """
    import csv

    styles_path = VENDOR_ROOT / "data" / "styles.csv"
    if not direction or not styles_path.is_file():
        return None

    target = direction.lower().strip()
    # Reverse aliases: enum "Sketch Hand-Drawn" ↔ csv "Sketch Hand-Drawn (Mobile)"
    reverse_aliases = {v.lower(): k for k, v in _DIRECTION_ALIASES.items()}
    alias_csv_name = reverse_aliases.get(target)

    best: Optional[Dict[str, str]] = None
    best_score = -1

    with open(styles_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("Style Category") or "").strip()
            if not name:
                continue
            nl = name.lower()
            score = 0
            if nl == target:
                score = 100
            elif alias_csv_name and nl == alias_csv_name:
                score = 95
            elif target in nl or nl in target:
                score = 50 + min(len(nl), 40)
            elif alias_csv_name and (alias_csv_name in nl or nl in alias_csv_name):
                score = 45 + min(len(nl), 40)
            if score > best_score:
                best_score = score
                best = dict(row)

    return best if best_score >= 45 else None


def _hex_luminance(hex_color: str) -> float:
    """Relative luminance 0–1 for #RRGGBB (for background vs accent heuristics)."""
    h = _expand_short_hex(hex_color).lstrip("#")
    if len(h) != 6:
        return 0.5
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def _theme_prefs_from_style_row(
    row: Dict[str, str],
    *,
    direction: str,
) -> Dict[str, Any]:
    """Build theme_preferences tokens from a styles.csv row (palette locked to that style)."""
    primary_field = row.get("Primary Colors") or ""
    secondary_field = row.get("Secondary Colors") or ""
    primary_hexes = _extract_hex_colors(primary_field)
    secondary_hexes = _extract_hex_colors(secondary_field)

    prefs: Dict[str, Any] = {
        "source": "style_vocabulary",
        "direction": direction,
        "antiPatterns": _split_constraint_list(row.get("Do Not Use For")),
        "usedGenericReasoningFallback": False,
        "productCategory": "",
        "pattern": "",
    }

    if primary_hexes:
        prefs["primary"] = primary_hexes[0]
        if len(primary_hexes) > 1:
            prefs["text"] = primary_hexes[1]

    if secondary_hexes:
        by_lum = sorted(secondary_hexes, key=_hex_luminance)
        lightest = by_lum[-1]
        if _hex_luminance(lightest) >= 0.70:
            prefs["background"] = lightest
        # Accent: darkest usable secondary that isn't the paper/background
        for h in by_lum:
            if h != prefs.get("background") and _hex_luminance(h) < 0.65:
                prefs["accent"] = h
                break
        for h in secondary_hexes:
            if h not in (prefs.get("background"), prefs.get("accent")):
                prefs["secondary"] = h
                break
        darkest = by_lum[0]
        # Only infer text from secondary when we have a paper/light surface
        # (avoids neon accents becoming "text" for Glassmorphism-style rows).
        if (
            prefs.get("background")
            and _hex_luminance(darkest) < 0.35
            and "text" not in prefs
        ):
            prefs["text"] = darkest

    # Primary field often has no hex (e.g. Glassmorphism rgba) — borrow from secondary.
    if "primary" not in prefs and secondary_hexes:
        candidates = [
            h for h in secondary_hexes
            if h != prefs.get("background") and _hex_luminance(h) < 0.75
        ]
        prefs["primary"] = (candidates or secondary_hexes)[0]
        if len(candidates) > 1 and "accent" not in prefs:
            prefs["accent"] = candidates[1]

    effects = _split_constraint_list(row.get("Effects & Animation"))
    if effects:
        prefs["effects"] = effects

    fonts = _parse_fonts_from_ai_keywords(row.get("AI Prompt Keywords") or "")
    if fonts:
        prefs["typography"] = fonts

    return prefs


def enrich_style_vocabulary_prefs(
    rich_intent: Dict[str, Any],
    *,
    user_prompt: str = "",
    project_name: Optional[str] = None,
    direction_enum: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Soften style-vocabulary hits: keep the locked direction, fill palette from
    the matching styles.csv row (not BM25 product-category search).

    If the CSV row has no usable hex colors, fall back to an engine call queried
    with the direction name, then re-force ``direction``.
    """
    direction = (rich_intent.get("direction") or "").strip()
    if not direction:
        return rich_intent

    row = _find_style_csv_row(direction)
    if row:
        prefs = _theme_prefs_from_style_row(row, direction=direction)
        has_palette = bool(prefs.get("primary") or prefs.get("background"))
        if has_palette:
            logger.info(
                "Style vocabulary enriched from styles.csv → direction=%s primary=%s bg=%s",
                direction,
                prefs.get("primary"),
                prefs.get("background"),
            )
            return prefs
        logger.info(
            "Style CSV row for %s has no hex palette; falling back to engine tokens",
            direction,
        )

    # Fallback: engine query biased to the locked style name (not product BM25 alone).
    query = f"{direction} {(user_prompt or '')}".strip()[:400]
    try:
        design_system = generate_design_system_dict(query, project_name=project_name)
        prefs = map_design_system_to_theme_preferences(
            design_system, direction_enum=direction_enum
        )
        prefs["direction"] = direction
        prefs["source"] = "style_vocabulary"
        prefs["_palette_source"] = "design_engine_fallback"
        return prefs
    except Exception as exc:
        logger.warning(
            "Style vocabulary palette fallback failed for %s: %s", direction, exc
        )
        return rich_intent


# Engine styles.csv still uses "(Mobile)" labels; map onto desktop-neutral enum names.
_DIRECTION_ALIASES = {
    "modern dark (cinema mobile)": "Modern Dark Cinema",
    "saas mobile (high-tech boutique)": "High-Tech Boutique",
    "terminal cli (mobile)": "Terminal CLI",
    "kinetic brutalism (mobile)": "Kinetic Brutalism",
    "flat design mobile (touch-first)": "Flat Design (Touch-First)",
    "material you (md3 mobile)": "Material You (MD3)",
    "neo brutalism (mobile)": "Neo Brutalism",
    "bold typography (mobile poster)": "Bold Typography Poster",
    "academia (scholarly mobile)": "Academia (Scholarly)",
    "cyberpunk mobile hud": "Cyberpunk HUD",
    "bitcoin defi (mobile)": "Bitcoin DeFi",
    "claymorphism (mobile)": "Claymorphism",
    "enterprise saas (mobile)": "Enterprise SaaS",
    "sketch hand-drawn (mobile)": "Sketch Hand-Drawn",
    "neumorphism (mobile)": "Neumorphism",
}


def _match_direction(style_name: str, direction_enum: List[str]) -> Optional[str]:
    """Map engine style name onto DESIGN_DIRECTION_ENUM (exact / case / substring / alias)."""
    if not style_name:
        return None
    if style_name in direction_enum:
        return style_name
    lower_map = {s.lower(): s for s in direction_enum}
    key = style_name.lower().strip()
    if key in lower_map:
        return lower_map[key]
    alias = _DIRECTION_ALIASES.get(key)
    if alias and alias in direction_enum:
        return alias
    if alias:
        # Alias target may still match via case-insensitive enum lookup
        alias_hit = lower_map.get(alias.lower())
        if alias_hit:
            return alias_hit
    # Prefer longest substring match to avoid "Flat Design" stealing "Flat Design Mobile"
    candidates = []
    for s in direction_enum:
        sl = s.lower()
        if key in sl or sl in key:
            candidates.append((len(s), s))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def _split_constraint_list(raw: Any) -> List[str]:
    """Split engine anti_patterns / effects strings (often '+' separated) into a list."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    # Engine formatters split on '+' ; also allow ';' / newlines
    parts = re.split(r"\s*[+;|\n]\s*", text)
    return [p.strip(" -•\t") for p in parts if p.strip(" -•\t")]


def _detect_generic_reasoning_fallback(design_system: Dict[str, Any]) -> bool:
    """
    True when ui-reasoning.csv did not match and design_system.py used its
    hardcoded generic defaults (Hero + Features + CTA / Minimalism + Flat Design).
    """
    if "_used_generic_reasoning_fallback" in design_system:
        return bool(design_system["_used_generic_reasoning_fallback"])

    pattern = design_system.get("pattern") or {}
    pattern_name = (pattern.get("name") if isinstance(pattern, dict) else "") or ""
    anti = (design_system.get("anti_patterns") or "").strip()
    if pattern_name == _GENERIC_PATTERN and not anti:
        return True
    category = (design_system.get("category") or "").strip().lower()
    if not anti and category in ("", "general") and pattern_name == _GENERIC_PATTERN:
        return True
    return False


def _load_direction_enum_from_csv() -> List[str]:
    """Read Style Category values from vendored styles.csv (avoids importing ai_processor)."""
    import csv

    styles_path = VENDOR_ROOT / "data" / "styles.csv"
    if not styles_path.is_file():
        return []
    names: List[str] = []
    with open(styles_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("Style Category") or "").strip()
            if name:
                names.append(name)
    return names


def map_design_system_to_theme_preferences(
    design_system: Dict[str, Any],
    *,
    direction_enum: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Convert DesignSystemGenerator.generate() dict → IR theme_preferences shape."""
    if direction_enum is None:
        direction_enum = _load_direction_enum_from_csv()

    style = design_system.get("style") or {}
    colors = design_system.get("colors") or {}
    typography = design_system.get("typography") or {}
    pattern = design_system.get("pattern") or {}

    style_name = style.get("name") if isinstance(style, dict) else ""
    direction = _match_direction(str(style_name or ""), direction_enum)

    effects_raw = design_system.get("key_effects") or (
        style.get("effects") if isinstance(style, dict) else ""
    )
    effects = _split_constraint_list(effects_raw)
    if not effects and isinstance(style, dict) and style.get("effects"):
        effects = _split_constraint_list(style.get("effects"))

    anti_patterns = _split_constraint_list(design_system.get("anti_patterns"))

    heading = ""
    body = ""
    if isinstance(typography, dict):
        heading = (typography.get("heading") or typography.get("display") or "").strip()
        body = (typography.get("body") or "").strip()

    primary = (colors.get("primary") or "").strip() if isinstance(colors, dict) else ""
    secondary = (colors.get("secondary") or "").strip() if isinstance(colors, dict) else ""
    accent = (
        (colors.get("accent") or colors.get("cta") or "").strip()
        if isinstance(colors, dict)
        else ""
    )
    background = (colors.get("background") or "").strip() if isinstance(colors, dict) else ""
    text = (
        (colors.get("text") or colors.get("foreground") or "").strip()
        if isinstance(colors, dict)
        else ""
    )

    used_generic = _detect_generic_reasoning_fallback(design_system)

    prefs: Dict[str, Any] = {
        "source": "design_engine",
        "productCategory": design_system.get("category") or "",
        "pattern": pattern.get("name") if isinstance(pattern, dict) else "",
        "antiPatterns": anti_patterns,
        "usedGenericReasoningFallback": used_generic,
    }
    if direction:
        prefs["direction"] = direction
    if primary:
        prefs["primary"] = primary
    if secondary:
        prefs["secondary"] = secondary
    if accent:
        prefs["accent"] = accent
    if background:
        prefs["background"] = background
    if text:
        prefs["text"] = text
    if heading or body:
        prefs["typography"] = {
            "display": heading or body,
            "body": body or heading,
        }
    if effects:
        prefs["effects"] = effects

    # Optional Google Fonts URL for codegen (not part of IR theme schema, but useful context)
    if isinstance(typography, dict) and typography.get("google_fonts_url"):
        prefs["googleFontsUrl"] = typography.get("google_fonts_url")

    return prefs


def build_design_engine_query(
    user_prompt: str,
    enhanced_prompt: str = "",
    *,
    max_len: int = 400,
) -> str:
    """
    Build the BM25 query for DesignSystemGenerator from the *user* prompt.

    The enhanced prompt is for IR structure only. Feeding it into BM25 mixes in
    pipeline boilerplate and can dominate style ranking. If the user asked for
    "mobile-first" (or any other look), that text is already in user_prompt and
    will be used as-is.
    """
    user = (user_prompt or "").strip()
    if user:
        return user[:max_len]
    enhanced = (enhanced_prompt or "").strip()
    return enhanced[:max_len] if enhanced else ""


def generate_design_system_dict(
    query: str,
    project_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Call DesignSystemGenerator.generate() in-process.

    Note: the public ``generate_design_system()`` helper returns a formatted
    string; we need the structured dict for IR theme mapping.
    Style comes from BM25 against the query — we do not override it with a
    hardcoded Style_Priority list.
    """
    if not _ENGINE_DIR.is_dir():
        raise FileNotFoundError(
            f"Vendored design engine not found at {_ENGINE_DIR} "
            f"(expected pin {VENDORED_TAG})"
        )
    data_dir = VENDOR_ROOT / "data"
    if not data_dir.is_dir():
        raise FileNotFoundError(f"Vendored design data not found at {data_dir}")

    Generator = _load_generator_class()
    generator = Generator()
    design_system = generator.generate(query, project_name=project_name or "Screen")

    # Accurate generic-fallback flag: no matching row in ui-reasoning.csv for category.
    category = design_system.get("category") or "General"
    rule = generator._find_reasoning_rule(category)
    design_system["_reasoning_rule_matched"] = bool(rule)
    design_system["_used_generic_reasoning_fallback"] = not bool(rule)
    return design_system


def _build_persistence_stub_design_system(
    theme_preferences: Dict[str, Any],
    *,
    project_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a minimal design-system-like dict for persistence when the BM25 engine
    was intentionally skipped (explicit brand/hex or style vocabulary).

    The JSON cache (`master.json`) is the primary store. This stub only exists so
    `save_master()` / `save_page_override()` can still attempt a human-readable MD
    write without crashing the pipeline if the vendored formatter expects a dict.
    """
    typo = theme_preferences.get("typography") or {}
    return {
        "project_name": project_name or "Screen",
        "category": theme_preferences.get("productCategory") or "User Directed",
        "style": {
            "name": theme_preferences.get("direction") or "User Directed",
            "effects": theme_preferences.get("effects") or [],
        },
        "colors": {
            "primary": theme_preferences.get("primary") or "",
            "secondary": theme_preferences.get("secondary") or "",
            "accent": theme_preferences.get("accent") or "",
            "background": theme_preferences.get("background") or "",
            "text": theme_preferences.get("text") or "",
        },
        "typography": {
            "heading": typo.get("display") or "",
            "body": typo.get("body") or "",
        },
        "pattern": {
            "name": theme_preferences.get("pattern") or "User Directed",
        },
        "anti_patterns": " + ".join(theme_preferences.get("antiPatterns") or []),
        "key_effects": " + ".join(theme_preferences.get("effects") or []),
        "_reasoning_rule_matched": False,
        "_used_generic_reasoning_fallback": False,
    }


def _clean_theme_object(data: Any) -> Dict[str, Any]:
    """Normalize LLM theme JSON into theme_preferences shape."""
    if not isinstance(data, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key in (
        "direction",
        "primary",
        "secondary",
        "accent",
        "background",
        "text",
        "effects",
        "antiPatterns",
        "productCategory",
        "pattern",
    ):
        value = data.get(key)
        if value not in (None, "", []):
            cleaned[key] = value
    typo = data.get("typography")
    if isinstance(typo, dict):
        display = str(typo.get("display") or "").strip()
        body = str(typo.get("body") or "").strip()
        if display or body:
            cleaned["typography"] = {"display": display or body, "body": body or display}
    return cleaned


def _synthesize_theme_from_prompt(
    *,
    user_prompt: str,
    locked_theme: Optional[Dict[str, Any]] = None,
    suggested_theme: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Short LLM call for skip-path theme synthesis.

    - `locked_theme` contains non-negotiable fields inferred from the prompt
      (e.g. explicit hex values, explicit direction).
    - `suggested_theme` contains style-row defaults that can be used as hints
      but must not override richer prompt intent.
    """
    locked_theme = locked_theme or {}
    suggested_theme = suggested_theme or {}

    schema_hint = {
        "direction": "string",
        "primary": "#RRGGBB",
        "secondary": "#RRGGBB",
        "accent": "#RRGGBB",
        "background": "#RRGGBB",
        "text": "#RRGGBB",
        "typography": {"display": "string", "body": "string"},
        "effects": ["string"],
        "antiPatterns": ["string"],
        "productCategory": "string",
        "pattern": "string",
    }
    prompt = (
        "You are extracting a workflow-level UI theme from a user's natural-language design prompt.\n"
        "Return ONLY valid JSON.\n\n"
        "Rules:\n"
        "1. Respect LOCKED_THEME exactly; do not change locked values.\n"
        "2. Use SUGGESTED_THEME only as fallback inspiration when the prompt is vague.\n"
        "3. Prefer the user's own described look, colors, typography mood, and style.\n"
        "4. Omit fields you cannot infer confidently.\n"
        "5. Colors must be concrete hex values when present.\n\n"
        f"USER_PROMPT:\n{user_prompt}\n\n"
        f"LOCKED_THEME:\n{json.dumps(locked_theme, ensure_ascii=False)}\n\n"
        f"SUGGESTED_THEME:\n{json.dumps(suggested_theme, ensure_ascii=False)}\n\n"
        f"JSON_SCHEMA_SHAPE:\n{json.dumps(schema_hint, ensure_ascii=False)}\n"
    )
    try:
        response = sync_generate(
            _THEME_SYNTH_MODEL,
            prompt,
            config=GeminiGenerateConfig(
                temperature=0.0,
                response_mime_type="application/json",
                max_output_tokens=500,
            ),
        )
        raw_text = getattr(response, "text", "") or ""
        parsed = json.loads(raw_text)
        return _clean_theme_object(parsed)
    except Exception as exc:
        logger.warning("Theme synthesis LLM failed; falling back to non-LLM tokens: %s", exc)
        return {}


def _merge_theme_preferences(
    *,
    base: Optional[Dict[str, Any]] = None,
    override: Optional[Dict[str, Any]] = None,
    locked: Optional[Dict[str, Any]] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge theme_preferences with `locked` taking final precedence."""
    merged: Dict[str, Any] = {}
    for part in (base or {}, override or {}, locked or {}):
        for key, value in part.items():
            if value not in (None, "", []):
                merged[key] = value
    if source:
        merged["source"] = source
    merged.setdefault("antiPatterns", [])
    merged.setdefault("usedGenericReasoningFallback", False)
    return merged


def _persist_resolved_theme_preferences(
    theme_preferences: Dict[str, Any],
    *,
    project_name: Optional[str] = None,
    project_id: Optional[str] = None,
    storage_root: Optional[Path] = None,
    screen_id: Optional[str] = None,
    force_refresh: bool = False,
) -> None:
    """
    Persist resolved theme preferences even when the BM25 engine was skipped.

    Behavior mirrors the normal engine path:
    - `force_refresh=True` with `screen_id` writes a page override
    - otherwise writes the project master
    """
    if not (project_id and storage_root):
        return

    try:
        from services.design_reference import design_persistence as _dp  # lazy; avoids circular

        stub = _build_persistence_stub_design_system(
            theme_preferences, project_name=project_name or project_id
        )
        if force_refresh and screen_id:
            _dp.save_page_override(
                project_id, screen_id, theme_preferences, stub, storage_root
            )
        else:
            _dp.save_master(project_id, theme_preferences, stub, storage_root)
    except Exception as exc:
        logger.warning("Design theme persistence failed (non-fatal): %s", exc)


def resolve_theme_preferences(
    *,
    user_prompt: str,
    enhanced_prompt: str,
    project_name: Optional[str] = None,
    direction_enum: Optional[List[str]] = None,
    # Phase 6 — persistence
    project_id: Optional[str] = None,
    storage_root: Optional[Path] = None,
    screen_id: Optional[str] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Resolve theme preferences for IR generation.

    Resolution order
    ----------------
    1. Explicit hex / brand short-circuit on the *user* prompt (always highest priority).
    2. If ``force_refresh`` is False and ``project_id`` + ``storage_root`` are provided:
       a. Page-level override (if ``screen_id`` provided and override file exists).
       b. Project MASTER (if master file exists).
    3. Vendored design engine call on the user prompt (generates and persists on cache miss).

    Persistence
    -----------
    When ``project_id`` and ``storage_root`` are set and the engine is called:
    * First-time call (no master): writes master.json + MASTER.md.
    * ``force_refresh=True`` with ``screen_id``: writes page override JSON + MD;
      leaves the project master intact.
    """
    # --- 1. Explicit hex / brand short-circuit (always checked first) ---
    for candidate in (user_prompt, enhanced_prompt):
        explicit = detect_explicit_theme_signal(candidate or "")
        if explicit is not None:
            llm_theme = _synthesize_theme_from_prompt(
                user_prompt=user_prompt or enhanced_prompt or "",
                locked_theme=explicit,
                suggested_theme={},
            )
            explicit = _merge_theme_preferences(
                base=explicit,
                override=llm_theme,
                locked=explicit,
                source=str(explicit.get("source") or "explicit"),
            )
            logger.info(
                "Design engine skipped (explicit signal: %s)", explicit.get("source")
            )
            _persist_resolved_theme_preferences(
                explicit,
                project_name=project_name,
                project_id=project_id,
                storage_root=storage_root,
                screen_id=screen_id,
                force_refresh=force_refresh,
            )
            return explicit

    # --- 1.5. Rich style vocabulary (soften, do not hard-skip palette) ---
    # When the user names a style (sketch-style, glassmorphism, …), lock that
    # direction and fill palette/effects/antiPatterns from the matching styles.csv
    # row — not from BM25 product-category search (which remaps sketch → 3D Preview).
    rich_intent = detect_rich_style_intent(user_prompt or "")
    if rich_intent is not None:
        csv_theme = enrich_style_vocabulary_prefs(
            rich_intent,
            user_prompt=user_prompt or "",
            project_name=project_name or project_id,
            direction_enum=direction_enum,
        )
        llm_theme = _synthesize_theme_from_prompt(
            user_prompt=user_prompt or enhanced_prompt or "",
            locked_theme={"direction": rich_intent.get("direction")},
            suggested_theme=csv_theme,
        )
        resolved = _merge_theme_preferences(
            base=csv_theme,
            override=llm_theme,
            locked={"direction": rich_intent.get("direction")},
            source="style_vocabulary",
        )
        _persist_resolved_theme_preferences(
            resolved,
            project_name=project_name,
            project_id=project_id,
            storage_root=storage_root,
            screen_id=screen_id,
            force_refresh=force_refresh,
        )
        return resolved

    # --- 2. Persistence cache lookup (skip when force_refresh requested) ---
    if project_id and storage_root and not force_refresh:
        from services.design_reference import design_persistence as _dp  # lazy; avoids circular

        if screen_id and _dp.has_page_override(project_id, screen_id, storage_root):
            cached = _dp.load_page_override(project_id, screen_id, storage_root)
            if cached is not None:
                cached["source"] = "design_cache_page"
                return cached

        if _dp.has_master(project_id, storage_root):
            cached = _dp.load_master(project_id, storage_root)
            if cached is not None:
                cached["source"] = "design_cache_master"
                return cached

    # --- 3. Engine call (user prompt only — preserves user intent for BM25) ---
    query = build_design_engine_query(user_prompt, enhanced_prompt)
    if not query:
        return {
            "source": "empty",
            "antiPatterns": [],
            "usedGenericReasoningFallback": False,
        }

    try:
        design_system = generate_design_system_dict(query, project_name=project_name or project_id)
    except Exception as exc:
        logger.exception("Design engine failed; continuing without theme lookup: %s", exc)
        return {
            "source": "design_engine_error",
            "antiPatterns": [],
            "usedGenericReasoningFallback": False,
            "error": str(exc),
        }

    prefs = map_design_system_to_theme_preferences(
        design_system, direction_enum=direction_enum
    )
    if prefs.get("usedGenericReasoningFallback"):
        logger.info(
            "Design engine used generic reasoning fallback (category=%s pattern=%s)",
            prefs.get("productCategory"),
            prefs.get("pattern"),
        )
    else:
        logger.info(
            "Design engine matched category=%s direction=%s",
            prefs.get("productCategory"),
            prefs.get("direction"),
        )

    # --- 4. Persist on cache miss ---
    if project_id and storage_root:
        try:
            from services.design_reference import design_persistence as _dp  # lazy

            if force_refresh and screen_id:
                _dp.save_page_override(
                    project_id, screen_id, prefs, design_system, storage_root
                )
            else:
                _dp.save_master(project_id, prefs, design_system, storage_root)
        except Exception as exc:
            logger.warning("Design system persistence failed (non-fatal): %s", exc)

    return prefs


def measure_generic_fallback_rate(
    prompts: List[str],
    *,
    project_name: str = "FallbackProbe",
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """
    Run a batch of prompts through the engine and count generic-reasoning fallbacks.

    Returns (fallback_count, total, per_prompt_summaries).
    """
    summaries: List[Dict[str, Any]] = []
    fallbacks = 0
    for prompt in prompts:
        prefs = resolve_theme_preferences(
            user_prompt=prompt,
            enhanced_prompt=prompt,
            project_name=project_name,
        )
        used = bool(prefs.get("usedGenericReasoningFallback"))
        if used:
            fallbacks += 1
        summaries.append(
            {
                "prompt": prompt,
                "source": prefs.get("source"),
                "category": prefs.get("productCategory"),
                "direction": prefs.get("direction"),
                "pattern": prefs.get("pattern"),
                "usedGenericReasoningFallback": used,
                "antiPatternsCount": len(prefs.get("antiPatterns") or []),
            }
        )
    return fallbacks, len(prompts), summaries

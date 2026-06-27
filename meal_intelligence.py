"""Deterministic meal correction, fingerprinting and duplicate detection."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from models import MealAnalysis

try:
    from PIL import Image
except ImportError:  # pragma: no cover - fallback remains functional
    Image = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LockedQuantity:
    food_text: str
    grams: float
    measurement_state: str | None = None  # cooked/raw/unknown


_QUANTITY_PATTERNS = (
    re.compile(
        r"(?P<grams>\d+(?:[.,]\d+)?)\s*(?:גרם|גר'|ג\b|g\b)\s*(?P<food>[^,.;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<food>[^,.;\n]+?)\s*[-:]?\s*(?P<grams>\d+(?:[.,]\d+)?)\s*(?:גרם|גר'|ג\b|g\b)",
        re.IGNORECASE,
    ),
)


def parse_locked_quantities(text: str) -> list[LockedQuantity]:
    """Extract explicit user quantities. User numbers are hard constraints."""
    normalized = text.replace("־", "-")
    measurement_state: str | None = None
    if any(token in normalized for token in ("מבושל", "אחרי בישול", "מוכן")):
        measurement_state = "cooked"
    elif any(token in normalized for token in ("יבש", "לפני בישול", "נא")):
        measurement_state = "raw"

    found: list[LockedQuantity] = []
    seen: set[tuple[str, float]] = set()
    for pattern in _QUANTITY_PATTERNS:
        for match in pattern.finditer(normalized):
            grams = float(match.group("grams").replace(",", "."))
            food = match.group("food").strip(" -–—:()")
            food = re.sub(r"\s+", " ", food)
            if not food or grams <= 0 or grams > 5000:
                continue
            key = (food.casefold(), grams)
            if key not in seen:
                found.append(LockedQuantity(food, grams, measurement_state))
                seen.add(key)
    return found


# ---------------------------------------------------------------------------
# Preparation-method correction parser  (REC-MEAL-01 / REC-MEAL-04)
# ---------------------------------------------------------------------------

PREPARATION_METHODS: dict[str, str] = {
    "raw": "נא",
    "boiled": "מבושל",
    "grilled": "צלוי",
    "baked": "אפוי",
    "fried": "מטוגן",
    "deep_fried": "מטוגן בשמן עמוק",
    "air_fried": "מטוגן באוויר",
    "steamed": "מאודה",
    "unknown": "לא ידוע",
}

PREPARATION_ALIASES: dict[str, str] = {
    # Hebrew
    "מטוגן": "fried",
    "טיגון": "fried",
    "בטיגון": "fried",
    "עשוי בטיגון": "fried",
    "מטוגנת": "fried",
    "מטוגנים": "fried",
    "צלוי": "grilled",
    "על הגריל": "grilled",
    "גריל": "grilled",
    "צלויה": "grilled",
    "צלויים": "grilled",
    "אפוי": "baked",
    "בתנור": "baked",
    "אפויה": "baked",
    "אפויים": "baked",
    "מבושל": "boiled",
    "בישול": "boiled",
    "מבושלת": "boiled",
    "מבושלים": "boiled",
    "מאודה": "steamed",
    "נא": "raw",
    "טרי": "raw",
    "חי": "raw",
    "מטוגן בשמן עמוק": "deep_fried",
    "בשמן עמוק": "deep_fried",
    "דיפ פריי": "deep_fried",
    "מטוגן באוויר": "air_fried",
    "אייר פרייר": "air_fried",
    "באוויר חם": "air_fried",
    # English
    "fried": "fried",
    "grilled": "grilled",
    "baked": "baked",
    "boiled": "boiled",
    "steamed": "steamed",
    "raw": "raw",
    "deep fried": "deep_fried",
    "deep-fried": "deep_fried",
    "air fried": "air_fried",
    "air-fried": "air_fried",
}

# Relative calorie multipliers when preparation changes.
# Base = boiled/steamed (no added oil).
# These are conservative estimates; the user can override.
PREPARATION_CALORIE_FACTORS: dict[str, float] = {
    "raw": 1.0,
    "boiled": 1.0,
    "steamed": 1.0,
    "grilled": 1.05,   # minimal oil
    "baked": 1.05,
    "fried": 1.25,      # pan-fried, conservative oil estimate
    "deep_fried": 1.40,
    "air_fried": 1.08,
    "unknown": 1.0,
}


@dataclass(frozen=True)
class MealCorrection:
    """A structured correction parsed from user text."""
    kind: str  # "preparation", "quantity", "remove", "add", "rename"
    item_hint: str  # food item the correction targets ("" = whole meal)
    value: str  # new value: preparation method key, grams, etc.
    original_text: str  # the user's raw text for logging


# ---------------------------------------------------------------------------
# Item-removal patterns  (REC-PLAN-MEAL-03-12)
# ---------------------------------------------------------------------------

# Each tuple is (compiled_pattern, canonical_item_name).
# The canonical name is a Hebrew term used for similarity matching against
# the analysis items.  Use the most common Hebrew name for the ingredient.
_REMOVAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # "בלי שמן" / "ללא שמן" / "הוצא שמן" / "בלי שמן זית"
    (re.compile(r"(?:בלי|ללא|הוצא|הסר)\s+שמן(?:\s+זית)?", re.IGNORECASE), "שמן"),
    # Generic "בלי <item>" / "ללא <item>" for other common additions
    # Captures the item after the negation keyword so it can be used as item_hint.
    (re.compile(r"(?:בלי|ללא|הוצא|הסר)\s+([\u0590-\u05FF][\u0590-\u05FF\s]{1,30})", re.IGNORECASE), ""),
]


def _parse_removal_corrections(text: str) -> list[MealCorrection]:
    """Extract explicit item-removal instructions from *text*."""
    normalized = text.strip()
    found: list[MealCorrection] = []

    # First try the oil-specific pattern (highest priority).
    oil_pat, oil_item = _REMOVAL_PATTERNS[0]
    if oil_pat.search(normalized):
        found.append(MealCorrection(
            kind="remove",
            item_hint=oil_item,
            value="",
            original_text=text,
        ))
        return found  # oil removal found; don't also emit a generic removal

    # Generic negation pattern.
    gen_pat, _ = _REMOVAL_PATTERNS[1]
    m = gen_pat.search(normalized)
    if m:
        item_hint = m.group(1).strip()
        # Skip if the captured text looks like a preparation method
        # (those are handled by _PREP_ITEM_PATTERNS).
        if item_hint not in PREPARATION_ALIASES:
            found.append(MealCorrection(
                kind="remove",
                item_hint=item_hint,
                value="",
                original_text=text,
            ))
    return found


# Pattern: "ה<item> <method>" or "<item> <method>"
_PREP_ITEM_PATTERNS = [
    re.compile(
        r"(?:ה)?(?P<item>[\u0590-\u05FF]+)\s+(?:עשוי\s+ב?|הוא\s+)?(?P<method>[\u0590-\u05FF\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:תתקן|תשנה|שנה)\s+(?:ל|ל-)?\s*(?P<method>[\u0590-\u05FF\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:לא\s+\S+[,،]\s*)?(?P<method>[\u0590-\u05FF]+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:זה|הוא|היא)\s+(?P<method>[\u0590-\u05FF]+)$",
        re.IGNORECASE,
    ),
]


def parse_meal_correction(text: str) -> list[MealCorrection]:
    """Parse user correction text into structured :class:`MealCorrection` objects.

    Priority order:
    1. Item-removal corrections ("בלי שמן", "ללא שמן", generic "בלי X").
    2. Preparation-method corrections.
    3. Quantity corrections (delegate to parse_locked_quantities).
    """
    normalized = text.strip().lower()
    corrections: list[MealCorrection] = []

    # --- Item-removal corrections (REC-PLAN-MEAL-03-12) ---
    # Checked first so "בלי שמן" is an explicit removal, not a prep change.
    removal_corrections = _parse_removal_corrections(text)
    if removal_corrections:
        corrections.extend(removal_corrections)
        return corrections  # removal is unambiguous; skip further parsing

    # --- Preparation corrections ---
    for pattern in _PREP_ITEM_PATTERNS:
        m = pattern.search(normalized)
        if m is None:
            continue
        method_text = m.group("method").strip()
        canonical = PREPARATION_ALIASES.get(method_text)
        if canonical is None:
            continue
        item_hint = m.groupdict().get("item", "")
        if item_hint:
            item_hint = item_hint.strip()
        corrections.append(
            MealCorrection(
                kind="preparation",
                item_hint=item_hint or "",
                value=canonical,
                original_text=text,
            )
        )
        break  # first successful match wins

    # --- Quantity corrections (delegate) ---
    for lq in parse_locked_quantities(text):
        corrections.append(
            MealCorrection(
                kind="quantity",
                item_hint=lq.food_text,
                value=str(lq.grams),
                original_text=text,
            )
        )

    return corrections


def _detect_existing_preparation(item_name: str) -> str | None:
    """Return the canonical preparation key found in *item_name*, if any."""
    for label, key in PREPARATION_ALIASES.items():
        if label in item_name:
            return key
    return None


def apply_preparation_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Apply a preparation-method correction to *analysis* and return it.

    * Finds the target item via *correction.item_hint* similarity (or picks
      the first protein-heavy item when the hint is empty).
    * Adjusts calories and fat using :data:`PREPARATION_CALORIE_FACTORS`.
    * Updates the item name with the Hebrew preparation label.
    """
    if correction.kind != "preparation":
        return analysis

    new_method = correction.value
    if new_method not in PREPARATION_CALORIE_FACTORS:
        return analysis

    # --- Locate target item ---
    target = None
    if correction.item_hint:
        candidates = sorted(
            analysis.items,
            key=lambda item: _similarity(correction.item_hint, item.name),
            reverse=True,
        )
        if candidates and _similarity(correction.item_hint, candidates[0].name) >= 0.15:
            target = candidates[0]
    if target is None and analysis.items:
        # Default: first protein-heavy item, else first item
        protein_items = [i for i in analysis.items if i.protein > 0]
        target = protein_items[0] if protein_items else analysis.items[0]

    if target is None:
        return analysis

    # --- Determine old method & compute factor ---
    old_method = _detect_existing_preparation(target.name) or "unknown"
    old_factor = PREPARATION_CALORIE_FACTORS.get(old_method, 1.0)
    new_factor = PREPARATION_CALORIE_FACTORS[new_method]

    if old_factor > 0:
        ratio = new_factor / old_factor
    else:
        ratio = 1.0

    # Adjust calories and fat; protein and carbs stay mostly the same
    target.calories = round(target.calories * ratio, 1)
    target.fat = round(target.fat * ratio, 1)

    # --- Update item name with new preparation label ---
    hebrew_label = PREPARATION_METHODS.get(new_method, new_method)
    # Remove any existing preparation annotations "(מבושל)" etc.
    cleaned_name = re.sub(
        r"\s*\((?:" + "|".join(re.escape(v) for v in PREPARATION_METHODS.values()) + r")\)",
        "",
        target.name,
    )
    target.name = f"{cleaned_name} ({hebrew_label})"

    # --- Add explanatory note ---
    old_label = PREPARATION_METHODS.get(old_method, old_method)
    note = f"שיטת הכנה עודכנה: {old_label} → {hebrew_label} (מקדם קלורי ×{ratio:.2f})"
    if note not in analysis.notes:
        analysis.notes.append(note)

    return analysis


def apply_item_removal_correction(
    analysis: MealAnalysis,
    correction: MealCorrection,
) -> MealAnalysis:
    """Remove the item identified by *correction.item_hint* from *analysis*.

    Uses token-based similarity to find the best match.  If no item scores
    above the minimum threshold the analysis is returned unchanged so a
    partial match cannot silently delete the wrong item.

    This implements the deterministic path for REC-PLAN-MEAL-03-12: when the
    user says "בלי שמן" the oil line item is removed and the meal totals are
    recalculated from the remaining items.
    """
    if correction.kind != "remove":
        return analysis

    item_hint = correction.item_hint
    if not item_hint:
        return analysis

    # Find the closest matching item.
    candidates = sorted(
        analysis.items,
        key=lambda item: _similarity(item_hint, item.name),
        reverse=True,
    )
    if not candidates:
        return analysis

    best = candidates[0]
    score = _similarity(item_hint, best.name)

    # Minimum similarity threshold to avoid accidental removal.
    # For "שמן" we also accept items whose name contains the hint token.
    hint_tokens = _tokens(item_hint)
    name_contains_hint = any(
        t in _tokens(best.name) for t in hint_tokens
    )
    if score < 0.15 and not name_contains_hint:
        # No good match — leave the analysis unchanged.
        note = f"פריט לא נמצא להסרה: {item_hint}"
        if note not in analysis.notes:
            analysis.notes.append(note)
        return analysis

    removed_name = best.name
    analysis.items = [item for item in analysis.items if item is not best]

    note = f"הוסר: {removed_name}"
    if note not in analysis.notes:
        analysis.notes.append(note)

    return analysis


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w\u0590-\u05FF]+", text.casefold())
        if len(token) > 1 and token not in {"מבושל", "נא", "גרם"}
    }


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def apply_locked_quantities(
    analysis: MealAnalysis,
    constraints: Iterable[LockedQuantity],
) -> tuple[MealAnalysis, list[LockedQuantity]]:
    """Apply explicit quantities and return unmatched constraints."""
    unmatched: list[LockedQuantity] = []
    for constraint in constraints:
        candidates = sorted(
            analysis.items,
            key=lambda item: _similarity(constraint.food_text, item.name),
            reverse=True,
        )
        if not candidates or _similarity(constraint.food_text, candidates[0].name) < 0.2:
            unmatched.append(constraint)
            continue
        item = candidates[0]
        old_grams = float(item.grams)
        ratio = constraint.grams / old_grams if old_grams > 0 else 1.0
        item.grams = round(constraint.grams, 1)
        item.calories = round(item.calories * ratio, 1)
        item.protein = round(item.protein * ratio, 1)
        item.carbs = round(item.carbs * ratio, 1)
        item.fat = round(item.fat * ratio, 1)
        item.confidence = max(float(item.confidence), 0.95)
        if constraint.measurement_state == "cooked" and "מבושל" not in item.name:
            item.name = f"{item.name} (מבושל)"
        elif constraint.measurement_state == "raw" and "יבש" not in item.name and "נא" not in item.name:
            item.name = f"{item.name} (משקל לפני בישול)"
    return analysis, unmatched


def requires_cooked_raw_clarification(constraints: Iterable[LockedQuantity]) -> bool:
    sensitive = ("אורז", "פסטה", "פתיתים", "קוסקוס", "קטניות", "עדשים", "שעועית")
    return any(
        c.measurement_state is None and any(food in c.food_text for food in sensitive)
        for c in constraints
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def perceptual_hash(data: bytes) -> str:
    """Return a compact dHash; SHA fallback if Pillow is unavailable."""
    if Image is None:
        return sha256_bytes(data)[:16]
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("L").resize((9, 8))
            pixels = list(image.getdata())
        bits = []
        for row in range(8):
            start = row * 9
            for col in range(8):
                bits.append(pixels[start + col] > pixels[start + col + 1])
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:016x}"
    except Exception:
        return sha256_bytes(data)[:16]


def hamming_distance_hex(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (ValueError, TypeError):
        return 999


async def register_meal_fingerprint(
    db: Any,
    *,
    user_id: int,
    meal_id: int,
    image_path: str | None,
    telegram_file_unique_id: str | None = None,
) -> None:
    raw = b""
    if image_path:
        path = Path(image_path)
        if path.exists() and path.is_file():
            raw = path.read_bytes()
    sha = sha256_bytes(raw) if raw else ""
    phash = perceptual_hash(raw) if raw else ""
    fingerprint = telegram_file_unique_id or phash or sha
    await db.execute(
        """
        INSERT INTO meal_fingerprints(
            meal_id, user_id, telegram_file_unique_id, sha256, perceptual_hash,
            fingerprint, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(meal_id) DO UPDATE SET
            telegram_file_unique_id=excluded.telegram_file_unique_id,
            sha256=excluded.sha256,
            perceptual_hash=excluded.perceptual_hash,
            fingerprint=excluded.fingerprint
        """,
        (
            meal_id,
            user_id,
            telegram_file_unique_id,
            sha,
            phash,
            fingerprint,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


async def find_image_duplicate(
    db: Any,
    *,
    user_id: int,
    image_bytes: bytes,
    telegram_file_unique_id: str | None = None,
    window_hours: int = 6,
    max_phash_distance: int = 8,
) -> dict[str, Any] | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    sha = sha256_bytes(image_bytes)
    phash = perceptual_hash(image_bytes)
    rows = await db.fetch_all(
        """
        SELECT f.*, m.name, m.calories, m.created_at AS meal_created_at
        FROM meal_fingerprints f JOIN meals m ON m.id=f.meal_id
        WHERE f.user_id=? AND f.created_at>=?
        ORDER BY f.created_at DESC
        """,
        (user_id, cutoff),
    )
    for row in rows:
        if telegram_file_unique_id and row.get("telegram_file_unique_id") == telegram_file_unique_id:
            return row
        if row.get("sha256") == sha:
            return row
        existing_phash = row.get("perceptual_hash") or ""
        if existing_phash and hamming_distance_hex(existing_phash, phash) <= max_phash_distance:
            return row
    return None

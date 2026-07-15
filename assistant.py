"""Natural-language assistant layer.

Turns any free-text message into an *intent* so the user can simply talk to the
coach ("מה לאכול עכשיו?", "תבנה לי 4 אימונים", "לקחתי ריטלין", "כואבת לי הברך",
"המשקל שלי 89") instead of navigating button menus. The intent is then routed
in ``coach_bot.py`` to the capability that already exists.

Classification uses the same OpenAI Responses API + structured-output pattern as
``analyze_meal_image`` / ``morning_menu``. If no client is available or the
model is unsure, a deterministic keyword fallback keeps the bot responsive, and
unknown input falls back to a friendly help reply (never silence).
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel, Field

# B9/ARCH-16: bounded unresolved-reference candidates for the CURRENT turn,
# set by the turn-context pipeline (noam_coach.services.turn_context) before
# delegating an unresolved turn to classification. The classifier includes
# them as structured context instead of guessing references blind.
REFERENCE_CANDIDATES: ContextVar[dict[str, Any] | None] = ContextVar(
    "REFERENCE_CANDIDATES", default=None
)

# All actions the assistant can route to. Each maps to an existing builder.
Action = Literal[
    "today_menu",  # "מה לאכול היום" / "תפריט"
    "next_meal",  # "מה לאכול עכשיו" / "נשארו לי קלוריות?"
    "evening_summary",  # "סיכום" / "איך היה היום"
    "log_meal_text",  # "אכלתי 2 ביצים וטוסט" (ללא תמונה)
    "meal_status",  # "מה עם הארוחות?" / "כמה ארוחות שמרתי?" (REC-PLAN-MEAL-03-04)
    "build_plan",  # "תבנה לי תוכנית" / "3 אימונים בשבוע"
    "start_workout",  # "בוא נתאמן" / "אימון"
    "set_goal",  # "אני רוצה לרדת ל-85"
    "set_calorie_goal",  # "יעד 2100" / "תשנה לי ל-2100 קלוריות" (REC re7 P0-4)
    "set_dietary_pref",  # "אני לא שותה אלכוהול" / "אני צמחוני" / "אלרגי לבוטנים"
    "morning_flag",  # "לקחתי ריטלין" / "היום צום"
    "report_pain",  # "כואבת לי הברך"
    "pain_resolved",  # "הכאב עבר" / "הברך כבר לא כואבת" (FIX 48)
    "update_measurement",  # "המשקל שלי 89"
    "request_progress",  # "מה ההתקדמות שלי"
    "show_profile",  # "מה אתה יודע עליי"
    "redundant_question_challenge",  # "כבר אמרתי לך" / "יש לך את הנתונים" (REC-PLAN-MEAL-03-17)
    "smalltalk_or_help",  # ברירת מחדל — עזרה/שיחה
]


class Intent(BaseModel):
    action: Action
    # Free-form extracted parameters, e.g. {"frequency": 4}, {"weight_kg": 89},
    # {"flag": "ritalin"}, {"location": "ברך ימין"}, {"goal_weight": 85}.
    slots: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1, default=0.5)


SYSTEM_PROMPT = (
    "You are the intent router of a Hebrew fitness & nutrition coaching bot. "
    "Classify the user's free-text message into exactly one action and extract "
    "any useful slots. Actions:\n"
    "- today_menu: wants today's meal plan / menu.\n"
    "- next_meal: wants what to eat next / remaining calories now.\n"
    "- evening_summary: wants a daily summary / how the day went.\n"
    "- log_meal_text: is reporting food they ate, in text (no photo).\n"
    "- meal_status: asks about meals logged today, meal history, or what happened "
    "with a specific meal. Examples: 'מה עם הארוחות?', 'כמה ארוחות שמרתי?', "
    "'מה אכלתי היום?', 'תראה לי את הארוחות', 'מה מצב האוכל?'.\n"
    "- build_plan: wants a workout program built; slot 'frequency' if a number "
    "of workouts/week is mentioned.\n"
    "- start_workout: wants to start/open a workout now.\n"
    "- set_goal: states a target body weight; slot 'goal_weight'.\n"
    "- set_calorie_goal: wants to set/change the DAILY CALORIE target. Examples: "
    "'יעד 2100', 'תשנה לי ל-2100 קלוריות', 'אני רוצה לאכול 2100 ביום', 'היעד שלי "
    "2100', 'בעצם 2000'. Slot 'calories' = the number (typically 800-6000).\n"
    "- set_dietary_pref: states a STANDING dietary preference, restriction or "
    "allergy that is NOT a report of food eaten now. Examples: 'אני לא שותה "
    "אלכוהול', 'אני לא אוכל בשר', 'אני צמחוני', 'אלרגי לבוטנים', 'בלי גלוטן'. "
    "Slots: 'kind' in {restriction, allergy, preference}; 'item' = the food/"
    "drink it concerns (e.g. 'אלכוהול', 'בשר', 'בוטנים'); 'polarity' in "
    "{avoid, prefer}. A negated food sentence ('לא ...') is set_dietary_pref "
    "with polarity 'avoid', NOT log_meal_text. Only use log_meal_text when the "
    "user actually ATE/DRANK something now.\n"
    "- morning_flag: reports something about today affecting appetite/training "
    "(e.g. Ritalin, fasting); slot 'flag' in {ritalin, fasting, other} and "
    "'note'.\n"
    "- report_pain: reports pain/injury/limitation; slot 'location' if given.\n"
    "- pain_resolved: says a previously reported pain/injury is gone/healed/"
    "over. Examples: 'הכאב עבר', 'הברך כבר לא כואבת', 'זה נגמר, אני בסדר', "
    "'החלמתי'. Slot 'location' if a specific body part is named. NOT the same "
    "as report_pain -- this is the pain going AWAY, not appearing.\n"
    "- update_measurement: states a body measurement; slots like 'weight_kg', "
    "'height_cm', 'body_fat_pct'.\n"
    "- request_progress: asks about progress/trends.\n"
    "- show_profile: asks what the bot knows about them.\n"
    "- redundant_question_challenge: user is frustrated that the bot is asking "
    "a question it should already know. Examples: 'כבר אמרתי לך', 'יש לך את "
    "הנתונים', 'למה אתה שואל שוב', 'אתה יודע מתי אני מתאמן'.\n"
    "- smalltalk_or_help: anything else / unclear.\n"
    "Set confidence honestly. Only fill slots you are sure about."
)


def _looks_numeric(text: str) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


_NEGATION_PREFIXES = ("לא ", "אין ", "בלי ", "אל ", "לא־", "אין־", "בלי־")


def _negated(text: str, keyword: str) -> bool:
    """Return True if *keyword* appears in *text* but is preceded by a negation."""
    idx = text.find(keyword)
    if idx < 0:
        return False
    # Look at the words before the keyword (up to ~20 chars back covers "אני לא").
    prefix = text[max(0, idx - 20) : idx]
    # Split into words and check if any is a negation word.
    prefix_words = prefix.split()
    neg_words = {"לא", "אין", "בלי", "אל", "לא־", "אין־", "בלי־", "בלעדי"}
    return bool(neg_words & set(prefix_words))


_EQUIPMENT_PATTERNS = re.compile(
    r"(מכשיר|עמדה|ספסל|מתקן|בר|מוט)\s+(תפוס|תפוסה|תפוסים|תפוסות)"
    r"|"
    r"(תפוס|תפוסה|תפוסים|תפוסות)\s+(מכשיר|עמדה|ספסל|מתקן|בר|מוט)",
    re.UNICODE,
)


def _equipment_occupied(text: str) -> bool:
    """Detect 'device/bench/station is occupied' — NOT pain."""
    return bool(_EQUIPMENT_PATTERNS.search(text))


# Verbs that mark eating/drinking. A negated one ("לא אוכל בשר", "לא שותה
# אלכוהול") is a STANDING preference; a *past-tense* negation ("לא אכלתי היום")
# is a report about today, which we do not turn into a permanent restriction.
_DIET_HABIT_VERBS = ("אוכל", "אוכלת", "שותה", "צורך", "צורכת", "נוגע")
_DIET_PAST_VERBS = ("אכלתי", "שתיתי", "אכלנו", "שתינו")


def _dietary_negation(text: str) -> dict[str, Any] | None:
    """Return preference slots if *text* is a negated standing-diet statement.

    Handles the canonical bug case "לא אלכוהול" and forms like "אני לא אוכל
    בשר" / "אני לא שותה אלכוהול". Returns ``None`` when the text is not a
    standing dietary negation (e.g. a past-tense "לא אכלתי היום").
    """
    t = text.strip()
    has_neg = any(t.startswith(p.strip()) or f" {p.strip()} " in f" {t} " for p in _NEGATION_PREFIXES)
    if not has_neg:
        return None
    # Past-tense negation = a report about today, not a permanent rule.
    if any(v in t for v in _DIET_PAST_VERBS):
        return None
    # Habitual negation ("לא אוכל/שותה X") -> standing avoidance.
    if any(v in t for v in _DIET_HABIT_VERBS):
        return {"kind": "restriction", "polarity": "avoid", "item": t, "note": t}
    # Bare negated noun ("לא אלכוהול", "בלי סוכר"): short, no eating verb at all.
    words = t.split()
    if 1 <= len(words) <= 4:
        return {"kind": "restriction", "polarity": "avoid", "item": t, "note": t}
    return None


def keyword_fallback(text: str) -> Intent:
    """Deterministic classifier used when AI is unavailable or unsure."""
    t = text.strip()

    def has(*words: str) -> bool:
        return any(w in t for w in words)

    def has_positive(*words: str) -> bool:
        """Match keyword only if it is NOT preceded by a negation."""
        return any(w in t and not _negated(t, w) for w in words)

    # Ritalin / fasting — negation matters ("לא לקחתי ריטלין" = skipped).
    if has("ריטלין", "ritalin"):
        status = "skipped" if _negated(t, "ריטלין") or _negated(t, "ritalin") else "taken"
        return Intent(
            action="morning_flag",
            slots={"flag": "ritalin", "status": status, "note": t},
            confidence=0.9,
        )
    if has("צום", "בצום"):
        status = "not_fasting" if _negated(t, "צום") or _negated(t, "בצום") else "fasting"
        return Intent(
            action="morning_flag",
            slots={"flag": "fasting" if status == "fasting" else "not_fasting", "status": status, "note": t},
            confidence=0.8,
        )
    # "המכשיר תפוס" / "העמדה תפוסה" / "הספסל תפוס" → equipment, not pain
    if _equipment_occupied(t):
        return Intent(action="smalltalk_or_help", slots={"note": t, "equipment_occupied": True}, confidence=0.6)
    # FIX 48: pain going AWAY must be checked before the generic pain-report
    # rule below, since both share keywords like "כאב"/"כואב".
    if has("הכאב עבר", "כבר לא כואב", "כבר לא כואבת", "הכאב נעלם", "החלמתי",
           "זה נגמר", "אני בסדר עכשיו", "הברך בסדר", "כבר לא כואבת לי",
           "כבר לא כואב לי"):
        return Intent(action="pain_resolved", slots={"note": t}, confidence=0.75)
    if has_positive("כואב", "כאב", "פציעה", "נתפס", "תפוס"):
        return Intent(action="report_pain", slots={"note": t}, confidence=0.8)
    # REC-PLAN-MEAL-03-17: Detect user challenging a redundant question
    if has("כבר אמרתי", "יש לך את הנתונים", "למה אתה שואל שוב",
           "תבדוק בהיסטוריה", "אתה יודע מתי", "לא רוצה למלא הכול מחדש",
           "יש לך מידע", "כבר יש לך"):
        return Intent(action="redundant_question_challenge", confidence=0.8)
    # REC-PLAN-MEAL-03-04: Detect meal-status questions
    if has("מה עם הארוחות", "מה עם הארוחה", "כמה ארוחות", "מה אכלתי היום",
           "תראה לי את הארוחות", "מה מצב האוכל", "כמה נשאר לי לאכול",
           "מצב הארוחות"):
        return Intent(action="meal_status", confidence=0.8)
    if has("מה לאכול עכשיו", "נשאר לי", "נשארו לי", "כמה נשאר"):
        return Intent(action="next_meal", confidence=0.7)
    if has("תפריט", "מה לאכול", "ארוחות היום"):
        return Intent(action="today_menu", confidence=0.7)
    if has("סיכום", "איך היה", "סוף יום"):
        return Intent(action="evening_summary", confidence=0.7)
    if has("תוכנית", "ספליט", "אימונים בשבוע", "תבנה"):
        num = _looks_numeric(t)
        slots = {"frequency": int(num)} if num and 1 <= num <= 7 else {}
        return Intent(action="build_plan", slots=slots, confidence=0.7)
    if has_positive("אימון", "להתאמן", "נתאמן"):
        return Intent(action="start_workout", confidence=0.7)
    if has("התקדמות", "מגמה", "איך אני מתקדם"):
        return Intent(action="request_progress", confidence=0.6)
    if has("מה אתה יודע", "פרופיל", "עליי"):
        return Intent(action="show_profile", confidence=0.6)
    if has("משקל", "שוקל", 'ק"ג', "קילו"):
        num = _looks_numeric(t)
        if num and 30 <= num <= 400:
            return Intent(action="update_measurement", slots={"weight_kg": num}, confidence=0.7)
    if has("גובה"):
        num = _looks_numeric(t)
        if num and 100 <= num <= 250:
            return Intent(action="update_measurement", slots={"height_cm": num}, confidence=0.7)
    # Daily calorie-goal change (REC re7 P0-4) — must come BEFORE goal weight so
    # "יעד 2100" is read as 2100 calories, not an impossible 2100 kg body weight.
    if (
        has("קלוריות", "קלוריה", "יעד", "תשנה", "לשנות", "בעצם")
        or (has("לאכול") and has("ביום"))
    ):
        num = _looks_numeric(t)
        if num and 800 <= num <= 6000:
            return Intent(action="set_calorie_goal", slots={"calories": int(num)}, confidence=0.7)
    if has("רדת", "להוריד", "יעד", "להגיע ל"):
        num = _looks_numeric(t)
        if num and 30 <= num <= 400:
            return Intent(action="set_goal", slots={"goal_weight": num}, confidence=0.6)
    # Standing dietary preference / restriction / allergy — must be caught BEFORE
    # the meal heuristic so "לא אלכוהול" / "אני לא אוכל בשר" is never logged as a
    # meal. Allergy keywords are a clear restriction even without negation.
    if has("אלרגי", "אלרגיה", "רגיש ל"):
        return Intent(
            action="set_dietary_pref",
            slots={"kind": "allergy", "polarity": "avoid", "item": t, "note": t},
            confidence=0.7,
        )
    if has("לא אוהב", "לא אוהבת", "לא מתחבר", "לא מתחברת", "לא בא לי"):
        return Intent(
            action="set_dietary_pref",
            slots={"kind": "preference", "polarity": "avoid", "item": t, "note": t},
            confidence=0.72,
        )
    if has("מעדיף", "מעדיפה"):
        return Intent(
            action="set_dietary_pref",
            slots={"kind": "preference", "polarity": "prefer", "item": t, "note": t},
            confidence=0.68,
        )
    pref = _dietary_negation(t)
    if pref is not None:
        return Intent(action="set_dietary_pref", slots=pref, confidence=0.7)
    if has("צמחוני", "טבעוני", "כשר", "קטוגני", "ללא גלוטן", "בלי גלוטן", "צמחונית", "טבעונית"):
        return Intent(
            action="set_dietary_pref",
            slots={"kind": "preference", "polarity": "prefer", "item": t, "note": t},
            confidence=0.65,
        )
    # Food-report heuristic: mentions eating — but not "I didn't eat".
    if has_positive("אכלתי", "אכלנו", "אכל ", "שתיתי"):
        return Intent(action="log_meal_text", slots={"text": t}, confidence=0.6)
    return Intent(action="smalltalk_or_help", confidence=0.3)


def _reference_candidates_block() -> str:
    """Bounded turn-context candidates (identities only) for the prompt."""
    candidates = REFERENCE_CANDIDATES.get()
    if not candidates:
        return ""
    try:
        payload = json.dumps(candidates, ensure_ascii=False)[:800]
    except (TypeError, ValueError):
        return ""
    return (
        "\nמצב שיחה (לפענוח רפרנסים כמו 'כן', 'השני', 'תשמור את זה'): "
        + payload
    )


async def classify_intent(
    client: Any | None,
    model: str,
    text: str,
    profile_summary: str = "",
) -> Intent:
    """Classify a free-text message into an :class:`Intent`.

    Falls back to keyword matching when the client is missing, the call fails,
    or the model is not confident enough to act.
    """
    text = (text or "").strip()
    if not text:
        return Intent(action="smalltalk_or_help", confidence=0.0)

    if client is None:
        return keyword_fallback(text)

    try:
        response = await client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"הקשר על המשתמש: {profile_summary or 'אין'}"
                        f"{_reference_candidates_block()}\n\nהודעת המשתמש: {text}"
                    ),
                },
            ],
            text_format=Intent,
        )
        intent = response.output_parsed
        if intent is None:
            return keyword_fallback(text)
        # If the model is unsure, prefer the (cheap, safe) keyword guess when it
        # is more confident, otherwise keep the model's answer.
        if intent.confidence < 0.45:
            fb = keyword_fallback(text)
            if fb.confidence >= intent.confidence:
                return fb
        return intent
    except Exception:  # noqa: BLE001 - never let routing crash the chat
        return keyword_fallback(text)

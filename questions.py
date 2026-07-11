"""Question engine + minimal, goal-focused safety gate.

Two product principles drive this module:

1. **"No question without action"** (chapters 6-8): a question is only worth
   asking if its answer would change a decision. Every question therefore
   declares the fact it fills and the decisions it ``affects`` — a question
   with an empty ``affects`` is never asked.

2. **Question budget / priority** (chapter 7): instead of asking blindly, each
   candidate question is scored and only the single highest-value, currently
   relevant question is surfaced, in a fitting context.

The safety gate is intentionally minimal (per the user's instruction): only the
questions needed to adapt exercise selection safely, each tied to the user's
goals. It does **not** scan messages continuously and never diagnoses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import user_model

# Priority weights (chapter 7):
#   priority = safety*3 + plan_impact*2 + urgency + uncertainty - burden
W_SAFETY = 3
W_PLAN = 2

TIME_RANGE_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}\b")


@dataclass
class Question:
    id: str
    fact_key: str  # which fact this fills
    text: str
    options: list[tuple[str, Any]]  # (label, value); empty -> free text
    affects: tuple[str, ...]  # decisions changed by the answer (must be set)
    numeric: bool = False  # free-text answer should be parsed as a number
    # When True, the question keeps a single "none" button but also accepts a
    # typed free-text answer directly at the same prompt — no extra tap to
    # reach a "yes"/"other" button before typing is required.
    free_text_fallback: bool = False
    min_value: float | None = None
    max_value: float | None = None
    unit: str = ""
    sensitivity: str = "normal"
    safety: int = 0  # 0..5
    plan_impact: int = 0  # 0..5
    urgency: int = 0  # 0..5
    uncertainty: int = 0  # 0..5
    burden: int = 1  # 0..5
    # Only ask when this returns True (context = arbitrary signals dict).
    when: Callable[[dict[str, Any]], bool] = field(default=lambda ctx: True)

    def priority(self) -> int:
        return (
            self.safety * W_SAFETY
            + self.plan_impact * W_PLAN
            + self.urgency
            + self.uncertainty
            - self.burden
        )

    def __post_init__(self) -> None:
        # Enforce "no question without action".
        if not self.affects:
            raise ValueError(f"question {self.id} has no 'affects' — never ask")


# ---------------------------------------------------------------------------
# Safety gate — minimal, asked once before building/running a workout plan.
# Each item maps to exercise selection and is justified by training goals.
# ---------------------------------------------------------------------------

SAFETY_QUESTIONS: list[Question] = [
    Question(
        id="safety_training_limitations",
        fact_key="training_limitations",
        text=(
            "האם יש לך כאב, פציעה או מגבלה שצריך לקחת בחשבון באימונים?\n\n"
            "לדוגמה: כאבי ברכיים, מרפק טניס, פציעה קיימת, מגבלה בתנועה או "
            "הנחיה רפואית להימנע מתרגיל/תנועה מסוימת.\n\n"
            "אם כן — כתוב לי בקצרה מה הבעיה וממה צריך להימנע."
        ),
        options=[("אין", "none")],
        free_text_fallback=True,
        affects=("exercise_selection", "safety"),
        safety=5,
        plan_impact=4,
        urgency=4,
        uncertainty=3,
        burden=1,
    ),
]

# ---------------------------------------------------------------------------
# Plan-building questions — only those that genuinely change the plan.
# ---------------------------------------------------------------------------

PLAN_QUESTIONS: list[Question] = [
    Question(
        id="q_sex",
        fact_key="sex",
        text="מה המין שלך? - להתאמת יעד קלוריות",
        options=[("זכר", "male"), ("נקבה", "female")],
        affects=("calorie_target",),
        plan_impact=4,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_age",
        fact_key="age",
        text="בן כמה אתה? כתוב מספר",
        options=[],  # free text number
        numeric=True,
        min_value=14,
        max_value=100,
        unit="שנים",
        affects=("calorie_target",),
        plan_impact=4,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_primary_goal",
        fact_key="primary_goal",
        text="מה המטרה הראשית שלך כרגע?",
        options=[
            ("ירידה בשומן + ועלייה במסת שריר", "fat_loss_muscle_retention"),
            ("עלייה במסת שריר", "muscle_gain"),
            ("כוח", "strength"),
            ("בריאות וכושר כללי", "general_health"),
        ],
        affects=("calorie_target", "workout_schedule", "menu_planning"),
        plan_impact=5,
        uncertainty=4,
        burden=1,
    ),
    Question(
        id="q_height",
        fact_key="height_cm",
        text="מה הגובה שלך בס\"מ? כתוב מספר (למשל 175).",
        options=[],  # free text number
        numeric=True,
        min_value=120,
        max_value=230,
        unit="ס\"מ",
        affects=("calorie_target",),
        plan_impact=3,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_goal_weight",
        fact_key="goal_weight_kg",
        text="מה משקל היעד שלך? כתוב מספר בק\"ג (למשל 80).",
        options=[],  # free text number
        numeric=True,
        min_value=30,
        max_value=300,
        unit='ק"ג',
        affects=("calorie_target", "rate_of_loss"),
        plan_impact=5,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_goal_timeframe",
        fact_key="goal_timeframe_weeks",
        text="תוך כמה זמן תרצה להגיע למשקל היעד?",
        options=[
            ("3 חודשים", 13),
            ("6 חודשים", 26),
            ("שנה", 52),
        ],
        affects=("calorie_target", "rate_of_loss"),
        plan_impact=4,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_training_days",
        fact_key="training_days_per_week",
        text="כמה ימים בשבוע תוכל להתאמן בפועל? כתוב מספר",
        options=[],  # free text — user types the number
        numeric=True,
        min_value=1,
        max_value=6,
        unit="אימונים",
        affects=("workout_schedule",),
        plan_impact=5,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_session_minutes",
        fact_key="session_minutes",
        text="כמה דקות יש לך בדרך כלל לאימון בודד? כתוב מספר (למשל 45).",
        options=[],  # free text
        numeric=True,
        min_value=15,
        max_value=180,
        unit="דקות",
        affects=("workout_volume",),
        plan_impact=4,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_location",
        fact_key="training_location",
        text="איפה אתה מתאמן בעיקר?",
        options=[("חדר כושר", "gym"), ("בית", "home"), ("משולב", "mixed")],
        affects=("exercise_selection",),
        plan_impact=4,
        uncertainty=3,
        burden=1,
    ),
    Question(
        id="q_experience",
        fact_key="strength_experience",
        text="מה רמת הניסיון שלך באימוני כוח?",
        options=[("מתחיל", "beginner"), ("בינוני", "intermediate"), ("מתקדם", "advanced")],
        affects=("exercise_selection", "workout_volume"),
        plan_impact=4,
        uncertainty=4,
        burden=1,
    ),
    Question(
        id="q_diet_restrictions",
        fact_key="diet_restrictions",
        text="איזה מזונות אתה מעדיף לא לאכול? (אפשר לכתוב חופשי)",
        options=[],
        affects=("menu_planning",),
        plan_impact=3,
        uncertainty=3,
        burden=2,
        when=lambda ctx: ctx.get("planning_nutrition", False),
    ),
    Question(
        id="q_allergies",
        fact_key="allergies",
        text="איזה מזונות עושים לך רגישות או אסורים לך? (אפשר לכתוב חופשי)",
        options=[("אין אלרגיות/רגישויות", "none")],
        free_text_fallback=True,
        affects=("menu_planning", "safety"),
        safety=3,
        plan_impact=3,
        uncertainty=3,
        burden=1,
        when=lambda ctx: ctx.get("planning_nutrition", False),
    ),
]

JIT_QUESTIONS: list[Question] = [
    Question(
        id="q_meal_structure",
        fact_key="meal_structure_preference",
        text="איזה מבנה ארוחות יהיה לך הכי קל לשמור?",
        options=[
            ("3 ארוחות מסודרות", "three_structured"),
            ("2 ארוחות גדולות", "two_large"),
            ("ארוחות קטנות", "small_frequent"),
            ("מסגרת גמישה", "flexible"),
        ],
        affects=("menu_planning", "meal_timing"),
        plan_impact=4,
        uncertainty=4,
        burden=1,
        when=lambda ctx: ctx.get("planning_nutrition", False),
    ),
    Question(
        id="q_cooking_capacity",
        fact_key="cooking_capacity",
        text="כמה התעסקות בהכנת אוכל מתאימה לך בשבוע?",
        options=[
            ("כמעט בלי בישול", "none"),
            ("בסיסי ומהיר", "basic"),
            ("הכנה פעמיים בשבוע", "moderate"),
            ("נהנה לבשל", "enjoys"),
        ],
        affects=("menu_planning", "shopping"),
        plan_impact=4,
        uncertainty=4,
        burden=1,
        when=lambda ctx: ctx.get("planning_nutrition", False),
    ),
    Question(
        id="q_weekly_availability",
        fact_key="weekly_availability",
        text=("באילו ימים ושעות אתה באמת פנוי לאימון? "
              "אפשר לכתוב למשל: ראשון 19:00 שעה, שלישי 18:30 45 דקות."),
        options=[],
        affects=("workout_schedule", "workout_timing"),
        plan_impact=5,
        uncertainty=5,
        burden=2,
        when=lambda ctx: ctx.get("planning_workout", False),
    ),
    Question(
        id="q_equipment",
        fact_key="equipment",
        text="איזה ציוד זמין במקום שבו תתאמן? אפשר לכתוב בקצרה או לבחור אחת מהאפשרויות.",
        options=[("חדר כושר מלא", "full_gym"), ("משקולות בבית", "home_dumbbells"), ("משקל גוף", "bodyweight")],
        free_text_fallback=True,
        affects=("exercise_selection",),
        plan_impact=5,
        uncertainty=4,
        burden=1,
        when=lambda ctx: ctx.get("planning_workout", False),
    ),
    Question(
        id="q_main_barrier",
        fact_key="main_barrier",
        text="מה בדרך כלל מקשה עליך להתמיד?",
        options=[("זמן", "time"), ("רעב", "hunger"), ("בישול", "cooking"), ("עייפות", "fatigue"), ("יציאות", "social"), ("כאב", "pain")],
        affects=("menu_planning", "workout_schedule", "motivation"),
        plan_impact=3,
        uncertainty=4,
        burden=1,
    ),
    Question(
        id="q_coaching_style",
        fact_key="coaching_style",
        text="איך תרצה שאדבר איתך לאורך התהליך?",
        options=[("תומך ועדין", "supportive"), ("ענייני", "direct"), ("תובעני", "challenging"), ("מבוסס נתונים", "data_driven")],
        affects=("notification_style", "motivation"),
        plan_impact=2,
        uncertainty=3,
        burden=1,
    ),
]

LIFESTYLE_QUESTIONS: list[Question] = [
    Question(
        id="q_daily_routine",
        fact_key="daily_routine_summary",
        text=(
            "ספר לי בקצרה איך נראה יום רגיל שלך: "
            "מתי אתה קם, עובד, אוכל, חוזר הביתה ומתי יכול להתאמן. "
            "אפשר גם לשלוח הודעה קולית."
        ),
        options=[],
        affects=(
            "meal_timing", "workout_timing", "menu_planning",
            "workout_schedule",
        ),
        plan_impact=5,
        uncertainty=5,
        burden=3,
    ),
]

ALL_QUESTIONS = SAFETY_QUESTIONS + PLAN_QUESTIONS + JIT_QUESTIONS + LIFESTYLE_QUESTIONS

# TASK-02: question domain, keyed by fact_key. Used by get_next_missing_question
# so a caller can ask "what's the next missing nutrition question" without
# ever surfacing a training/goals/profile question by accident. HealthKit
# data arrives through the import pipeline rather than Q&A, so no question
# currently belongs to that domain — it's still a valid, empty selector.
QUESTION_DOMAINS = ("nutrition", "training", "healthkit", "goals", "profile")

_FACT_KEY_DOMAIN: dict[str, str] = {
    "active_pain": "training",
    "medical_avoidance": "training",
    "training_limitations": "training",
    "sex": "profile",
    "age": "profile",
    "height_cm": "profile",
    "primary_goal": "goals",
    "goal_weight_kg": "goals",
    "goal_timeframe_weeks": "goals",
    "training_days_per_week": "training",
    "session_minutes": "training",
    "training_location": "training",
    "strength_experience": "training",
    "weekly_availability": "training",
    "equipment": "training",
    "diet_restrictions": "nutrition",
    "allergies": "nutrition",
    "meal_structure_preference": "nutrition",
    "cooking_capacity": "nutrition",
    "main_barrier": "profile",
    "coaching_style": "profile",
    "daily_routine_summary": "profile",
}


def question_domain(question: Question) -> str:
    return _FACT_KEY_DOMAIN.get(question.fact_key, "profile")

# Minimal onboarding pool: the few questions truly needed before first value.
# One safety question (pain/limitation) + primary goal + weekly frequency.
# Everything else (location, equipment, experience, diet) is recorded as a gap
# and asked just-in-time later, when a decision actually needs it.
_ONBOARDING_IDS = {
    "safety_training_limitations",
    "q_primary_goal",
    "q_training_days",
    "q_sex",
    "q_age",
    "q_daily_routine",
}
ONBOARDING_QUESTIONS = [q for q in ALL_QUESTIONS if q.id in _ONBOARDING_IDS]

# Slots deferred to just-in-time, recorded as gaps at the end of onboarding.
DEFERRED_GAP_KEYS = {
    "session_minutes": "קובע נפח אימון",
    "training_location": "קובע אילו תרגילים אפשריים",
    "equipment": "קובע אילו תרגילים אפשריים",
    "strength_experience": "קובע מורכבות ונפח",
    "diet_restrictions": "קובע אילו מאכלים להציע",
    "allergies": "בטיחות תזונתית",
}


async def _is_relevant(
    db: user_model.SupportsDB, user_id: int, q: Question, ctx: dict[str, Any]
) -> bool:
    if not q.when(ctx):
        return False
    # Already answered (valid, non-gap fact present) -> not relevant.
    # A KIND_GAP fact means the data is still missing (recorded during onboarding
    # deferral) and the question should still be asked.
    # REC-PLAN-MEAL-03-05: An unconfirmed fact (e.g. from Apple Health import)
    # is still relevant — ask_next_question will show confirmation UI instead of
    # re-asking from scratch.
    existing = (
        await user_model.get_training_limitations_fact(db, user_id)
        if q.fact_key == "training_limitations"
        else await user_model.get_fact(db, user_id, q.fact_key)
    )
    if existing is None:
        return True
    if existing["kind"] == user_model.KIND_GAP:
        # TASK-02: a deliberate skip must not be re-asked on every loop —
        # unlike a plain deferred gap, which stays eligible.
        return not user_model.is_skipped_gap(existing)
    if not existing.get("confirmed"):
        return True  # needs user confirmation
    return False


async def next_question(
    db: user_model.SupportsDB,
    user_id: int,
    context: dict[str, Any] | None = None,
    pool: list[Question] | None = None,
) -> Question | None:
    """Return the single highest-priority relevant question, or None.

    Safety questions naturally win because of the ``safety*3`` weight, so the
    gate is honored without special-casing.
    """
    ctx = context or {}
    candidates: list[Question] = []
    for q in pool if pool is not None else ALL_QUESTIONS:
        if await _is_relevant(db, user_id, q, ctx):
            candidates.append(q)
    if not candidates:
        return None
    return max(candidates, key=lambda q: q.priority())


async def get_next_missing_question(
    db: user_model.SupportsDB,
    user_id: int,
    *,
    domain: str,
    context: dict[str, Any] | None = None,
) -> Question | None:
    """Return the next relevant question for a single domain (TASK-02).

    Unlike ``next_question`` with a hand-picked pool, this always scopes to
    one of ``QUESTION_DOMAINS`` so a nutrition-only or training-only caller
    can never be handed a question from a different domain — no separate
    "don't mix flows" bookkeeping needed at the call site.
    """
    if domain not in QUESTION_DOMAINS:
        raise ValueError(f"unknown question domain: {domain!r}")
    pool = [q for q in ALL_QUESTIONS if question_domain(q) == domain]
    return await next_question(db, user_id, context, pool=pool)


async def pending_safety_questions(db: user_model.SupportsDB, user_id: int) -> list[Question]:
    """Safety questions not yet answered — used as a pre-plan gate.

    A KIND_GAP fact counts as unanswered (it was deferred, not answered).
    """
    pending = []
    for q in SAFETY_QUESTIONS:
        fact = (
            await user_model.get_training_limitations_fact(db, user_id)
            if q.fact_key == "training_limitations"
            else await user_model.get_fact(db, user_id, q.fact_key)
        )
        if fact is None or fact["kind"] == user_model.KIND_GAP:
            pending.append(q)
    return pending


async def record_answer(
    db: user_model.SupportsDB,
    user_id: int,
    question: Question,
    value: Any,
) -> None:
    """Store an answer as a user-reported fact."""
    await user_model.set_fact(
        db,
        user_id,
        question.fact_key,
        value,
        kind=user_model.KIND_FACT,
        source=user_model.SOURCE_USER,
        confirmed=True,
        affects=question.affects,
    )


def normalize_answer(question: Question, value: Any) -> Any:
    """Normalize and validate an answer consistently for text and callbacks."""
    if not question.numeric:
        return value
    if looks_like_time_range(value):
        raise ValueError("זה נראה כמו טווח שעות, לא מספר לשאלה הזו.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("נדרש מספר") from exc
    if question.min_value is not None and numeric < question.min_value:
        raise ValueError(f"הערך חייב להיות לפחות {question.min_value:g} {question.unit}".strip())
    if question.max_value is not None and numeric > question.max_value:
        raise ValueError(f"הערך חייב להיות לכל היותר {question.max_value:g} {question.unit}".strip())
    return int(numeric) if numeric.is_integer() else numeric


def looks_like_time_range(value: Any) -> bool:
    return bool(TIME_RANGE_RE.search(str(value or "")))


def question_by_id(qid: str) -> Question | None:
    for q in ALL_QUESTIONS:
        if q.id == qid:
            return q
    return None


def question_by_fact_key(fact_key: str) -> Question | None:
    """Find a question that populates the given fact key."""
    for q in ALL_QUESTIONS:
        if q.fact_key == fact_key:
            return q
    return None

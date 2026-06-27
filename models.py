"""Pydantic models shared across modules."""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _require_tz(value: datetime | None) -> datetime | None:
    """Reject naive (timezone-unaware) timestamps."""
    if value is not None and value.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return value


def _reject_nan(value: float) -> float:
    """Block NaN and Infinity in numeric fields."""
    if not math.isfinite(value):
        raise ValueError("value must be finite (no NaN or Infinity)")
    return value


class FoodItem(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    name: str
    grams: float = Field(ge=0, le=5000)
    calories: float = Field(ge=0, le=10000)
    protein: float = Field(ge=0, le=1000)
    carbs: float = Field(ge=0, le=2000)
    fat: float = Field(ge=0, le=1000)
    confidence: float = Field(ge=0, le=1)

    @field_validator("grams", "calories", "protein", "carbs", "fat", "confidence")
    @classmethod
    def finite_numbers(cls, v: float) -> float:
        return _reject_nan(v)

    @model_validator(mode="after")
    def macro_calorie_consistency(self) -> "FoodItem":
        """Flag when declared calories diverge wildly from macro sum."""
        macro_kcal = self.protein * 4 + self.carbs * 4 + self.fat * 9
        if self.calories > 0 and macro_kcal > 0:
            diff = abs(self.calories - macro_kcal)
            threshold = max(100, self.calories * 0.25)
            if diff > threshold:
                # Don't hard-reject — lower confidence instead so the approval
                # flow forces user confirmation.
                # Use object.__setattr__ to avoid re-triggering validation
                # (which would cause infinite recursion with validate_assignment).
                object.__setattr__(self, "confidence", min(self.confidence, 0.3))
        return self


class ClarificationOption(BaseModel):
    label: str
    item_index: int | None = Field(default=None, ge=0, le=100)
    item_name: str | None = None
    calories_delta: float = 0
    protein_delta: float = 0
    carbs_delta: float = 0
    fat_delta: float = 0


class MealAnalysis(BaseModel):
    meal_name: str
    items: list[FoodItem]
    confidence: float = Field(ge=0, le=1)
    question: str | None = None
    options: list[ClarificationOption] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def totals(self) -> dict[str, float]:
        return {
            "calories": round(sum(item.calories for item in self.items), 1),
            "protein": round(sum(item.protein for item in self.items), 1),
            "carbs": round(sum(item.carbs for item in self.items), 1),
            "fat": round(sum(item.fat for item in self.items), 1),
        }

    def is_meaningful(self) -> bool:
        """True only if this is a real meal worth saving.

        Rejects empty item lists and all-zero meals (no calories AND no macros),
        which is how mis-routed text like "לא אלכוהול" used to slip through as a
        zero-calorie meal. A genuine 0-calorie drink still has no macros and is
        not auto-saved as a meal — the user is told it was not understood.
        """
        if not self.items:
            return False
        t = self.totals()
        return (t["calories"] + t["protein"] + t["carbs"] + t["fat"]) > 0


class MealCorrectionResult(BaseModel):
    meal_name: str
    items: list[FoodItem]
    notes: list[str] = Field(default_factory=list)

    def totals(self) -> dict[str, float]:
        return {
            "calories": round(sum(item.calories for item in self.items), 1),
            "protein": round(sum(item.protein for item in self.items), 1),
            "carbs": round(sum(item.carbs for item in self.items), 1),
            "fat": round(sum(item.fat for item in self.items), 1),
        }


class RoutineExtraction(BaseModel):
    wake_time: str | None = Field(default=None, description="e.g. '06:30'")
    sleep_time: str | None = Field(default=None, description="e.g. '23:00'")
    work_start: str | None = Field(default=None, description="e.g. '08:00'")
    work_end: str | None = Field(default=None, description="e.g. '17:00'")
    work_type: str | None = Field(default=None, description="office/physical/remote/shifts")
    commute_minutes: int | None = Field(default=None, ge=0, le=300)
    meal_break_time: str | None = Field(default=None, description="e.g. '12:30'")
    has_fridge_at_work: bool | None = None
    has_microwave_at_work: bool | None = None
    preferred_workout_time: str | None = Field(default=None, description="e.g. '19:00'")
    available_workout_minutes: int | None = Field(default=None, ge=0, le=300)
    cooking_willingness: str | None = Field(default=None, description="none/basic/moderate/enjoys")
    typical_meals_per_day: int | None = Field(default=None, ge=1, le=10)
    medication_appetite_note: str | None = Field(
        default=None,
        description="Appetite constraint related to medication, e.g. 'reduced appetite on Ritalin days'. Do NOT infer diagnosis or dosage.",
    )
    main_challenges: list[str] = Field(default_factory=list)
    additional_notes: str = ""


class HealthSample(BaseModel):
    external_id: str = Field(max_length=256)
    sample_type: str = Field(max_length=128)
    value: float
    unit: str = Field(max_length=32)
    start_time: datetime
    end_time: datetime | None = None
    source_device: str | None = Field(default=None, max_length=256)

    @field_validator("start_time", "end_time")
    @classmethod
    def require_timezone(cls, v: datetime | None) -> datetime | None:
        return _require_tz(v)

    @field_validator("value")
    @classmethod
    def finite_value(cls, v: float) -> float:
        return _reject_nan(v)

    @model_validator(mode="after")
    def end_after_start(self) -> "HealthSample":
        if self.end_time is not None and self.end_time < self.start_time:
            raise ValueError("end_time must be >= start_time")
        return self


class HealthBatch(BaseModel):
    telegram_user_id: int
    samples: list[HealthSample] = Field(min_length=1, max_length=1000)


class ShortcutHealthPayload(BaseModel):
    telegram_user_id: int
    measured_at: datetime
    weight_kg: float | None = Field(default=None, ge=20, le=400)
    steps: int | None = Field(default=None, ge=0, le=200000)
    active_calories: float | None = Field(default=None, ge=0, le=10000)
    sleep_minutes: int | None = Field(default=None, ge=0, le=1440)
    resting_heart_rate: float | None = Field(default=None, ge=20, le=250)
    hrv_ms: float | None = Field(default=None, ge=0, le=1000)

    @field_validator("measured_at")
    @classmethod
    def require_timezone(cls, v: datetime) -> datetime:
        return _require_tz(v)  # type: ignore[return-value]


class WatchSetPayload(BaseModel):
    telegram_user_id: int
    client_event_id: str = Field(max_length=256)
    reps: int = Field(ge=1, le=1000)
    # RIR is optional: a watch that does not collect it leaves it unset, which is
    # stored as "unknown" rather than a fabricated value.
    rir: int | None = Field(default=None, ge=0, le=5)
    weight: float | None = Field(default=None, ge=0, le=2000)


class WeeklyAvailabilitySlot(BaseModel):
    weekday: int = Field(ge=0, le=6)
    available: bool = True
    start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    minutes: int | None = Field(default=None, ge=15, le=180)


class MiniProfileUpdate(BaseModel):
    """Whitelisted profile fields editable from the Mini App.

    Internal state, goals and active plan IDs are deliberately absent, so a
    browser cannot mutate conversation state or bypass plan versioning.
    """

    work_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    work_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    work_type: str | None = Field(default=None, max_length=50)
    commute_minutes: int | None = Field(default=None, ge=0, le=300)
    meal_break_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    has_fridge: bool | None = None
    has_microwave: bool | None = None
    cooking_capacity: str | None = Field(default=None, max_length=50)
    food_budget_level: str | None = Field(default=None, max_length=50)
    meal_structure_preference: str | None = Field(default=None, max_length=80)
    training_location: str | None = Field(default=None, max_length=120)
    equipment: str | None = Field(default=None, max_length=1000)
    session_minutes: int | None = Field(default=None, ge=15, le=180)
    strength_experience: str | None = Field(default=None, max_length=50)
    weekly_availability: list[WeeklyAvailabilitySlot] | None = Field(default=None, max_length=7)
    diet_restrictions: str | None = Field(default=None, max_length=1000)
    allergies: str | None = Field(default=None, max_length=1000)
    coaching_style: str | None = Field(default=None, max_length=80)
    notification_preference: str | None = Field(default=None, max_length=80)

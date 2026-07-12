"""TASK-15 — a photo caption guides full image analysis, it does not replace it.

Captioned photos must go through analyze_meal_image (full visual detection with
the caption as guidance), NOT the text-correction reanalysis path that biased
the result toward the caption tokens. The full-image prompt instructs the model
to detect every visible component and not limit items to the caption words.
"""
from __future__ import annotations

import inspect

from noam_coach.bot import meals as meals_bot
from noam_coach.services import profile as profile_svc


def test_analyze_meal_image_accepts_caption_guidance() -> None:
    sig = inspect.signature(profile_svc.analyze_meal_image)
    assert "caption" in sig.parameters


def test_photo_handler_routes_caption_through_full_image_analysis() -> None:
    src = inspect.getsource(meals_bot)
    # Captioned photos call analyze_meal_image with a caption argument.
    assert "caption=caption or None" in src
    # They must NOT route captions through the text-correction reanalysis path.
    assert "reanalyze_meal_with_text_and_image(\n            image_path=str(path),\n            correction_text=caption" not in src


def test_prompt_treats_caption_as_guidance_not_inventory() -> None:
    src = inspect.getsource(profile_svc.analyze_meal_image)
    # The instruction that the caption guides and does not replace detection.
    assert "GUIDES visual analysis" in src
    assert "do not limit the detected items" in src
    assert "detect every other visible component" in src or "detect every" in src

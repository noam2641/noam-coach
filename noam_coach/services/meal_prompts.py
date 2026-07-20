"""Shared system-prompt building blocks for meal analysis (RE10-15 / D5).

Before this module, the three AI meal-analysis call sites in
``noam_coach/services/profile.py`` (``analyze_meal_image``,
``analyze_meal_text``, ``reanalyze_meal_with_text_and_image``) each inlined
their own system prompt. That duplication meant:

* No shared locale context — a photo of an Israeli snack like Bamba was
  described generically ("fried potato chips") because nothing told the
  model it should recognize common Israeli products/brands.
* The first-pass analyses (image/text) never received the user's allergies
  or dietary restrictions, unlike the re-analyze path — so the very first
  estimate could not flag a food as unsafe for that user.

This module centralizes both additions as small, composable text blocks so
any future prompt change lands once instead of three times.
"""

from __future__ import annotations

from typing import Any

# Locale guidance shared by every meal-analysis prompt. Deliberately generic
# (a short list of common products, not the whole israeli_foods.py catalog —
# the catalog is applied deterministically as a post-processing step instead
# of being pasted into the prompt) so it stays cheap and stable.
ISRAELI_LOCALE_BLOCK = (
    "The user is in Israel. Prefer recognizing common Israeli packaged foods "
    "and dishes by their real name instead of a generic visual description — "
    "for example: Bamba (במבה, a peanut-based puff snack — NOT potato), "
    "Bissli (ביסלי), cottage cheese (קוטג'), shoko (שוקו, chocolate milk), "
    "pitim/Israeli couscous (פתיתים), halva (חלווה), borekas (בורקס), "
    "jachnun (ג'חנון), sabich (סביח), falafel, shakshuka, malawach (מלאווח). "
    "If you recognize a specific branded/local product, return ITS real "
    "Hebrew name (not a generic description of its appearance).\n"
    # TASK-58 F: visual portion estimation must be evidence-driven, never a
    # jump from food identity straight to a habitual small gram number.
    "PORTION EVIDENCE — derive every gram estimate from what is actually "
    "visible, in this order: (1) countable units first (set quantity_count/"
    "quantity_unit); (2) the item's share of the plate area and the apparent "
    "pile height/volume (a side dish covering a third of a dinner plate in a "
    "real pile is typically 120-200g cooked, not 50g); (3) the food's density "
    "class and cooked/prepared state; (4) typical Israeli serving sizes as a "
    "sanity check. Do not default to minimal token quantities when the image "
    "shows a full serving; equally, do not inflate beyond visible evidence. "
    "When the visual evidence is weak, express that honestly through the "
    "item's confidence and quantity_source='estimate' instead of inventing "
    "false precision.\n"
    # TASK-58 G: bone-in portions.
    "BONE-IN MEAT/POULTRY: name the visible cut when the image supports it "
    "(שוק עוף, ירך עוף, רבע עוף, כרעיים, כנפיים; עם עור/בלי עור). The grams/"
    "calories/protein you return must describe the estimated EDIBLE COOKED "
    "MEAT (without bone), and the item name should carry the cut so the "
    "serving is not reported as a generic small piece. A large bone-in "
    "quarter is materially more than a small boneless fillet — reflect that. "
    "Do not force an anatomical label the image cannot support.\n"
    # TASK-58 C/H: ambiguous spreads/pastes.
    "AMBIGUOUS SPREADS/PASTES (טחינה, ממרחים, סלטים): if you can only see "
    "a generic spread, return the GENERIC name (e.g. טחינה, ממרח) — never "
    "commit to a specific high-calorie variant (טחינה גולמית) on visual "
    "evidence alone. If such an item is a large share of the meal's "
    "calories, prefer asking the one allowed targeted question about it."
)


def safety_context_block(nutrition_context: dict[str, Any] | None) -> str:
    """Build a short safety-only context block for the FIRST-pass analyses.

    RE10-15 / D5: analyze_meal_image and analyze_meal_text used to receive no
    user context at all, so an allergy could not be flagged on the very first
    estimate. This mirrors (a strict subset of) what
    reanalyze_meal_with_text_and_image already sends, restricted to safety
    fields only — it must never be used to change food identification, only
    to carry allergy/restriction awareness forward into item notes.
    """
    if not nutrition_context:
        return ""
    allergies = nutrition_context.get("allergies")
    restrictions = nutrition_context.get("diet_restrictions")
    if not allergies and not restrictions:
        return ""
    parts = []
    if allergies:
        parts.append(f"allergies: {allergies}")
    if restrictions:
        parts.append(f"dietary restrictions: {restrictions}")
    return (
        "\nUser safety context (for awareness only — do not change food "
        "identification based on this, only flag it in `notes` if a "
        "recognized item conflicts with it): " + "; ".join(parts)
    )

# TASK 58 - Meal Image Identity Correction and Portion Estimation

## Status

Open production incident and implementation task.

## Instruction

Do not patch only the specific foods in the example. Investigate and fix the
underlying meal-image interpretation and correction architecture.

## Incident

### Meal Image Identity Correction and Systematic Portion Under-Estimation

### Observed Production Case

Initial image contains, based on confirmed user evidence:

- a large bone-in roasted chicken quarter / leg portion with skin
- cooked white rice
- cooked green beans
- chopped cucumber
- chopped tomato
- eggplant with mayonnaise

Initial bot result:

- roasted chicken - 1 piece - 330 kcal - 31g protein
- green beans - 70g - 20 kcal
- cooked white rice - 50g - 70 kcal
- chopped tomato/cucumber salad - 80g - 20 kcal
- raw tahini - 50g - 298 kcal

Total:

- 738 kcal
- 44g protein

The user then explicitly corrected:

```text
לא טחינה חציל במיונז
```

Meaning:

```text
Not tahini. Eggplant with mayonnaise.
```

The corrected bot result still contained:

```text
טחינה גולמית - 25 גרם - 149 קל׳
```

The unrelated items remained approximately:

- chicken - 330 kcal / 31g protein
- green beans - 70g
- rice - 50g
- salad - 80g

Corrected total:

- 589 kcal
- 39g protein

This incident exposes multiple independent failures.

## A. User Food Identity Correction Is Not a Hard Constraint

Current evidence in the repository:

`reanalyze_meal_with_text_and_image` explicitly tells the model:

- the user's text is authoritative
- if the user names a specific food, use that food identity
- "לא X" means X is a rejected identification
- rejected identification must not be retained

However, deterministic code only has explicit hard enforcement for user
quantities through `_enforce_user_quantities`.

There is no equivalent deterministic enforcement of food identity
replacement/rejection.

Implement a structured identity-correction mechanism.

Required behavior:

User correction:

```text
לא טחינה חציל במיונז
```

must produce structured semantics equivalent to:

```text
target_item_identity = "טחינה"
rejected_identity = "טחינה"
confirmed_identity = "חציל במיונז"
operation = REPLACE_ITEM_IDENTITY
```

The correction must become a hard constraint for the current meal draft.

Invariant:

Once a user explicitly rejects an item identity in the current meal lifecycle,
the rejected identity or its canonical aliases must not return for that item
unless the user explicitly reverses the correction.

Examples:

```text
לא טחינה, חציל במיונז
זה עוף, לא הודו
זו קולה זירו ולא קולה רגילה
זה קוטג' ולא גבינה לבנה
```

must be handled as item identity corrections, not as generic full-meal
re-prompts.

Do not rely only on prompt compliance.

Enforce the identity correction after AI parsing and after deterministic food
normalization.

## B. Corrections Must Be Item-Scoped

The current correction path re-analyzes the image and allows unrelated
quantities to be estimated again.

In the observed case:

```text
50g tahini became 25g tahini even though the user did not correct the quantity.
```

This proves that the correction path is performing broad reinterpretation
rather than a controlled item edit.

Required behavior:

For an item-scoped correction such as:

```text
לא טחינה חציל במיונז
```

identify the target item and replace/recalculate only that item.

Preserve unrelated meal evidence by default:

- chicken identity and quantity
- rice identity and quantity
- green bean identity and quantity
- salad identity and quantity

Do not silently re-estimate unrelated items.

Full meal re-analysis is allowed only when the correction genuinely changes the
interpretation of the entire meal.

Design the distinction explicitly.

Possible correction scopes:

- `ITEM_IDENTITY`
- `ITEM_QUANTITY`
- `ITEM_PREPARATION`
- `ITEM_ADDITION`
- `ITEM_REMOVAL`
- `WHOLE_MEAL_REINTERPRETATION`

Use engineering judgment for the final model, but preserve this semantic
distinction.

## C. Israeli Food Override Safety

Audit `_apply_israeli_food_overrides` and `israeli_foods.lookup`.

The current curated entry effectively maps the generic alias:

```text
טחינה
```

to the canonical identity:

```text
טחינה גולמית
```

and then applies raw tahini macros:

```text
595 kcal / 100g
```

This is unsafe for image interpretation.

In normal Israeli usage, "טחינה" may refer to:

- raw tahini paste
- prepared tahini
- diluted restaurant tahini
- another visually similar spread

Required changes:

1. Remove generic "טחינה" as a deterministic alias for raw tahini paste.
2. Keep raw tahini matching specific:
   - טחינה גולמית
   - raw tahini
   - tahini paste
   - and other genuinely specific equivalents
3. Represent prepared tahini separately if deterministic values are retained.
4. Ambiguous generic food names must not trigger a high-confidence
   deterministic macro override.
5. `_apply_israeli_food_overrides` currently documents "confident match"
   behavior but does not inspect `FoodItem.confidence` before replacing macros.
   Audit this contract mismatch.

Do not blindly add a confidence threshold without understanding confidence
calibration.

Design an evidence-aware canonicalization policy.

The deterministic override must respect:

- explicit current user identity
- locked prior corrections
- rejected identities
- confirmed identities
- evidence source
- ambiguity of the alias

## D. Evidence Precedence Must Be Enforced in Code

The system already describes an evidence precedence concept in prompts.

Make the precedence operational and testable.

Required semantic order:

1. explicit current user correction
2. locked prior user corrections
3. user-confirmed item identity/quantity
4. trusted deterministic known-food evidence
5. current image inference
6. generic estimation

A lower-level evidence source must never overwrite a higher-level fact.

Do not leave this only as prompt text.

## E. Portion Estimation Audit

The same production image shows a possible systematic visual portion
under-estimation pattern.

Observed estimates:

- cooked white rice: 50g
- green beans: 70g
- tomato/cucumber salad: 80g
- corrected spread estimate: 25g

Independent visual review of the exact image suggests materially larger
plausible ranges:

- cooked white rice: approximately 100-140g
- green beans: approximately 130-170g
- chopped cucumber + tomato: approximately 140-190g combined
- eggplant with mayonnaise: approximately 90-130g

The image also appears to contain a large bone-in chicken quarter / leg portion,
while the bot reports only:

```text
עוף צלוי - 1 חתיכה
```

330 kcal / 31g protein.

Do not hard-code the independent estimates above. They are incident evidence
and expected plausible ranges, not deterministic ground truth.

Investigate whether the current prompt/model/schema produces systematic portion
compression.

Audit:

- image analysis prompt
- `FoodItem` quantity representation
- `quantity_source`
- `quantity_count` / `quantity_unit`
- confidence fields
- cooked-weight instruction
- meal correction path
- learned-food calibration
- any formatting or post-processing that modifies grams
- model and structured-output constraints

Determine whether the model is jumping directly from food identity to grams
without representing enough visual quantity evidence.

## F. Improve Visual Quantity Representation

Do not solve this with a global multiplier such as:

```python
estimated_grams *= 1.5
```

and do not simply prompt:

```text
estimate larger portions.
```

The solution must improve evidence quality.

Consider a structured internal quantity estimate containing concepts such as:

- visible portion class
- count when countable
- estimated plate-area share
- apparent pile height / volume class
- bone-in versus edible-weight distinction
- density / food class
- prepared/cooked state
- quantity confidence
- quantity evidence source

You do not need to expose these fields to the Telegram user.

Use engineering judgment to decide the smallest robust model.

The final user-facing value may still be grams, but the system should not jump
from visual recognition directly to false-precision grams without representing
uncertainty/evidence.

## G. Bone-In Chicken Handling

The observed output:

```text
עוף צלוי - 1 חתיכה
```

is not sufficiently informative for a large bone-in portion.

Audit how the system handles:

- drumstick
- thigh
- leg quarter
- chicken quarter
- bone-in pieces
- skin-on versus skinless

Do not force anatomical classification when the image does not support it.

However, when the difference materially affects edible weight, calories, or
protein, generic "1 piece" must not create false confidence.

The estimate should distinguish, where possible:

- visible serving weight
- estimated edible cooked weight

## H. High-Calorie Uncertainty Gate

The original tahini item contributed:

```text
298 / 738 kcal
```

approximately 40% of the entire estimated meal.

Yet it was based on an incorrect visual identity.

The existing prompt requests one targeted clarification when uncertainty
materially changes calories by more than 15%.

This is currently dependent on model compliance.

Design a deterministic or hybrid quality gate for high-impact uncertain items.

The system should consider clarification when:

- an unconfirmed item has large calorie contribution
- plausible alternative identities create more than 15% meal-total variance
- the identity is visually ambiguous
- deterministic canonicalization would significantly amplify the estimate

Example desired question:

```text
הממרח האפור בצד הוא חציל במיונז, טחינה מוכנה או משהו אחר?
```

Do not ask a generic question when a more targeted image-grounded question is
possible.

## I. Regression Tests

Add regression coverage for the exact production failure.

At minimum test:

### Case 1 - Rejected Identity Cannot Return

Initial item:

```text
טחינה / טחינה גולמית
```

User:

```text
לא טחינה חציל במיונז
```

Expected:

- no item named טחינה
- no item named טחינה גולמית
- an item representing חציל במיונז exists
- deterministic Israeli-food overrides cannot restore tahini

### Case 2 - Unrelated Items Remain Unchanged

Initial draft:

- chicken
- green beans 70g
- rice 50g
- salad 80g
- tahini 50g

Correction:

```text
לא טחינה חציל במיונז
```

Expected:

- unrelated items and their quantities remain unchanged
- only the target item is replaced/recalculated

### Case 3 - Correction Survives Lifecycle

The confirmed identity must survive:

- post-processing
- known-food override
- persistence
- another unrelated correction
- formatting
- save/approval

### Case 4 - Generic Tahini Does Not Canonicalize to Raw Tahini

Input identity:

```text
טחינה
```

Expected:

must not automatically receive raw tahini paste macros solely because of the
generic word.

### Case 5 - Explicit Raw Tahini Still Matches

Input:

```text
טחינה גולמית
```

Expected:

trusted deterministic raw tahini match remains available.

### Case 6 - High-Impact Uncertain Visual Item

An uncertain spread represents a large percentage of total meal calories.

Expected:

the system emits or requires one targeted clarification rather than silently
hard-canonicalizing the item.

## J. Implementation Process

First inspect the current implementation and tests.

Trace the complete path:

```text
Telegram photo
-> handle_photo
-> analyze_meal_image
-> MealAnalysis
-> Israeli food overrides
-> draft persistence
-> free-text correction routing
-> reanalyze_meal_with_text_and_image
-> locked corrections
-> quantity enforcement
-> formatting
-> save/approval
```

Do not assume the root cause is limited to `profile.py`.

Identify the exact existing domain model for meal drafts and corrections.

Then implement the smallest coherent architectural fix.

Do not create a parallel third correction-state system.

Reuse and unify existing meal correction/lifecycle mechanisms where possible.

Preserve existing FIX 1-57 behavior.

Run focused regression tests first, then the relevant meal/photo/correction
suites, then the full test suite if the environment permits.

Commit the completed work as one coherent fix or a small dependency-ordered
commit series.

## Completion Report Required

At the end report:

1. confirmed root causes
2. files changed
3. architecture chosen
4. exact precedence/invariants now enforced
5. tests added
6. tests run and results
7. remaining risks, especially visual portion estimation limits

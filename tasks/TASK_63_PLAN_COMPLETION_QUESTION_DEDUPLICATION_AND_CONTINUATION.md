# TASK 63 - Plan Completion Question Deduplication and Continuation

## Status

Open product/implementation task.

## Source Incident

During nutrition plan completion, the bot repeatedly asks:

```text
איזה מזונות עושים לך רגישות או אסורים לך? (אפשר לכתוב חופשי)
```

The user answers with food items such as:

```text
אגוזים
```

The same allergy/sensitivity question appears again instead of advancing
cleanly to the next missing question. The user also challenges the repeated
question and asks why the bot is asking again after the answer was already
given.

## Problem

The plan-completion flow should be a linear, stateful wizard:

```text
ask missing fact
-> record answer
-> mark that fact complete
-> ask next missing fact
```

In practice, the screenshots show that a typed answer can leave the user on the
same question or cause the same missing fact to be re-rendered. Existing code
has several overlapping mechanisms:

- active conversation flow
- `PENDING_QUESTION`
- plan-completion flow state
- free-text fallback handling
- dietary restriction classification follow-up
- redundant-question challenge handling

The bug is likely in the handoff between these mechanisms, not in the text of
the allergy question itself.

## Required Product Behavior

Once the user provides a valid answer to a plan-completion question:

- the answer must be persisted under the correct fact
- the pending question must be cleared or advanced
- the same question must not be asked again unless the user explicitly chooses
  to edit it
- if additional classification is needed, it must be presented as a
  sub-question for the same answer, not as a full repetition of the original
  question
- after classification is complete, the flow must continue to the next missing
  fact

If the user says "I already answered", "you already asked", or similar, the bot
should inspect the active pending question and stored fact before deciding what
to ask next.

## Proposed Solution

Audit the complete plan-completion answer path:

```text
ask_next_plan_completion_question
-> set_pending
-> handle_onboarding_text
-> free_text_fallback / dietary parsing
-> questions.record_answer / user_model.set_fact
-> clear_pending
-> advance_after_answer
-> continue_after_plan_completion_answer
-> ask_next_plan_completion_question
```

Implement a single invariant:

```text
after a successful answer save, the next rendered question must not have the
same fact_key unless a structured clarification for that same answer is still
unresolved
```

## What Not To Do

- Do not hide the question without saving the answer.
- Do not treat every free-text answer as a generic preference if it belongs to
  an active wizard question.
- Do not ask the same question again just because dietary classification is
  still pending.
- Do not reset the whole plan-completion flow after one answer.
- Do not solve this by hard-coding "אגוזים".

## Acceptance Criteria

1. Answering the allergies/sensitivities question with "אגוזים" records the
   answer and does not immediately re-ask the same top-level question.
2. If classification is required, the bot asks "how to treat אגוזים" once and
   then continues.
3. Answering "אין אלרגיות/רגישויות" marks the fact complete and advances.
4. The same invariant holds for other free-text fallback questions such as
   equipment and training limitations.
5. Redundant-question challenge handling uses stored facts to continue or
   explain, not to restart the same question.
6. Regression tests cover button answers, free-text answers, classification
   sub-flows, and repeated-question complaints.

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. flow-state invariant enforced
4. affected question types
5. tests added
6. tests run and results
7. remaining risks

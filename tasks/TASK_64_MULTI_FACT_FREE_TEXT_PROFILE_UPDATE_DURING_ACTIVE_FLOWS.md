# TASK 64 - Multi-Fact Free-Text Profile Update During Active Flows

## Status

Open product/implementation task.

## Source Incident

While the bot is asking or confirming profile/Health-derived data, the user
types a compact correction containing multiple facts, for example:

```text
גובה 174 אימונים מזוהים 4
```

The expected behavior is that the bot should update height and the workout
frequency / detected workout count when those facts are clear, then continue
the active flow. Instead, the screenshot shows the flow remaining around profile
confirmation and pattern approval, with the user asking why height appears in
the confirmation stage and why the bot recommends sleep / workout values in an
unclear way.

## Problem

Free-text handling currently tends to be routed through one active pending
question or one detected intent. That works for single-fact answers, but it
breaks when the user naturally provides multiple corrections in one message.

Examples:

- "גובה 174 אימונים מזוהים 4"
- "הגובה 174, משקל 101.8"
- "4 אימונים בשבוע, 50 דקות, ראשון שני רביעי שישי"
- "שינה 00:20-06:50 וגובה 174"

The system already has parsers for some individual facts, but it needs a
transactional multi-fact extraction path that can run during active profile,
Health confirmation, and plan-completion flows.

## Required Product Behavior

When a user sends a free-text message with multiple recognizable facts:

- extract every high-confidence fact
- validate each fact with its normal range and domain rules
- save each fact under the canonical fact key
- confirm what was updated in one concise response
- continue the currently active flow from the next unresolved item
- leave ambiguous fragments unresolved and ask only about those

If the message contains both an answer to the active question and additional
facts, the active question must still be completed correctly.

## Proposed Solution

Introduce a shared multi-fact profile update parser used before falling back to
single-question handling when the text clearly contains multiple fact labels.

The parser should support at least:

- height
- weight
- body fat
- weekly workout frequency
- workout duration
- workout weekdays
- workout time
- sleep window
- allergies / restrictions when explicitly labeled

The result should be structured:

```text
recognized_facts
ambiguous_fragments
active_question_answer
confidence
```

Then route the result through existing `user_model.set_fact`,
availability-saving, and flow-continuation mechanisms.

## What Not To Do

- Do not bypass existing validation ranges.
- Do not overwrite user-confirmed facts with ambiguous text.
- Do not treat every number in the message as the active question answer.
- Do not ignore the active flow after saving side facts.
- Do not create duplicate fact keys.
- Do not hard-code only the screenshot phrase.

## Acceptance Criteria

1. "גובה 174 אימונים מזוהים 4" updates `height_cm` and the appropriate workout
   frequency fact without corrupting the active flow.
2. Multiple numeric facts in one message are assigned by nearby labels, not by
   position alone.
3. Active question answers still complete the active question.
4. Ambiguous values trigger a targeted clarification instead of a silent save.
5. The response lists the facts that were updated.
6. Plan-completion, Health confirmation, and profile-edit flows continue after
   the update.
7. Regression tests cover mixed Hebrew labels, multiple numbers, active
   pending question context, and invalid ranges.

## Completion Report Required

At implementation completion, report:

1. root cause
2. files changed
3. parser architecture
4. supported fact keys
5. flow-continuation behavior
6. tests added
7. tests run and results
8. remaining risks

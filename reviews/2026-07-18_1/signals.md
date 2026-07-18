# Deterministic signal scan

Detector set version: 1.0

## [HIGH] interaction_no_output × 2
User input with no delivered output, state change, or view render.
- events [275] · interaction in_1bc6bde7ca064f3a · {"kind": "callback"}
- events [601] · interaction in_558c5fea44be4afc · {"kind": "callback"}

## [MEDIUM] repeated_user_input × 4
Consecutive identical user inputs (retry/frustration evidence).
- events [66, 75, 84] · interaction in_3cf6446463474be7 · {"kind": "callback", "input_digest": "health:skip_item@1606", "repetitions": 3, "interaction_ids": ["in_3cf6446463474be7", "in_e4fd15aeadd84ddf", "in_76cdb180bb5b4877"]}
- events [244, 256] · interaction in_26e21a5aadf14424 · {"kind": "callback", "input_digest": "planv2:generate:workout@1621", "repetitions": 2, "interaction_ids": ["in_26e21a5aadf14424", "in_a779031cb4e64422"]}
- events [267, 275] · interaction in_a3d9ebee13a74564 · {"kind": "callback", "input_digest": "planv2:wiz_type:consistency:ff-322493274-a13bfe366328:v39@1621", "repetitions": 2, "interaction_ids": ["in_a3d9ebee13a74564", "in_1bc6bde7ca064f3a"]}
- events [590, 601] · interaction in_862d943ee4be485d · {"kind": "callback", "input_digest": "approve_meal:FsfP5UUdPsk@1637", "repetitions": 2, "interaction_ids": ["in_862d943ee4be485d", "in_558c5fea44be4afc"]}

## [MEDIUM] user_correction × 1
Explicit user correction of produced output (undo / analysis override).
- events [584] · interaction in_8fc643f35fae4e9e · {"correction": "analysis_override", "entity": "meal_analysis", "override_count": 2}

_Signals are a floor and a navigation aid — repetition alone never proves a shared root cause; the review protocol owns judgment._

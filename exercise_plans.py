"""Exercise plan templates, constants, and the exercise builder.

This module is pure data — no database or network dependencies.
"""

from __future__ import annotations

from typing import Any

from noam_coach.services.weekdays import WEEKDAY_NAMES_HE as _WEEKDAY_NAMES_HE
from noam_coach.services.weekdays import weekday_he as _weekday_he

WEEKDAY_NAMES_HE = _WEEKDAY_NAMES_HE


def weekday_he(idx: int) -> str:
    return _weekday_he(idx)

# Primary muscle per exercise id (used for the mind-muscle cue).
EXERCISE_MUSCLES = {
    "bench": "חזה",
    "incline_db": "חזה עליון",
    "fly": "חזה",
    "incline_curl": "יד קדמית",
    "lat_pull": "גב רחב",
    "lat_pull_fb": "גב",
    "one_arm_row": "גב",
    "rope_push": "יד אחורית",
    "military": "כתפיים",
    "squat": "רגליים",
    "lateral": "כתף צדית",
    "bar_curl": "יד קדמית",
    "leg_press": "רגליים",
    "crossover": "חזה",
    "chest_machine": "חזה",
    "rdl": "שרשרת אחורית",
}


def exercise(
    exercise_id: str,
    name: str,
    sets: int,
    reps_min: int,
    reps_max: int,
    rest: int,
    weight: float,
    increment: float,
    cues: list[str],
    alternatives: list[tuple[str, str, float]],
    muscle: str = "",
) -> dict[str, Any]:
    return {
        "id": exercise_id,
        "name": name,
        "sets": sets,
        "rmin": reps_min,
        "rmax": reps_max,
        "rest": rest,
        "weight": weight,
        "inc": increment,
        "cues": cues,
        # primary worked muscle — for mind-muscle connection
        "muscle": muscle or EXERCISE_MUSCLES.get(exercise_id, ""),
        "alts": [
            {"id": alt_id, "name": alt_name, "weight": alt_weight}
            for alt_id, alt_name, alt_weight in alternatives
        ],
    }


PLANS: dict[str, dict[str, Any]] = {
    "A": {
        "name": "אימון A — חזה ויד קדמית",
        "exercises": [
            exercise(
                "bench",
                "לחיצת חזה עם מוט",
                4,
                8,
                10,
                150,
                50,
                2.5,
                ["שכמות לאחור ולמטה", "כפות רגליים יציבות", "הורדה בשליטה"],
                [
                    ("chest_machine", "Chest Press", 55),
                    ("db_bench", "לחיצה עם משקולות", 22.5),
                    ("smith_bench", "לחיצה בסמית׳", 45),
                ],
            ),
            exercise(
                "incline_db",
                "לחיצה בשיפוע עם משקולות",
                4,
                8,
                10,
                150,
                20,
                2,
                ["שיפוע מתון", "חזה פתוח", "ללא נעילת מרפקים"],
                [
                    ("incline_machine", "מכונת לחיצה בשיפוע", 45),
                    ("incline_smith", "לחיצה בשיפוע בסמית׳", 40),
                    ("incline_bar", "לחיצה בשיפוע עם מוט", 40),
                ],
            ),
            exercise(
                "fly",
                "פרפר עם משקולות",
                4,
                10,
                12,
                120,
                12.5,
                2,
                ["כיפוף קל במרפק", "טווח ללא כאב", "סגירה דרך החזה"],
                [
                    ("pec_deck", "Pec Deck", 40),
                    ("cable_fly", "פרפר בכבלים", 12.5),
                    ("one_arm_fly", "פרפר בכבל ביד אחת", 7.5),
                ],
            ),
            exercise(
                "incline_curl",
                "כפיפת מרפק בשיפוע",
                4,
                10,
                12,
                120,
                10,
                2,
                ["מרפקים יציבים", "בלי תנופה", "יישור מבוקר"],
                [
                    ("cable_curl", "כפיפה בכבל", 20),
                    ("preacher", "Preacher Curl", 20),
                    ("db_curl", "כפיפה בעמידה", 10),
                ],
            ),
        ],
    },
    "B": {
        "name": "אימון B — גב ויד אחורית",
        "exercises": [
            exercise(
                "lat_pull",
                "משיכה רחבה לחזה",
                4,
                10,
                12,
                150,
                54,
                4.5,
                ["חזה פתוח", "משיכה לחזה", "לא מאחורי הראש"],
                [
                    ("assisted_pullup", "מתח בסיוע", 35),
                    ("neutral_pull", "פולי ניטרלי", 50),
                    ("one_arm_pull", "משיכה ביד אחת", 22.5),
                ],
            ),
            exercise(
                "one_arm_row",
                "חתירה יד אחת",
                4,
                10,
                12,
                120,
                25,
                2.5,
                ["גב ניטרלי", "מרפק לכיוון האגן", "בלי סיבוב גו"],
                [
                    ("cable_row", "חתירה בכבל", 45),
                    ("supported_row", "חתירה עם תמיכת חזה", 20),
                    ("row_machine", "מכונת חתירה", 45),
                ],
            ),
            exercise(
                "rope_push",
                "פשיטת מרפק בחבל",
                4,
                10,
                12,
                120,
                15,
                2.5,
                ["מרפקים צמודים", "יישור מלא", "בלי תנופה"],
                [
                    ("bar_push", "פשיטה במוט", 20),
                    ("one_arm_push", "פשיטה ביד אחת", 7.5),
                    ("dip_machine", "מכונת מקבילים", 35),
                ],
            ),
        ],
    },
    "C": {
        "name": "אימון C — כתפיים ורגליים",
        "exercises": [
            exercise(
                "military",
                "לחיצת כתפיים עם מוט",
                4,
                8,
                10,
                150,
                35,
                2.5,
                ["בטן וישבן מכווצים", "מוט קרוב לפנים", "לא לקשת גב"],
                [
                    ("db_shoulder", "לחיצה עם משקולות", 15),
                    ("shoulder_machine", "מכונת כתפיים", 35),
                    ("landmine", "Landmine Press", 20),
                ],
            ),
            exercise(
                "squat",
                "סקוואט עם מוט",
                4,
                8,
                10,
                180,
                40,
                5,
                ["כף רגל מלאה", "ברכיים בכיוון האצבעות", "עומק בשליטה"],
                [
                    ("hack", "Hack Squat", 60),
                    ("smith_squat", "סקוואט בסמית׳", 45),
                    ("leg_press", "Leg Press", 90),
                ],
            ),
            exercise(
                "lateral",
                "הרחקת כתפיים לצדדים",
                4,
                10,
                12,
                90,
                7.5,
                1,
                ["מרפק מוביל", "כתפיים למטה", "טווח נוח"],
                [
                    ("cable_lateral", "הרחקה בכבל", 5),
                    ("lateral_machine", "מכונת הרחקה", 25),
                    ("lean_lateral", "הרחקה בהטיה", 6),
                ],
            ),
        ],
    },
    "F": {
        "name": "אימון גוף מלא",
        "exercises": [
            # Quad-dominant (knee flexion)
            exercise(
                "leg_press",
                "Leg Press",
                3,
                10,
                12,
                180,
                90,
                10,
                ["גב ואגן צמודים", "ברכיים בכיוון האצבעות", "לא לנעול"],
                [
                    ("hack", "Hack Squat", 60),
                    ("smith_squat", "סקוואט בסמית׳", 45),
                    ("goblet", "Goblet Squat", 25),
                ],
                muscle="רגליים",
            ),
            # Hinge / posterior chain
            exercise(
                "rdl",
                "דדליפט רומני עם משקולות",
                3,
                10,
                12,
                150,
                20,
                2.5,
                ["גב ניטרלי", "ירידה בשליטה", "מתיחה ב-hamstrings"],
                [
                    ("hip_thrust", "Hip Thrust", 60),
                    ("back_ext", "היפר-אקסטנשן", 0),
                    ("cable_pull_through", "Pull-Through בכבל", 25),
                ],
                muscle="שרשרת אחורית",
            ),
            # Horizontal push — compound pressing is preferred over an isolation fly
            # in a full-body session because it trains chest, triceps and anterior deltoid.
            exercise(
                "chest_machine",
                "לחיצת חזה במכונה",
                3,
                8,
                12,
                150,
                45,
                5,
                ["שכמות יציבות", "מרפקים בזווית נוחה", "עצור לפני כאב בכתף"],
                [
                    ("bench", "לחיצת חזה עם מוט", 40),
                    ("db_bench", "לחיצה עם משקולות", 17.5),
                    ("pushup", "שכיבות סמיכה", 0),
                ],
                muscle="חזה",
            ),
            # Vertical / horizontal pull (back)
            exercise(
                "lat_pull_fb",
                "משיכה רחבה לחזה",
                3,
                10,
                12,
                120,
                45,
                4.5,
                ["חזה פתוח", "משיכה לחזה", "לא מאחורי הראש"],
                [
                    ("assisted_pullup", "מתח בסיוע", 35),
                    ("cable_row", "חתירה בכבל", 40),
                    ("one_arm_row", "חתירה ביד אחת", 20),
                ],
                muscle="גב",
            ),
            # Vertical push — rounds out the main movement patterns.  Arms still
            # receive meaningful indirect work from the presses and pulls.
            exercise(
                "military",
                "לחיצת כתפיים",
                2,
                8,
                10,
                120,
                25,
                2.5,
                ["בטן מכווצת", "טווח ללא כאב", "לא לקשת את הגב"],
                [
                    ("db_shoulder", "לחיצה עם משקולות", 12.5),
                    ("shoulder_machine", "מכונת כתפיים", 30),
                    ("landmine", "Landmine Press", 15),
                ],
                muscle="כתפיים",
            ),
        ],
    },
}



# TASK-07: Professional 4-day structures.  The original four-day split used
# A/B/C/F only, and the consistency candidate sometimes dropped a requested day.
# These variants let the plan generator respect all four active days while
# offering Full Body and Upper/Lower structures with varied exercises.
PLANS.update({
    "FB1": {
        "name": "Full Body 1 — בסיס כוח",
        "exercises": [
            exercise("leg_press", "Leg Press", 3, 10, 12, 150, 90, 10, ["טווח ללא כאב", "ברכיים בכיוון האצבעות"], [("goblet", "Goblet Squat", 22.5), ("hack", "Hack Squat", 55)], muscle="רגליים"),
            exercise("chest_machine", "לחיצת חזה במכונה", 3, 8, 12, 120, 45, 5, ["שכמות יציבות", "מרפקים בזווית נוחה"], [("db_bench", "לחיצה עם משקולות", 17.5), ("pushup", "שכיבות סמיכה", 0)], muscle="חזה"),
            exercise("lat_pull_fb", "משיכה רחבה לחזה", 3, 10, 12, 120, 45, 4.5, ["חזה פתוח", "משיכה לחזה"], [("neutral_pull", "פולי ניטרלי", 45), ("band_row", "חתירה עם גומייה", 0)], muscle="גב"),
            exercise("dead_bug", "Dead Bug", 2, 8, 12, 60, 0, 0, ["גב תחתון יציב", "נשימה בשליטה"], [], muscle="ליבה"),
        ],
    },
    "FB2": {
        "name": "Full Body 2 — שרשרת אחורית ומשיכה",
        "exercises": [
            exercise("rdl", "דדליפט רומני", 3, 8, 10, 150, 35, 2.5, ["גב ניטרלי", "תנועה מהאגן"], [("hip_thrust", "Hip Thrust", 55), ("cable_pull_through", "Pull-Through בכבל", 25)], muscle="שרשרת אחורית"),
            exercise("db_bench", "לחיצה עם משקולות", 3, 8, 12, 120, 17.5, 2, ["שליטה מלאה", "טווח ללא כאב"], [("chest_machine", "Chest Press", 45), ("incline_pushup", "שכיבות סמיכה בשיפוע", 0)], muscle="חזה"),
            exercise("cable_row", "חתירה בכבל", 3, 10, 12, 120, 40, 5, ["גב ארוך", "מרפקים לאחור"], [("row_machine", "מכונת חתירה", 40), ("band_row", "חתירה עם גומייה", 0)], muscle="גב"),
            exercise("lateral", "הרחקת כתפיים", 2, 12, 15, 75, 7.5, 1, ["מרפק מוביל", "בלי כאב כתף"], [("cable_lateral", "הרחקה בכבל", 5), ("lateral_machine", "מכונת הרחקה", 20)], muscle="כתפיים"),
        ],
    },
    "FB3": {
        "name": "Full Body 3 — סקוואט קל ודחיפה",
        "exercises": [
            exercise("goblet", "Goblet Squat", 3, 10, 12, 120, 22.5, 2.5, ["חזה פתוח", "טווח ללא כאב"], [("chair_squat", "קימה מכיסא", 0), ("leg_press", "Leg Press", 80)], muscle="רגליים"),
            exercise("incline_machine", "לחיצה בשיפוע במכונה", 3, 8, 12, 120, 40, 5, ["שכמות יציבות", "מרפקים נוחים"], [("incline_db", "לחיצה בשיפוע עם משקולות", 17.5), ("incline_pushup", "שכיבות סמיכה בשיפוע", 0)], muscle="חזה עליון"),
            exercise("neutral_pull", "משיכה ניטרלית", 3, 10, 12, 120, 45, 5, ["כתפיים נמוכות", "משיכה לשליטה"], [("lat_pull_fb", "משיכה רחבה", 40), ("band_row", "חתירה עם גומייה", 0)], muscle="גב"),
            exercise("rope_push", "פשיטת מרפק בחבל", 2, 10, 12, 75, 15, 2.5, ["מרפקים צמודים", "ללא כאב מרפק"], [("one_arm_push", "פשיטה ביד אחת", 7.5), ("bar_push", "פשיטה במוט", 20)], muscle="יד אחורית"),
        ],
    },
    "FB4": {
        "name": "Full Body 4 — התאוששות אקטיבית",
        "exercises": [
            exercise("hip_thrust", "Hip Thrust", 3, 8, 12, 120, 55, 5, ["סנטר קל פנימה", "כיווץ ישבן בסוף"], [("glute_bridge", "גשר ישבן", 0), ("back_ext", "היפר-אקסטנשן", 0)], muscle="ישבן"),
            exercise("shoulder_machine", "לחיצת כתפיים במכונה", 3, 8, 12, 120, 30, 2.5, ["לא לנעול", "טווח ללא כאב"], [("landmine", "Landmine Press", 15), ("wall_push", "דחיפה מול קיר", 0)], muscle="כתפיים"),
            exercise("one_arm_row", "חתירה יד אחת", 3, 10, 12, 120, 20, 2.5, ["גב ניטרלי", "מרפק לכיוון האגן"], [("cable_row", "חתירה בכבל", 40), ("row_machine", "מכונת חתירה", 40)], muscle="גב"),
            exercise("cable_pull_through", "Pull-Through בכבל", 2, 10, 12, 90, 25, 2.5, ["תנועה מהאגן", "שליטה מלאה"], [("glute_bridge", "גשר ישבן", 0), ("dead_bug", "Dead Bug", 0)], muscle="שרשרת אחורית"),
        ],
    },
    "U1": {
        "name": "Upper 1 — חזה וגב",
        "exercises": [
            exercise("chest_machine", "לחיצת חזה במכונה", 3, 8, 12, 120, 45, 5, ["שכמות יציבות"], [("db_bench", "לחיצה עם משקולות", 17.5)], muscle="חזה"),
            exercise("lat_pull", "משיכה רחבה לחזה", 3, 10, 12, 120, 45, 4.5, ["חזה פתוח"], [("neutral_pull", "פולי ניטרלי", 45)], muscle="גב"),
            exercise("cable_row", "חתירה בכבל", 3, 10, 12, 120, 40, 5, ["גב ארוך"], [("row_machine", "מכונת חתירה", 40)], muscle="גב"),
            exercise("lateral", "הרחקת כתפיים", 2, 12, 15, 75, 7.5, 1, ["ללא כאב כתף"], [("cable_lateral", "הרחקה בכבל", 5)], muscle="כתפיים"),
        ],
    },
    "L1": {
        "name": "Lower 1 — רגליים קדמי",
        "exercises": [
            exercise("leg_press", "Leg Press", 3, 10, 12, 150, 90, 10, ["ברכיים בכיוון האצבעות"], [("goblet", "Goblet Squat", 22.5)], muscle="רגליים"),
            exercise("rdl", "דדליפט רומני", 3, 8, 10, 150, 35, 2.5, ["גב ניטרלי"], [("hip_thrust", "Hip Thrust", 55)], muscle="שרשרת אחורית"),
            exercise("glute_bridge", "גשר ישבן", 3, 10, 15, 90, 0, 0, ["שליטה", "ללא כאב גב"], [("hip_thrust", "Hip Thrust", 50)], muscle="ישבן"),
            exercise("dead_bug", "Dead Bug", 2, 8, 12, 60, 0, 0, ["ליבה יציבה"], [], muscle="ליבה"),
        ],
    },
    "U2": {
        "name": "Upper 2 — כתפיים וזרועות",
        "exercises": [
            exercise("incline_db", "לחיצה בשיפוע עם משקולות", 3, 8, 12, 120, 17.5, 2, ["טווח נוח"], [("incline_machine", "לחיצה בשיפוע במכונה", 40)], muscle="חזה עליון"),
            exercise("one_arm_row", "חתירה יד אחת", 3, 10, 12, 120, 20, 2.5, ["גב ניטרלי"], [("cable_row", "חתירה בכבל", 40)], muscle="גב"),
            exercise("shoulder_machine", "לחיצת כתפיים במכונה", 3, 8, 12, 120, 30, 2.5, ["ללא כאב כתף"], [("landmine", "Landmine Press", 15)], muscle="כתפיים"),
            exercise("bar_curl", "כפיפת מרפק במוט", 2, 10, 12, 75, 17.5, 2.5, ["מרפקים שקטים", "עצור אם יש כאב מרפק"], [("cable_curl", "כפיפה בכבל", 20)], muscle="יד קדמית"),
        ],
    },
    "L2": {
        "name": "Lower 2 — שרשרת אחורית",
        "exercises": [
            exercise("hip_thrust", "Hip Thrust", 3, 8, 12, 120, 55, 5, ["כיווץ ישבן"], [("glute_bridge", "גשר ישבן", 0)], muscle="ישבן"),
            exercise("goblet", "Goblet Squat", 3, 10, 12, 120, 22.5, 2.5, ["טווח ללא כאב"], [("chair_squat", "קימה מכיסא", 0)], muscle="רגליים"),
            exercise("back_ext", "היפר-אקסטנשן", 2, 10, 12, 90, 0, 0, ["תנועה מהאגן"], [("cable_pull_through", "Pull-Through בכבל", 25)], muscle="שרשרת אחורית"),
            exercise("dead_bug", "Dead Bug", 2, 8, 12, 60, 0, 0, ["ליבה יציבה"], [], muscle="ליבה"),
        ],
    },
})


# PLANS above is a read-only template. Per-user parameter edits are stored in
# the exercise_overrides table and applied on top, so they survive restarts and
# never leak across users.
OVERRIDE_FIELDS = {"weight", "sets", "rmin", "rmax", "rest"}


SPLIT_BY_FREQUENCY: dict[int, list[str]] = {
    1: ["F"],  # single full body session
    2: ["F", "F"],  # full body x2
    3: ["A", "B", "C"],  # chest / back / shoulders+legs
    4: ["A", "B", "C", "F"],
    5: ["A", "B", "C", "F", "F"],
    6: ["A", "B", "C", "A", "B", "C"],  # A/B/C twice
}
MIN_FREQUENCY = 1
MAX_FREQUENCY = 6

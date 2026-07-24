# -*- coding: utf-8 -*-
"""Verify a real Apple Health export end-to-end against a THROWAWAY database.

Runs the project's own import pipeline (extract → parse → upsert → facts →
routine profile) on a local ZIP/XML and prints a compact quality summary —
wear coverage, valid/burned weeks, filtered steps average and the RE13
health-quality report. The production database is never touched.

Usage (from the project root):
    python scripts/verify_health_import.py            # uses ./HealthKit.zip
    python scripts/verify_health_import.py path/to/export.zip

Only aggregate numbers are printed — no individual records.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TZ = ZoneInfo("Asia/Jerusalem")
USER_ID = 1


async def run(zip_path: Path, window_days: int) -> None:
    import health_import
    import health_service
    import routine
    import user_model
    from db import Database
    from helpers import utc_now
    from noam_coach.services.health_quality import build_health_quality_report

    if not zip_path.exists():
        raise SystemExit(f"file not found: {zip_path}")

    tmp = Path(tempfile.mkdtemp(prefix="verify_health_"))
    try:
        if zip_path.suffix.casefold() == ".zip":
            xml_path = health_import.extract_zip(zip_path, tmp / "extract")
            if xml_path is None:
                raise SystemExit("no export.xml found inside the ZIP")
        else:
            xml_path = zip_path
        print(f"[extract] export.xml: {xml_path.stat().st_size / (1024 * 1024):.1f} MB")

        rows, summary = health_import.summarize(
            health_import.iter_health_rows(xml_path, local_tz=TZ)
        )
        wear_rows = [r for r in rows if r.sample_type == "watch_wear"]
        print(f"[parse] rows={summary.rows:,} range={summary.min_date}..{summary.max_date}")
        print(
            f"[parse] workouts={summary.workouts} sleep_nights={summary.sleep_sessions} "
            f"watch_wear_days={len(wear_rows)}"
        )

        db = Database(str(tmp / "verify.db"))
        await db.init()
        await db.execute(
            "INSERT INTO users(id, first_name, username, updated_at) VALUES(?,?,NULL,?)",
            (USER_ID, "Verify", utc_now()),
        )
        health_service.DB = db
        inserted, duplicates = await health_service.upsert_health_rows(USER_ID, rows)
        await health_service.sync_health_measurements_to_facts(USER_ID)
        profile = await health_service.save_routine_profile(USER_ID)
        print(f"[db] inserted={inserted:,} duplicates={duplicates:,} (temp db, auto-deleted)")

        # --- wear coverage ------------------------------------------------
        wear_map = await routine.load_wear_days(db, USER_ID, TZ, window_days)
        full = partial = 0
        if wear_map:
            for wear in wear_map.values():
                if wear.covers_until_evening:
                    full += 1
                else:
                    partial += 1
        print(
            f"\n[wear] window={window_days}d worn_days={len(wear_map or {})} "
            f"full_wear_days={full} partial_wear_days={partial}"
        )

        # --- training weeks -------------------------------------------------
        analysis = await routine.analyze_training_weeks(db, USER_ID, TZ, window_days)
        if analysis is None:
            print("[weeks] no wear evidence — legacy fallback (no filtering)")
        else:
            print(
                f"[weeks] complete={len(analysis.weeks)} strict={analysis.valid_weeks_strict} "
                f"relaxed={analysis.valid_weeks_relaxed} burned={analysis.burned_weeks} "
                f"policy={analysis.policy}"
            )
            print(
                f"[weeks] frequency_raw={analysis.frequency_raw} "
                f"frequency_normalized={analysis.frequency_normalized} "
                f"workouts_on_unworn_days={analysis.workouts_on_unworn_days}"
            )
            for week in analysis.weeks:
                tag = (
                    "strict" if week.strict_valid
                    else "relaxed" if week.relaxed_valid
                    else "burned"
                )
                print(
                    f"[weeks]   {week.start}..{week.end}: worn={week.worn_days}/7 "
                    f"workouts={week.workouts_total} ({tag})"
                )

        workout = profile.get("workout") or {}
        print(
            f"[routine] weekly_frequency={workout.get('weekly_frequency')} "
            f"days={workout.get('common_weekdays')} hour={workout.get('typical_hour')}"
        )

        # --- steps ----------------------------------------------------------
        steps = await routine.average_daily_steps(db, USER_ID, TZ, window_days)
        raw_rows = await db.fetch_all(
            "SELECT AVG(value) AS v FROM health WHERE user_id=? AND sample_type='steps' "
            "AND start_time>=?",
            (
                USER_ID,
                (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)).isoformat(),
            ),
        )
        raw_avg = raw_rows[0]["v"] if raw_rows and raw_rows[0]["v"] is not None else None
        print(
            f"\n[steps] filtered_avg={steps.avg and round(steps.avg)} "
            f"days_used={steps.days_sampled} days_excluded={steps.days_excluded} "
            f"unfiltered_avg={raw_avg and round(raw_avg)}"
        )

        # --- latest measurements (values only, no history) -------------------
        for key in ("weight_kg", "body_fat_pct", "resting_hr"):
            value = await user_model.get_value(db, USER_ID, key)
            print(f"[latest] {key}={value}")
        sleep = profile.get("sleep") or {}
        from noam_coach.services import coaching_day

        print(
            f"[sleep] {coaching_day.sleep_bedtime(sleep)}-"
            f"{coaching_day.sleep_wake_time(sleep)} "
            f"nights={sleep.get('nights_sampled')}"
        )

        # --- RE13 quality report ---------------------------------------------
        report = await build_health_quality_report(db, USER_ID, TZ, window_days)
        print(f"\n[quality] overall={report['overall_confidence']}")
        wq = report["workout_frequency"]
        print(
            f"[quality] workouts: confidence={wq['confidence']} policy={wq['policy']} "
            f"frequency={wq['frequency']} valid={wq['valid_weeks']} burned={wq['burned_weeks']}"
        )
        sq = report["steps"]
        print(
            f"[quality] steps: confidence={sq['confidence']} used={sq['days_used']} "
            f"excluded={sq['days_excluded']}"
        )
        sl = report["sleep"]
        print(
            f"[quality] sleep: confidence={sl['confidence']} nights={sl['nights_used']} "
            f"confirmable={sl['should_ask_confirmation']}"
        )
        fr = report["freshness"]
        print(
            f"[quality] freshness: latest={fr['latest_sample_date']} "
            f"days_old={fr['days_old']} stale={fr['is_stale']}"
        )
        for section in ("workout_frequency", "steps", "sleep", "freshness"):
            warning = report[section].get("warning_he")
            if warning:
                print(f"[quality] ⚠ {section}: {warning}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Verify an Apple Health export")
    parser.add_argument(
        "path", nargs="?", default=str(PROJECT_ROOT / "HealthKit.zip"),
        help="ZIP or export.xml path (default: ./HealthKit.zip)",
    )
    parser.add_argument("--days", type=int, default=45, help="analysis window in days")
    args = parser.parse_args()
    asyncio.run(run(Path(args.path), args.days))


if __name__ == "__main__":
    main()

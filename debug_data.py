"""
debug_data.py — Trace exactly what data each Claude prompt would receive.

Usage:
    python debug_data.py              # Use cached Garmin data (no live fetch)
    python debug_data.py --fetch      # Force a live Garmin fetch first
    python debug_data.py --prompt daily_update   # Show one specific prompt's full input

Prints a section-by-section report of what's populated vs missing,
then shows the assembled user_prompt for each function.
"""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# ── Setup ──────────────────────────────────────────────────────────────────────

def _check_env():
    missing = [k for k in ("ANTHROPIC_API_KEY", "GARMIN_EMAIL", "GARMIN_PASSWORD",
                           "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
               if not os.getenv(k)]
    if missing:
        print(f"[warn] Missing env vars: {', '.join(missing)} — some fetches may fail\n")

# ── Data section checks ────────────────────────────────────────────────────────

def _check(label: str, value, non_empty_check=True):
    if value is None:
        status = "✗ None"
    elif isinstance(value, str) and (not value.strip() or value in ("No daily logs yet.", "None yet.", "Not available.")):
        status = "✗ empty/placeholder"
    elif isinstance(value, list) and len(value) == 0:
        status = "✗ empty list"
    elif isinstance(value, dict) and "error" in value:
        status = f"✗ error: {value['error']}"
    elif non_empty_check:
        if isinstance(value, str):
            status = f"✓ {len(value)} chars"
        elif isinstance(value, list):
            status = f"✓ {len(value)} entries"
        elif isinstance(value, dict):
            status = f"✓ keys: {list(value.keys())}"
        else:
            status = f"✓ {value}"
    else:
        status = "✓"
    print(f"  {'✓' if status.startswith('✓') else '✗'} {label}: {status}")
    return status.startswith("✓")


def audit_garmin_data(data: dict):
    print("\n" + "="*60)
    print("GARMIN DATA AUDIT")
    print("="*60)

    # Top-level keys
    top_keys = list(data.keys())
    print(f"\nTop-level keys present: {top_keys}\n")

    _check("sleep", data.get("sleep"))
    sleep = data.get("sleep", [])
    if isinstance(sleep, list) and sleep:
        sample = sleep[0]
        print(f"    Sample night: date={sample.get('date')}, total={sample.get('total_seconds')}s, "
              f"deep={sample.get('deep_seconds')}s, REM={sample.get('rem_seconds')}s, "
              f"score={sample.get('score')}, resp={sample.get('avg_respiration')}, "
              f"spo2={sample.get('avg_spo2')}")

    _check("hrv", data.get("hrv"))
    hrv = data.get("hrv", [])
    if isinstance(hrv, list) and hrv:
        sample = hrv[0]
        print(f"    Latest: date={sample.get('date')}, hrv={sample.get('hrv')}ms, "
              f"weekly_avg={sample.get('weekly_avg')}, status={sample.get('status')}")

    _check("resting_hr", data.get("resting_hr"))
    rhr = data.get("resting_hr", [])
    if isinstance(rhr, list) and rhr:
        print(f"    Latest: date={rhr[0].get('date')}, rhr={rhr[0].get('rhr')}bpm")

    _check("body_battery", data.get("body_battery"))
    bb = data.get("body_battery", [])
    if isinstance(bb, list) and bb:
        print(f"    Today: date={bb[0].get('date')}, start={bb[0].get('start')}, end={bb[0].get('end')}")

    _check("stress", data.get("stress"))
    st = data.get("stress", [])
    if isinstance(st, list) and st:
        print(f"    Today: date={st[0].get('date')}, avg={st[0].get('average')}, max={st[0].get('max')}, rest_pct={st[0].get('rest_pct')}")

    _check("training_readiness", data.get("training_readiness"))
    tr = data.get("training_readiness", [])
    if isinstance(tr, list) and tr:
        print(f"    Latest: date={tr[0].get('date')}, score={tr[0].get('score')}, level={tr[0].get('level')}")

    _check("vo2max", data.get("vo2max"))
    print(f"    Value: {data.get('vo2max')}")

    _check("lactate_threshold", data.get("lactate_threshold"))
    lt = data.get("lactate_threshold")
    if lt:
        print(f"    LT HR={lt.get('hr')}, pace_spm={lt.get('pace_spm')}")

    _check("training_status", data.get("training_status"))
    ts = data.get("training_status")
    if ts:
        print(f"    status={ts.get('status')}, recovery_hours={ts.get('recovery_hours')}")
        lf = ts.get("load_focus")
        if lf:
            print(f"    load_focus={lf}")

    _check("training_load", data.get("training_load"))
    tl = data.get("training_load")
    if tl:
        print(f"    acute={tl.get('acute')}, chronic={tl.get('chronic')}, acwr={tl.get('acwr')}")

    _check("activities", data.get("activities"))
    acts = data.get("activities", [])
    if isinstance(acts, list):
        for a in acts[:3]:
            laps = a.get("laps", [])
            weather = a.get("weather")
            print(f"    [{a.get('date')}] {a.get('name')} — dist={a.get('distance_meters')}m "
                  f"hr={a.get('avg_hr')} TE={a.get('aerobic_te')}/{a.get('anaerobic_te')} "
                  f"laps={len(laps)} weather={'✓' if weather else '✗'}")
            if laps:
                l = laps[0]
                print(f"      Lap1: hr={l.get('avg_hr')} gct={l.get('avg_ground_contact_ms')} "
                      f"stride={l.get('avg_stride_length_cm')} vo={l.get('avg_vertical_oscillation_mm')}")

    _check("steps", data.get("steps"))


def audit_formatted(garmin_formatted: str):
    print("\n" + "="*60)
    print("FORMATTED OUTPUT AUDIT")
    print("="*60)

    checks = [
        ("Sleep trend", "Sleep trend:"),
        ("Respiration data", "resp="),
        ("SpO2 data", "SpO2="),
        ("Respiration flag", "⚠ Elevated respiration"),
        ("HRV trend", "HRV:"),
        ("Resting HR", "Resting HR:"),
        ("Body battery", "Body Battery"),
        ("Body battery trend", "Body battery 7-day trend"),
        ("Stress", "Stress (today)"),
        ("Training readiness", "Training Readiness"),
        ("VO2 max", "VO2 Max"),
        ("Lactate threshold", "Lactate Threshold"),
        ("Training status", "Training Status"),
        ("Training load / ACWR", "Training Load"),
        ("Load focus", "Load Focus"),
        ("Steps", "Steps:"),
        ("Activity entries", "Recent activities"),
        ("Load distribution", "Load distribution"),
        ("Activity pattern", "Activity pattern"),
        ("Aerobic TE", "aerobic TE="),
        ("Anaerobic TE", "anaerobic TE="),
        ("Running dynamics", "Running dynamics"),
        ("GCT in laps", "GCT="),
        ("Stride length in laps", "stride="),
        ("HR zones", "HR zones"),
        ("Weather", "weather:"),
    ]

    found = sum(1 for _, marker in checks if marker in garmin_formatted)
    print(f"\n{found}/{len(checks)} expected sections present:\n")
    for label, marker in checks:
        present = marker in garmin_formatted
        print(f"  {'✓' if present else '✗'} {label}")

    print(f"\nTotal formatted length: {len(garmin_formatted)} chars")


def audit_supporting_data():
    print("\n" + "="*60)
    print("SUPPORTING FILES AUDIT")
    print("="*60)

    import notes
    import daily_log

    config = notes.load_config()

    plan = notes.read_plan()
    _check("training_plan.md", plan)

    coach_notes = notes.read_notes()
    _check("coach_notes.md", coach_notes)

    profile = notes.read_profile()
    _check("athlete_profile.md", profile)

    reflection = notes.read_weekly_reflection()
    _check("weekly_reflection.md", reflection)

    recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))
    _check("recent_logs (30d)", recent_logs)

    # Check today's log
    today_path = daily_log._log_path(date.today())
    print(f"\n  Today's log ({today_path}): {'exists' if today_path.exists() else 'NOT YET CREATED'}")
    if today_path.exists():
        text = today_path.read_text()
        has_feedback = "### Athlete Feedback" in text or "[FEEDBACK]" in text
        has_analysis = "## Coach Analysis" in text
        print(f"    Coach analysis: {'✓' if has_analysis else '✗'}")
        print(f"    Athlete feedback: {'✓' if has_feedback else '✗ (none logged yet today)'}")


def show_prompt(prompt_name: str, garmin_formatted: str):
    """Build and print the full user_prompt for a specific function."""
    import notes
    import daily_log
    import claude

    config = notes.load_config()
    plan = notes.read_plan()
    coach_notes = notes.read_notes()
    profile = notes.read_profile()
    reflection = notes.read_weekly_reflection()
    recent_logs = daily_log.read_recent_logs(n_days=config["claude"].get("daily_logs_days", 30))

    print(f"\n{'='*60}")
    print(f"FULL PROMPT PREVIEW: {prompt_name}")
    print(f"{'='*60}\n")

    # Monkey-patch _call_claude to capture the prompt without calling the API
    captured = {}
    original_call = claude._call_claude

    def capture(label, system_prompt, user_prompt, *args, **kwargs):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return "<captured — no API call made>"

    claude._call_claude = capture

    try:
        if prompt_name == "daily_update":
            claude.daily_update(garmin_formatted, plan, coach_notes, profile,
                                recent_logs=recent_logs, weekly_reflection=reflection)
        elif prompt_name == "evening_checkin":
            claude.evening_checkin(garmin_formatted, plan, coach_notes, profile,
                                   recent_logs=recent_logs, weekly_reflection=reflection)
        elif prompt_name == "today_readiness_check":
            claude.today_readiness_check(plan, coach_notes, profile,
                                         garmin_formatted=garmin_formatted,
                                         recent_logs=recent_logs, weekly_reflection=reflection)
        elif prompt_name == "post_workout_checkin":
            sample_summary = "Morning Run: 3.0 mi | time 28:00 | avg pace 9:20/mi | avg HR 152 | aerobic TE 2.4"
            claude.post_workout_checkin(sample_summary, coach_notes, profile,
                                        garmin_formatted=garmin_formatted,
                                        training_plan=plan, recent_logs=recent_logs,
                                        weekly_reflection=reflection)
        elif prompt_name == "respond_to_user":
            claude.respond_to_user("How am I doing this week?", plan, coach_notes, profile,
                                   garmin_formatted=garmin_formatted,
                                   weekly_reflection=reflection, recent_logs=recent_logs)
        elif prompt_name == "weekly_reflection":
            claude.generate_weekly_reflection(garmin_formatted, plan, coach_notes, profile,
                                              recent_logs=recent_logs, week_label="Week of Jun 2–8, 2026",
                                              weekly_reflection=reflection)
        else:
            print(f"Unknown prompt: {prompt_name}")
            return
    finally:
        claude._call_claude = original_call

    if "system" in captured:
        sep = "-" * 50
        print(f"[SYSTEM PROMPT] ({len(captured['system'])} chars)\n{sep}")
        print(captured["system"])
        print(f"\n[USER PROMPT] ({len(captured['user'])} chars)\n{sep}")
        print(captured["user"])
    else:
        print("Failed to capture prompt.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv
    load_dotenv(override=True)
    _check_env()

    parser = argparse.ArgumentParser(description="Debug relay data pipeline")
    parser.add_argument("--fetch", action="store_true", help="Force fresh Garmin fetch (requires credentials)")
    parser.add_argument("--prompt", metavar="NAME",
                        help="Show full assembled prompt for: daily_update, evening_checkin, "
                             "today_readiness_check, post_workout_checkin, respond_to_user, weekly_reflection")
    args = parser.parse_args()

    import garmin

    if args.fetch:
        print("Fetching fresh Garmin data...")
        garmin.invalidate_cache()

    print("Loading Garmin data (from cache if available)...")
    try:
        data = garmin.fetch_garmin_data()
    except Exception as e:
        print(f"[error] Garmin fetch failed: {e}")
        print("Tip: run without --fetch to use cached data, or check GARMIN_EMAIL/GARMIN_PASSWORD")
        sys.exit(1)

    audit_garmin_data(data)

    import notes as notes_module
    config = notes_module.load_config()
    garmin_formatted = garmin.format_garmin_data(data, units=config["coaching"]["units"])

    audit_formatted(garmin_formatted)
    audit_supporting_data()

    if args.prompt:
        show_prompt(args.prompt, garmin_formatted)
    else:
        print("\n" + "="*60)
        print("TIP: Run with --prompt <name> to see full assembled prompt")
        print("  e.g. python debug_data.py --prompt daily_update")
        print("       python debug_data.py --prompt evening_checkin")
        print("="*60)


if __name__ == "__main__":
    main()

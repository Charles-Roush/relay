import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from garminconnect import Garmin

import weather as weather_module

load_dotenv(override=True)


def load_config() -> dict:
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def _cache_dir() -> Path:
    config = load_config()
    return Path(config.get("paths", {}).get("logs_dir", "logs"))


def _cache_path(d: date) -> Path:
    return _cache_dir() / f"garmin_cache_{d.isoformat()}.json"


def _load_cache(d: date) -> dict | None:
    path = _cache_path(d)
    if path.exists():
        return json.loads(path.read_text())
    return None


def _save_cache(d: date, data: dict):
    _cache_dir().mkdir(exist_ok=True)
    _cache_path(d).write_text(json.dumps(data))


def get_local_date(timezone_str: str) -> date:
    return datetime.now(ZoneInfo(timezone_str)).date()


def invalidate_cache():
    """Delete today's Garmin cache so the next fetch pulls fresh data."""
    config = load_config()
    tz_str = config["schedule"]["timezone"]
    today = get_local_date(tz_str)
    path = _cache_path(today)
    if path.exists():
        path.unlink()


def get_latest_activity(data: dict) -> dict | None:
    """Return the most recent activity entry, or None."""
    activities = data.get("activities")
    if not isinstance(activities, list) or not activities:
        return None
    return activities[0]


def fetch_garmin_data() -> dict:
    config = load_config()
    tz_str = config["schedule"]["timezone"]
    today = get_local_date(tz_str)

    cached = _load_cache(today)
    if cached is not None:
        return cached

    g_cfg = config["garmin"]
    metrics = g_cfg["metrics"]
    lookback_days = g_cfg["lookback_days"]
    # Fetch 2x lookback to ensure full coverage even with multiple activities per day
    activities_fetch_count = lookback_days * 2

    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    client.login()

    data = {}

    if metrics.get("sleep"):
        try:
            sleep_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i + 1)).isoformat()
                try:
                    raw = client.get_sleep_data(d)
                    ds = raw.get("dailySleepDTO", {})
                    total_sec = ds.get("sleepTimeSeconds")
                    if not total_sec:
                        continue
                    sleep_history.append({
                        "date": d,
                        "total_seconds": total_sec,
                        "deep_seconds": ds.get("deepSleepSeconds"),
                        "rem_seconds": ds.get("remSleepSeconds"),
                        "light_seconds": ds.get("lightSleepSeconds"),
                        "awake_seconds": ds.get("awakeSleepSeconds"),
                        "score": ds.get("sleepScores", {}).get("overall", {}).get("value"),
                        "avg_respiration": ds.get("averageRespirationValue"),
                        "avg_spo2": ds.get("averageSpO2Value"),
                    })
                except Exception:
                    pass
            data["sleep"] = sleep_history
        except Exception as e:
            data["sleep"] = {"error": str(e)}

    if metrics.get("hrv"):
        try:
            hrv_values = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i + 1)).isoformat()
                try:
                    raw = client.get_hrv_data(d)
                    summary = raw.get("hrvSummary", {})
                    val = summary.get("lastNight")
                    status = summary.get("hrvStatus")
                    weekly_avg = summary.get("weeklyAvg")
                    if val:
                        hrv_values.append({
                            "date": d,
                            "hrv": val,
                            "status": status,
                            "weekly_avg": weekly_avg,
                        })
                except Exception:
                    pass
            data["hrv"] = hrv_values
        except Exception as e:
            data["hrv"] = {"error": str(e)}

    if metrics.get("resting_hr"):
        try:
            rhr_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_rhr_day(d)
                    # garminconnect returns {"value": {"calendarDate": ..., "value": X}} or list
                    val = None
                    if isinstance(raw, dict):
                        val = (raw.get("value") or {}).get("value") or raw.get("restingHeartRate")
                    elif isinstance(raw, list) and raw:
                        val = raw[0].get("value") or raw[0].get("restingHeartRate")
                    if val:
                        rhr_history.append({"date": d, "rhr": val})
                except Exception:
                    pass
            data["resting_hr"] = rhr_history
        except Exception as e:
            data["resting_hr"] = {"error": str(e)}

    if metrics.get("body_battery"):
        try:
            bb_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_body_battery(d)
                    if raw and isinstance(raw, list):
                        start_val = raw[0][1]
                        end_val = raw[-1][1]
                        if start_val is not None:
                            bb_history.append({"date": d, "start": start_val, "end": end_val})
                except Exception:
                    pass
            data["body_battery"] = bb_history
        except Exception as e:
            data["body_battery"] = {"error": str(e)}

    if metrics.get("steps"):
        try:
            steps_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_steps_data(d)
                    total = sum(s.get("steps", 0) for s in raw) if isinstance(raw, list) else 0
                    steps_history.append({"date": d, "steps": total})
                except Exception:
                    pass
            data["steps"] = steps_history
        except Exception as e:
            data["steps"] = {"error": str(e)}

    if metrics.get("stress"):
        try:
            stress_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_stress_data(d)
                    avg = raw.get("overallStressLevel") if isinstance(raw, dict) else None
                    max_s = raw.get("maxStressLevel") if isinstance(raw, dict) else None
                    rest_stress = raw.get("restStressPercentage") if isinstance(raw, dict) else None
                    if avg is not None and avg >= 0:
                        stress_history.append({
                            "date": d,
                            "average": avg,
                            "max": max_s,
                            "rest_pct": rest_stress,
                        })
                except Exception:
                    pass
            data["stress"] = stress_history
        except Exception as e:
            data["stress"] = {"error": str(e)}

    if metrics.get("training_readiness"):
        try:
            tr_history = []
            for i in range(lookback_days):
                d = (today - timedelta(days=i)).isoformat()
                try:
                    raw = client.get_training_readiness(d)
                    items = raw if isinstance(raw, list) else [raw]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        score = item.get("score") or item.get("trainingReadinessScore")
                        level = item.get("level") or item.get("trainingReadinessLevel")
                        feedback = item.get("feedback") or item.get("feedbackLongPhrase")
                        if score is not None:
                            tr_history.append({"date": d, "score": score, "level": level, "feedback": feedback})
                            break
                except Exception:
                    pass
            data["training_readiness"] = tr_history
        except Exception as e:
            data["training_readiness"] = {"error": str(e)}

    if metrics.get("vo2max"):
        try:
            raw = client.get_max_metrics(today.isoformat())
            vo2 = None
            if isinstance(raw, list) and raw:
                item = raw[0]
                generic = item.get("generic") or {}
                vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue") or item.get("vo2MaxValue")
            elif isinstance(raw, dict):
                generic = raw.get("generic") or {}
                vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue") or raw.get("vo2MaxValue")
            data["vo2max"] = vo2
        except Exception:
            data["vo2max"] = None

    if metrics.get("lactate_threshold"):
        try:
            raw = client.get_lactate_threshold()
            # API returns {"statisticsStartDate": ..., "lactateThresholdHeartRateUsed": bool,
            #              "heartRateThreshold": X, "ltPaceSecondsPerMeter": X, ...}
            items = raw if isinstance(raw, list) else [raw]
            lt = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                hr = item.get("heartRateThreshold") or item.get("lactateThresholdHeartRate")
                pace_spm = item.get("ltPaceSecondsPerMeter")  # seconds per meter
                if hr or pace_spm:
                    lt = {"hr": hr, "pace_spm": pace_spm}
                    break
            data["lactate_threshold"] = lt
        except Exception:
            data["lactate_threshold"] = None

    if metrics.get("training_status"):
        # training_status covers: Training Status label, Recovery Time, Load Focus
        try:
            raw = client.get_training_status(today.isoformat())
            items = raw if isinstance(raw, list) else [raw]
            ts = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Training Status
                status = (
                    item.get("trainingStatus")
                    or item.get("trainingStatusLoad", {}).get("trainingStatus")
                )
                # Recovery Time (hours until recovered)
                recovery_hrs = (
                    item.get("recoveryTime")
                    or item.get("mostRecentRecoveryTime")
                )
                # Load Focus — balance across base/tempo/threshold/anaerobic buckets
                load_focus = item.get("trainingLoadBalance") or item.get("loadFocusBalance")
                if status or recovery_hrs is not None:
                    ts = {
                        "status": status,
                        "recovery_hours": recovery_hrs,
                        "load_focus": load_focus,
                    }
                    break
            data["training_status"] = ts
        except Exception:
            data["training_status"] = None

    if metrics.get("training_load"):
        # Acute load, chronic load, and ACWR (acute:chronic workload ratio)
        try:
            raw = client.get_training_load(today.isoformat())
            items = raw if isinstance(raw, list) else [raw]
            tl = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                acute = (
                    item.get("acuteLoad")
                    or item.get("shortTermLoadValue")
                    or item.get("sevenDayLoad")
                )
                chronic = (
                    item.get("chronicLoad")
                    or item.get("longTermLoadValue")
                    or item.get("twentyEightDayLoad")
                )
                # ACWR may come directly or we compute it
                acwr = item.get("acuteChronicWorkloadRatio")
                if acwr is None and acute and chronic and chronic > 0:
                    acwr = round(acute / chronic, 2)
                if acute is not None or chronic is not None:
                    tl = {
                        "acute": acute,
                        "chronic": chronic,
                        "acwr": acwr,
                    }
                    break
            data["training_load"] = tl
        except Exception:
            data["training_load"] = None

    if metrics.get("activities"):
        try:
            raw = client.get_activities(0, activities_fetch_count)
            activities = []
            cutoff = today - timedelta(days=lookback_days)
            lap_fetch_cutoff = today - timedelta(days=2)  # full lap detail only for last 2 days

            for a in raw:
                activity_date_str = a.get("startTimeLocal", "")[:10]
                activity_id = a.get("activityId")

                # Stop once we're past the lookback window (list is newest-first)
                try:
                    if date.fromisoformat(activity_date_str) < cutoff:
                        break
                except ValueError:
                    pass

                avg_speed = a.get("averageSpeed")  # meters/second

                # Running dynamics (may be None for non-running or older devices)
                gct_ms = a.get("avgGroundContactTime")          # milliseconds
                vert_osc_mm = a.get("avgVerticalOscillation")   # millimeters
                stride_cm = a.get("avgStrideLength")             # centimeters
                vert_ratio = a.get("avgVerticalRatio")           # percent

                # Location + UTC start time (used for weather fetch)
                start_lat = a.get("startLatitude") or a.get("beginningLatitude")
                start_lon = a.get("startLongitude") or a.get("beginningLongitude")
                start_utc = a.get("startTimeGMT") or a.get("beginningTimestamp")

                entry = {
                    "type": a.get("activityType", {}).get("typeKey"),
                    "name": a.get("activityName"),
                    "date": activity_date_str,
                    "time": a.get("startTimeLocal", "")[11:16],
                    "activity_id": activity_id,
                    "duration_seconds": a.get("duration"),
                    "moving_duration": a.get("movingDuration"),
                    "distance_meters": a.get("distance"),
                    "avg_hr": a.get("averageHR"),
                    "max_hr": a.get("maxHR"),
                    "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
                    "elevation_gain": a.get("elevationGain"),
                    "tss": a.get("trainingStressScore"),
                    "aerobic_te": a.get("aerobicTrainingEffect"),
                    "anaerobic_te": a.get("anaerobicTrainingEffect"),
                    "avg_speed_ms": avg_speed,
                    "avg_ground_contact_ms": gct_ms,
                    "avg_vertical_oscillation_mm": vert_osc_mm,
                    "avg_stride_length_cm": stride_cm,
                    "avg_vertical_ratio": vert_ratio,
                    "start_lat": start_lat,
                    "start_lon": start_lon,
                    "start_utc": start_utc,
                    "weather": None,
                    "laps": [],
                }

                # Fetch weather for recent activities if location data available
                weather_enabled = g_cfg.get("weather", {}).get("enabled", True)
                try:
                    activity_date = date.fromisoformat(activity_date_str)
                    if (
                        weather_enabled
                        and start_lat is not None
                        and start_lon is not None
                        and start_utc
                        and activity_date >= lap_fetch_cutoff
                    ):
                        entry["weather"] = weather_module.get_weather_for_activity(
                            start_lat, start_lon, start_utc,
                            units=g_cfg.get("units", "imperial"),
                        )
                except Exception:
                    pass

                # Full lap detail for recent activities
                try:
                    activity_date = date.fromisoformat(activity_date_str)
                    if activity_id and activity_date >= lap_fetch_cutoff:
                        laps_raw = client.get_activity_splits(activity_id)
                        lap_list = laps_raw.get("lapDTOs") or laps_raw.get("laps", [])
                        laps = []
                        for idx, lap in enumerate(lap_list):
                            laps.append({
                                "lap": idx + 1,
                                "distance_meters": lap.get("distance"),
                                "duration_seconds": lap.get("duration"),
                                "avg_hr": lap.get("averageHR"),
                                "max_hr": lap.get("maxHR"),
                                "avg_speed_ms": lap.get("averageSpeed"),
                                "avg_cadence": lap.get("averageRunningCadenceInStepsPerMinute"),
                                "elevation_gain": lap.get("elevationGain"),
                                "avg_ground_contact_ms": lap.get("avgGroundContactTime"),
                                "avg_vertical_oscillation_mm": lap.get("avgVerticalOscillation"),
                                "avg_stride_length_cm": lap.get("avgStrideLength"),
                            })
                        entry["laps"] = laps
                except Exception:
                    pass  # laps are best-effort

                activities.append(entry)
            data["activities"] = activities
        except Exception as e:
            data["activities"] = {"error": str(e)}

    _save_cache(today, data)
    return data


# ── Trend helpers ──────────────────────────────────────────────────────────────

def _hrv_trend_summary(hrv_values: list[dict]) -> str:
    """Rolling HRV avg, deviation from baseline, and direction."""
    vals = [h["hrv"] for h in hrv_values]
    if not vals:
        return "no data"
    avg = sum(vals) / len(vals)
    latest = vals[0]  # most recent first
    delta = latest - avg
    if delta > 5:
        trend = "↑ above baseline"
    elif delta < -5:
        trend = "↓ below baseline (recovery flag)"
    else:
        trend = "→ near baseline"
    status = hrv_values[0].get("status", "")
    status_str = f", Garmin status: {status}" if status else ""
    weekly_avg = hrv_values[0].get("weekly_avg")
    weekly_str = f", Garmin 7-day avg: {weekly_avg}ms" if weekly_avg else ""
    return (
        f"Latest: {latest}ms | {len(vals)}-day avg: {avg:.0f}ms | "
        f"Deviation: {delta:+.0f}ms {trend}{status_str}{weekly_str}"
    )


def _sleep_trend_summary(sleep_history: list[dict]) -> str:
    """Rolling sleep averages."""
    totals = [s["total_seconds"] for s in sleep_history if s.get("total_seconds")]
    deeps = [s["deep_seconds"] for s in sleep_history if s.get("deep_seconds")]
    rems = [s["rem_seconds"] for s in sleep_history if s.get("rem_seconds")]
    scores = [s["score"] for s in sleep_history if s.get("score")]
    n = len(totals)
    if n == 0:
        return "no data"
    avg_total = sum(totals) / n / 3600
    avg_deep = sum(deeps) / len(deeps) / 3600 if deeps else None
    avg_rem = sum(rems) / len(rems) / 3600 if rems else None
    avg_score = sum(scores) / len(scores) if scores else None
    parts = [f"{n}-night avg: {avg_total:.1f}h total"]
    if avg_deep is not None:
        parts.append(f"deep={avg_deep:.1f}h")
    if avg_rem is not None:
        parts.append(f"REM={avg_rem:.1f}h")
    if avg_score is not None:
        parts.append(f"score={avg_score:.0f}")
    return " | ".join(parts)


def _classify_activity_load(aerobic_te: float | None) -> str:
    if aerobic_te is None:
        return "unknown"
    if aerobic_te < 2.0:
        return "easy"
    elif aerobic_te < 3.5:
        return "moderate"
    else:
        return "hard"


def _training_load_trend(activities: list[dict]) -> str:
    """14-day easy/moderate/hard distribution with total mileage."""
    config = load_config()
    tz_str = config["schedule"]["timezone"]
    units = config["coaching"]["units"]
    load_trend_days = config.get("claude", {}).get("load_trend_days", 14)
    today = get_local_date(tz_str)
    cutoff = today - timedelta(days=load_trend_days)

    easy = moderate = hard = unknown = 0
    total_meters = 0.0

    for a in activities:
        try:
            a_date = date.fromisoformat(a.get("date", ""))
        except ValueError:
            continue
        if a_date < cutoff:
            continue

        total_meters += a.get("distance_meters") or 0
        label = _classify_activity_load(a.get("aerobic_te"))
        if label == "easy":
            easy += 1
        elif label == "moderate":
            moderate += 1
        elif label == "hard":
            hard += 1
        else:
            unknown += 1

    total_runs = easy + moderate + hard + unknown
    if total_runs == 0:
        return f"No activity data for last {load_trend_days} days."

    dist_str = f"{total_meters / 1609.34:.1f} mi" if units == "imperial" else f"{total_meters / 1000:.1f} km"
    parts = [f"easy={easy}", f"moderate={moderate}", f"hard={hard}"]
    if unknown:
        parts.append(f"unclassified={unknown}")
    return f"Last {load_trend_days} days: {total_runs} runs, {dist_str} total | Load distribution (by aerobic TE): {', '.join(parts)}"


def _body_battery_trend(bb_list: list[dict]) -> str:
    """7-day net body battery trend from start-of-day values."""
    if len(bb_list) < 3:
        return ""
    recent = bb_list[:7]
    starts = [b["start"] for b in recent if b.get("start") is not None]
    ends = [b["end"] for b in recent if b.get("end") is not None]
    if not starts or not ends:
        return ""
    avg_start = sum(starts) / len(starts)
    avg_end = sum(ends) / len(ends)
    net = avg_end - avg_start
    if net > 5:
        direction = "↑ net positive (recovering well)"
    elif net < -5:
        direction = "↓ net negative (cumulative drain)"
    else:
        direction = "→ roughly neutral"
    return f"  Body battery 7-day trend: avg start={avg_start:.0f} → avg end={avg_end:.0f} | {direction}"


def _compute_hr_zones(laps: list[dict], lt_hr: int | float | None) -> str | None:
    """
    Compute time-in-zone percentages from lap data.
    Zones based on % of LT HR (lactate threshold heart rate).
    Returns a formatted string or None if insufficient data.
    """
    if not laps or not lt_hr:
        return None

    zone_seconds = {"Z1 (<80% LT)": 0, "Z2 (80-89%)": 0, "Z3 (90-99%)": 0,
                    "Z4 (100-110%)": 0, "Z5 (>110%)": 0}
    total_seconds = 0

    for lap in laps:
        avg_hr = lap.get("avg_hr")
        dur = lap.get("duration_seconds")
        if avg_hr is None or not dur:
            continue
        pct = avg_hr / lt_hr * 100
        total_seconds += dur
        if pct < 80:
            zone_seconds["Z1 (<80% LT)"] += dur
        elif pct < 90:
            zone_seconds["Z2 (80-89%)"] += dur
        elif pct < 100:
            zone_seconds["Z3 (90-99%)"] += dur
        elif pct <= 110:
            zone_seconds["Z4 (100-110%)"] += dur
        else:
            zone_seconds["Z5 (>110%)"] += dur

    if total_seconds == 0:
        return None

    parts = []
    for zone, secs in zone_seconds.items():
        if secs > 0:
            parts.append(f"{zone}: {secs / total_seconds * 100:.0f}%")
    return "HR zones (by LT%): " + " | ".join(parts) if parts else None


def _activity_pattern(activities: list[dict], lookback_days: int, tz_str: str) -> str:
    """
    Show which days in the lookback window had a recorded run vs. rest.
    Used as guideline adherence context (not a compliance grade).
    """
    today = get_local_date(tz_str)
    activity_dates = set()
    for a in activities:
        try:
            activity_dates.add(date.fromisoformat(a["date"]))
        except (ValueError, KeyError):
            pass

    pattern = []
    for i in range(lookback_days):
        d = today - timedelta(days=i)
        pattern.append("R" if d in activity_dates else "·")
    pattern_str = " ".join(pattern)
    run_count = pattern_str.count("R")
    return (
        f"Activity pattern (last {lookback_days} days, newest left): {pattern_str} "
        f"| {run_count}/{lookback_days} days with recorded activity"
    )


def _pace_str(avg_speed_ms: float | None, units: str) -> str:
    if not avg_speed_ms or avg_speed_ms <= 0:
        return "?"
    if units == "imperial":
        secs = 1609.34 / avg_speed_ms
        return f"{int(secs // 60)}:{int(secs % 60):02d}/mi"
    else:
        secs = 1000 / avg_speed_ms
        return f"{int(secs // 60)}:{int(secs % 60):02d}/km"


# ── Formatting ─────────────────────────────────────────────────────────────────

def format_garmin_data(data: dict, units: str = "imperial") -> str:
    config = load_config()
    tz_str = config["schedule"]["timezone"]
    g_cfg = config["garmin"]
    lookback_days = g_cfg["lookback_days"]

    lines = []

    # ── Sleep ──
    if "sleep" in data:
        s_data = data["sleep"]
        if isinstance(s_data, list) and s_data:
            lines.append(f"Sleep trend: {_sleep_trend_summary(s_data)}")
            # Surface any anomalous respiration or SpO2 nights as a summary flag
            resp_flags = [s for s in s_data if s.get("avg_respiration") and s["avg_respiration"] > 18]
            spo2_flags = [s for s in s_data if s.get("avg_spo2") and s["avg_spo2"] < 95]
            if resp_flags:
                flag_dates = ", ".join(s["date"] for s in resp_flags[:3])
                lines.append(f"  ⚠ Elevated respiration (>18 br/min): {flag_dates}")
            if spo2_flags:
                flag_dates = ", ".join(f"{s['date']} ({s['avg_spo2']:.1f}%)" for s in spo2_flags[:3])
                lines.append(f"  ⚠ Low SpO2 (<95%): {flag_dates}")
            for s in s_data:
                total_hrs = round(s["total_seconds"] / 3600, 1) if s.get("total_seconds") else "?"
                deep = round(s["deep_seconds"] / 3600, 1) if s.get("deep_seconds") else "?"
                rem = round(s["rem_seconds"] / 3600, 1) if s.get("rem_seconds") else "?"
                light = round(s["light_seconds"] / 3600, 1) if s.get("light_seconds") else "?"
                awake = round(s["awake_seconds"] / 3600, 1) if s.get("awake_seconds") else "?"
                extras = []
                if s.get("avg_respiration"):
                    flag = " ⚠" if s["avg_respiration"] > 18 else ""
                    extras.append(f"resp={s['avg_respiration']:.1f} br/min{flag}")
                if s.get("avg_spo2"):
                    flag = " ⚠" if s["avg_spo2"] < 95 else ""
                    extras.append(f"SpO2={s['avg_spo2']:.1f}%{flag}")
                extra_str = f" | {', '.join(extras)}" if extras else ""
                lines.append(
                    f"  Sleep ({s['date']}): {total_hrs}h total | score={s.get('score', '?')} | "
                    f"deep={deep}h | REM={rem}h | light={light}h | awake={awake}h{extra_str}"
                )

    # ── HRV ──
    if "hrv" in data and isinstance(data["hrv"], list) and data["hrv"]:
        lines.append(f"HRV: {_hrv_trend_summary(data['hrv'])}")
        daily = ", ".join(f"{h['date']}: {h['hrv']}ms" for h in data["hrv"])
        lines.append(f"  Daily HRV (recent first): {daily}")

    # ── Resting HR ──
    # Note: Garmin reports RHR for the date you wake up (e.g. "2026-06-08" = measured during June 7–8 night,
    # same physiological night as sleep entry dated "2026-06-07").
    if "resting_hr" in data and isinstance(data["resting_hr"], list) and data["resting_hr"]:
        rhr_list = data["resting_hr"]
        vals = [r["rhr"] for r in rhr_list]
        avg_rhr = sum(vals) / len(vals)
        latest_rhr = vals[0]
        delta = latest_rhr - avg_rhr
        direction = "↑ elevated" if delta > 2 else ("↓ low" if delta < -2 else "→ normal")
        lines.append(
            f"Resting HR: latest={latest_rhr} bpm | {len(vals)}-day avg={avg_rhr:.0f} bpm | {direction}"
        )
        daily_rhr = ", ".join(f"{r['date']}: {r['rhr']}bpm" for r in rhr_list)
        lines.append(f"  Daily RHR (recent first, date = wakeup morning): {daily_rhr}")

    # ── Body Battery ──
    if "body_battery" in data and isinstance(data["body_battery"], list) and data["body_battery"]:
        bb_list = data["body_battery"]
        today_bb = bb_list[0]
        lines.append(
            f"Body Battery (today): started={today_bb.get('start', '?')} | current={today_bb.get('end', '?')}"
        )
        trend_str = _body_battery_trend(bb_list)
        if trend_str:
            lines.append(trend_str)
        if len(bb_list) > 1:
            history = ", ".join(
                f"{b['date']}: {b.get('start', '?')}→{b.get('end', '?')}" for b in bb_list[1:]
            )
            lines.append(f"  Prior days (start→end): {history}")

    # ── Stress ──
    if "stress" in data and isinstance(data["stress"], list) and data["stress"]:
        st_list = data["stress"]
        avgs = [s["average"] for s in st_list if s.get("average") is not None]
        rolling_avg = sum(avgs) / len(avgs) if avgs else None
        today_st = st_list[0]
        stress_line = f"Stress (today): avg={today_st.get('average', '?')} | max={today_st.get('max', '?')}"
        if rolling_avg is not None:
            stress_line += f" | {len(avgs)}-day rolling avg={rolling_avg:.0f}"
        lines.append(stress_line)
        if len(st_list) > 1:
            history = ", ".join(f"{s['date']}: avg={s['average']}" for s in st_list[1:])
            lines.append(f"  Prior stress (avg): {history}")

    # ── Training Readiness ──
    if "training_readiness" in data and isinstance(data["training_readiness"], list) and data["training_readiness"]:
        tr_list = data["training_readiness"]
        today_tr = tr_list[0]
        tr_line = f"Training Readiness (today): score={today_tr.get('score', '?')}"
        if today_tr.get("level"):
            tr_line += f" ({today_tr['level']})"
        if today_tr.get("feedback"):
            tr_line += f" — {today_tr['feedback']}"
        lines.append(tr_line)
        if len(tr_list) > 1:
            history = ", ".join(f"{t['date']}: {t['score']}" for t in tr_list[1:])
            lines.append(f"  Prior readiness scores: {history}")

    # ── VO2 Max ──
    if data.get("vo2max") is not None:
        lines.append(f"VO2 Max estimate: {data['vo2max']:.1f} ml/kg/min")

    # ── Lactate Threshold ──
    lt = data.get("lactate_threshold")
    if lt and isinstance(lt, dict):
        parts = []
        if lt.get("hr"):
            parts.append(f"LT HR={lt['hr']} bpm")
        if lt.get("pace_spm"):
            parts.append(f"LT pace={_pace_str(1.0 / lt['pace_spm'], units)}")
        if parts:
            lines.append(f"Lactate Threshold: {' | '.join(parts)}")

    # ── Training Status + Recovery Time + Load Focus ──
    ts = data.get("training_status")
    if ts and isinstance(ts, dict):
        ts_parts = []
        if ts.get("status"):
            ts_parts.append(f"status={ts['status']}")
        if ts.get("recovery_hours") is not None:
            rh = ts["recovery_hours"]
            ts_parts.append(f"recovery time={rh}h")
        if ts_parts:
            lines.append(f"Training Status: {' | '.join(ts_parts)}")
        lf = ts.get("load_focus")
        if lf and isinstance(lf, dict):
            lf_parts = []
            for bucket in ("base", "tempo", "threshold", "anaerobic", "highAerobic", "lowAerobic"):
                v = lf.get(bucket)
                if v is not None:
                    lf_parts.append(f"{bucket}={v:.0f}%")
            if lf_parts:
                lines.append(f"  Load Focus: {', '.join(lf_parts)}")

    # ── Training Load / ACWR ──
    tl = data.get("training_load")
    if tl and isinstance(tl, dict):
        tl_parts = []
        if tl.get("acute") is not None:
            tl_parts.append(f"acute (7-day)={tl['acute']:.0f}")
        if tl.get("chronic") is not None:
            tl_parts.append(f"chronic (28-day)={tl['chronic']:.0f}")
        if tl.get("acwr") is not None:
            acwr = tl["acwr"]
            if acwr < 0.8:
                acwr_flag = "↓ undertraining risk"
            elif acwr > 1.5:
                acwr_flag = "↑↑ high injury risk"
            elif acwr > 1.3:
                acwr_flag = "↑ elevated — monitor"
            else:
                acwr_flag = "→ optimal range"
            tl_parts.append(f"ACWR={acwr:.2f} ({acwr_flag})")
        if tl_parts:
            lines.append(f"Training Load: {' | '.join(tl_parts)}")

    # ── Steps ──
    if "steps" in data and isinstance(data["steps"], list):
        step_strs = [f"{s['date']}: {s['steps']:,}" for s in data["steps"]]
        lines.append("Steps: " + ", ".join(step_strs))

    # ── Activities ──
    if "activities" in data and isinstance(data["activities"], list):
        activities = data["activities"]
        lines.append(f"\n{_training_load_trend(activities)}")
        lines.append(_activity_pattern(activities, lookback_days, tz_str))
        lines.append(f"\nRecent activities ({len(activities)} in lookback window):")

        # LT HR used for zone computation across all activities
        lt = data.get("lactate_threshold")
        lt_hr = lt.get("hr") if lt and isinstance(lt, dict) else None

        for a in activities:
            dist = a.get("distance_meters")
            dist_str = (
                f"{dist / 1609.34:.2f} mi" if dist and units == "imperial"
                else (f"{dist / 1000:.2f} km" if dist else "?")
            )
            dur = a.get("duration_seconds")
            dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "?"
            pace = _pace_str(a.get("avg_speed_ms"), units)
            elev = a.get("elevation_gain")
            elev_str = (
                f"{elev:.0f}ft gain" if elev and units == "imperial"
                else (f"{elev:.0f}m gain" if elev else "")
            )
            cadence = a.get("avg_cadence")
            cadence_str = f"cadence={cadence:.0f}spm" if cadence else ""
            te = ""
            if a.get("aerobic_te") is not None:
                te = f"aerobic TE={a['aerobic_te']:.1f} ({_classify_activity_load(a['aerobic_te'])})"
            if a.get("anaerobic_te") is not None:
                te += f" / anaerobic TE={a['anaerobic_te']:.1f}"

            # Running dynamics
            dynamics = []
            if a.get("avg_ground_contact_ms") is not None:
                dynamics.append(f"GCT={a['avg_ground_contact_ms']:.0f}ms")
            if a.get("avg_vertical_oscillation_mm") is not None:
                dynamics.append(f"vert osc={a['avg_vertical_oscillation_mm']:.1f}mm")
            if a.get("avg_stride_length_cm") is not None:
                stride_m = a["avg_stride_length_cm"] / 100
                if units == "imperial":
                    dynamics.append(f"stride={stride_m * 3.281:.2f}ft")
                else:
                    dynamics.append(f"stride={stride_m:.2f}m")
            if a.get("avg_vertical_ratio") is not None:
                dynamics.append(f"vert ratio={a['avg_vertical_ratio']:.1f}%")
            dynamics_str = " | ".join(dynamics)

            details = " | ".join(filter(None, [
                dist_str,
                f"time={dur_str}",
                f"avg pace={pace}",
                f"avg HR={a.get('avg_hr', '?')}",
                f"max HR={a.get('max_hr', '?')}",
                elev_str,
                cadence_str,
                f"TSS={a.get('tss', '?')}",
                te,
            ]))

            name = a.get("name") or a.get("type", "activity")
            weather_str = ""
            if a.get("weather") and a["weather"].get("summary"):
                weather_str = f" | weather: {a['weather']['summary']}"
            lines.append(f"  [{a['date']} {a.get('time', '')}] {name}: {details}{weather_str}")
            if dynamics_str:
                lines.append(f"    Running dynamics: {dynamics_str}")

            # HR zone distribution (only if laps available + LT HR known)
            laps = a.get("laps", [])
            zone_str = _compute_hr_zones(laps, lt_hr)
            if zone_str:
                lines.append(f"    {zone_str}")

            # Lap breakdown
            if laps:
                lines.append("    Laps:")
                for lap in laps:
                    lap_dist = lap.get("distance_meters")
                    lap_dist_str = (
                        f"{lap_dist / 1609.34:.2f} mi" if lap_dist and units == "imperial"
                        else (f"{lap_dist / 1000:.2f} km" if lap_dist else "?")
                    )
                    lap_dur = lap.get("duration_seconds")
                    lap_dur_str = f"{int(lap_dur // 60)}:{int(lap_dur % 60):02d}" if lap_dur else "?"
                    lap_pace = _pace_str(lap.get("avg_speed_ms"), units)
                    lap_elev = lap.get("elevation_gain")
                    lap_elev_str = (
                        f"+{lap_elev:.0f}ft" if lap_elev and units == "imperial"
                        else (f"+{lap_elev:.0f}m" if lap_elev else "")
                    )
                    lap_cadence = lap.get("avg_cadence")
                    lap_cadence_str = f"cadence={lap_cadence:.0f}spm" if lap_cadence else ""
                    lap_gct = lap.get("avg_ground_contact_ms")
                    lap_gct_str = f"GCT={lap_gct:.0f}ms" if lap_gct else ""
                    lap_vo = lap.get("avg_vertical_oscillation_mm")
                    lap_vo_str = f"vert osc={lap_vo:.1f}mm" if lap_vo else ""
                    lap_stride = lap.get("avg_stride_length_cm")
                    if lap_stride:
                        stride_m = lap_stride / 100
                        lap_stride_str = (
                            f"stride={stride_m * 3.281:.2f}ft" if units == "imperial"
                            else f"stride={stride_m:.2f}m"
                        )
                    else:
                        lap_stride_str = ""

                    lap_details = " | ".join(filter(None, [
                        lap_dist_str,
                        lap_dur_str,
                        f"pace={lap_pace}",
                        f"avg HR={lap.get('avg_hr', '?')}",
                        f"max HR={lap.get('max_hr', '?')}",
                        lap_elev_str,
                        lap_cadence_str,
                        lap_gct_str,
                        lap_vo_str,
                        lap_stride_str,
                    ]))
                    lines.append(f"      Lap {lap['lap']}: {lap_details}")

    return "\n".join(lines) if lines else "No Garmin data available."

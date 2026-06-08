from __future__ import annotations

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


def _fitness_history_path(config: dict) -> Path:
    return Path(config.get("paths", {}).get("fitness_history_file", "logs/fitness_history.json"))


def _append_fitness_history(config: dict, today: date, data: dict) -> None:
    """
    Append today's key fitness metrics to a rolling history file.
    Used to track VO2 max and training status trends over time.
    Keeps one entry per date (upserts by date).
    """
    try:
        path = _fitness_history_path(config)
        path.parent.mkdir(exist_ok=True)
        history: list[dict] = json.loads(path.read_text()) if path.exists() else []

        # Build today's snapshot
        vo2 = data.get("vo2max")
        ts = data.get("training_status") or {}
        hrv_list = data.get("hrv") or []
        rhr_list = data.get("resting_hr") or []
        today_str = today.isoformat()

        tl = data.get("training_load") or {}
        entry = {
            "date": today_str,
            "vo2max": float(vo2) if vo2 is not None else None,
            "training_status": (data.get("training_status") or {}).get("status") if isinstance(data.get("training_status"), dict) else None,
            "acwr": tl.get("acwr"),
            "hrv": hrv_list[0].get("hrv") if hrv_list and isinstance(hrv_list[0], dict) else None,
            "rhr": rhr_list[0].get("rhr") if rhr_list and isinstance(rhr_list[0], dict) else None,
        }

        # Upsert: replace existing entry for today if present
        history = [h for h in history if h.get("date") != today_str]
        history.append(entry)

        # Keep last 180 days
        history = sorted(history, key=lambda h: h["date"], reverse=True)[:180]
        path.write_text(json.dumps(history, indent=2))
    except Exception:
        pass  # Never let history logging break a fetch


def load_fitness_history(config: dict | None = None, days: int = 90) -> list[dict]:
    """Load the rolling fitness history, newest first, limited to `days` entries."""
    if config is None:
        config = load_config()
    path = _fitness_history_path(config)
    if not path.exists():
        return []
    try:
        history = json.loads(path.read_text())
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        return [h for h in history if h.get("date", "") >= cutoff]
    except Exception:
        return []


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


# ── Per-metric fetch helpers ────────────────────────────────────────────────────
# Each helper takes (client, today, lookback_days) and returns the parsed list/dict.
# Isolated try/except means one bad endpoint never kills other metrics.

def _fetch_sleep(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i + 1)).isoformat()
        try:
            raw = client.get_sleep_data(d)
            ds = raw.get("dailySleepDTO", {})
            total_sec = ds.get("sleepTimeSeconds")
            if not total_sec:
                continue
            result.append({
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
    return result


def _fetch_hrv(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i + 1)).isoformat()
        try:
            raw = client.get_hrv_data(d)
            summary = raw.get("hrvSummary", {})
            # Garmin returns lastNightAvg (overnight avg), not lastNight
            val = summary.get("lastNightAvg") or summary.get("lastNight")
            status = summary.get("status") or summary.get("hrvStatus")
            weekly_avg = summary.get("weeklyAvg")
            if val:
                result.append({"date": d, "hrv": val, "status": status, "weekly_avg": weekly_avg})
        except Exception:
            pass
    return result


def _fetch_rhr(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_rhr_day(d)
            val = None
            if isinstance(raw, dict):
                # Format: {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": X}]}}}
                metrics_map = raw.get("allMetrics", {}).get("metricsMap", {})
                rhr_entries = metrics_map.get("WELLNESS_RESTING_HEART_RATE", [])
                if rhr_entries:
                    val = rhr_entries[0].get("value")
                if val is None:
                    val = (raw.get("value") or {}).get("value") or raw.get("restingHeartRate")
            elif isinstance(raw, list) and raw:
                val = raw[0].get("value") or raw[0].get("restingHeartRate")
            if val:
                result.append({"date": d, "rhr": int(val)})
        except Exception:
            pass
    return result


def _fetch_body_battery(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_body_battery(d)
            if not raw or not isinstance(raw, list):
                continue
            entry = raw[0] if isinstance(raw[0], dict) else None
            if entry:
                # Format: [{"date": ..., "bodyBatteryValuesArray": [[ts_ms, val], ...]}]
                values = entry.get("bodyBatteryValuesArray", [])
                if values:
                    start_val = values[0][1] if values[0] else None
                    end_val = values[-1][1] if values[-1] else None
                else:
                    start_val = entry.get("charged")
                    end_val = None
            else:
                # Older format: list of [ts_ms, val] pairs
                start_val = raw[0][1] if isinstance(raw[0], (list, tuple)) else None
                end_val = raw[-1][1] if isinstance(raw[-1], (list, tuple)) else None
            if start_val is not None:
                result.append({"date": d, "start": start_val, "end": end_val})
        except Exception:
            pass
    return result


def _fetch_stress(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_stress_data(d)
            if not isinstance(raw, dict):
                continue
            # Garmin returns avgStressLevel (not overallStressLevel)
            avg = raw.get("avgStressLevel") or raw.get("overallStressLevel")
            max_s = raw.get("maxStressLevel")
            if avg is not None and avg >= 0:
                result.append({"date": d, "average": avg, "max": max_s})
        except Exception:
            pass
    return result


def _fetch_training_readiness(client, today: date, lookback_days: int) -> list[dict]:
    result = []
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
                    result.append({"date": d, "score": score, "level": level, "feedback": feedback})
                    break
        except Exception:
            pass
    return result


def _fetch_steps(client, today: date, lookback_days: int) -> list[dict]:
    result = []
    for i in range(lookback_days):
        d = (today - timedelta(days=i)).isoformat()
        try:
            raw = client.get_steps_data(d)
            total = sum(s.get("steps", 0) for s in raw) if isinstance(raw, list) else 0
            result.append({"date": d, "steps": total})
        except Exception:
            pass
    return result


_TRAINING_STATUS_CODES = {
    0: "Unknown", 1: "No Status", 2: "Overreaching", 3: "Maintaining",
    4: "Productive", 5: "Recovering", 6: "Peaking", 7: "Detraining",
}


def _fetch_training_and_vo2(client, today: date) -> dict:
    """
    Fetch training status, load, ACWR, load focus, and VO2 max from a single
    get_training_status() call. Returns a dict with keys:
    vo2max, training_status, training_load.
    """
    out: dict = {"vo2max": None, "training_status": None, "training_load": None}
    try:
        raw = client.get_training_status(today.isoformat())
        if not isinstance(raw, dict):
            return out

        # ── VO2 max ──
        generic = (raw.get("mostRecentVO2Max") or {}).get("generic") or {}
        vo2 = generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue")
        out["vo2max"] = float(vo2) if vo2 is not None else None

        # ── Training Status + ACWR + load ──
        status_map = ((raw.get("mostRecentTrainingStatus") or {})
                      .get("latestTrainingStatusData") or {})
        device_entry = None
        for entry in status_map.values():
            if isinstance(entry, dict) and entry.get("trainingStatus") is not None:
                device_entry = entry
                break

        if device_entry:
            code = device_entry.get("trainingStatus")
            status_str = _TRAINING_STATUS_CODES.get(code, f"code={code}") if code is not None else None
            out["training_status"] = {"status": status_str, "recovery_hours": None}

            # ACWR + load: nested inside device_entry.acuteTrainingLoadDTO
            atl_dto = device_entry.get("acuteTrainingLoadDTO") or {}
            acwr = atl_dto.get("dailyAcuteChronicWorkloadRatio")
            acute = atl_dto.get("dailyTrainingLoadAcute")
            chronic = atl_dto.get("dailyTrainingLoadChronic")
            if acwr is not None:
                out["training_load"] = {
                    "acute": round(acute, 1) if acute is not None else None,
                    "chronic": round(chronic, 1) if chronic is not None else None,
                    "acwr": round(acwr, 2),
                }

        # ── Load focus ──
        lb_map = ((raw.get("mostRecentTrainingLoadBalance") or {})
                  .get("metricsTrainingLoadBalanceDTOMap") or {})
        lf_entry = next(iter(lb_map.values()), None) if lb_map else None
        if lf_entry and out["training_status"] is not None:
            out["training_status"]["load_focus"] = {
                "aerobicLow": lf_entry.get("monthlyLoadAerobicLow"),
                "aerobicHigh": lf_entry.get("monthlyLoadAerobicHigh"),
                "anaerobic": lf_entry.get("monthlyLoadAnaerobic"),
                "feedback": lf_entry.get("trainingBalanceFeedbackPhrase"),
            }
    except Exception:
        pass
    return out


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

    # Retry login up to 2 times on transient failures
    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    last_exc = None
    for attempt in range(3):
        try:
            client.login()
            break
        except Exception as e:
            last_exc = e
            import time; time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"Garmin login failed after 3 attempts: {last_exc}")

    data = {}

    if metrics.get("sleep"):
        try:
            data["sleep"] = _fetch_sleep(client, today, lookback_days)
        except Exception as e:
            data["sleep"] = {"error": str(e)}

    if metrics.get("hrv"):
        try:
            data["hrv"] = _fetch_hrv(client, today, lookback_days)
        except Exception as e:
            data["hrv"] = {"error": str(e)}

    if metrics.get("resting_hr"):
        try:
            data["resting_hr"] = _fetch_rhr(client, today, lookback_days)
        except Exception as e:
            data["resting_hr"] = {"error": str(e)}

    if metrics.get("body_battery"):
        try:
            data["body_battery"] = _fetch_body_battery(client, today, lookback_days)
        except Exception as e:
            data["body_battery"] = {"error": str(e)}

    if metrics.get("steps"):
        try:
            data["steps"] = _fetch_steps(client, today, lookback_days)
        except Exception as e:
            data["steps"] = {"error": str(e)}

    if metrics.get("stress"):
        try:
            data["stress"] = _fetch_stress(client, today, lookback_days)
        except Exception as e:
            data["stress"] = {"error": str(e)}

    if metrics.get("training_readiness"):
        try:
            data["training_readiness"] = _fetch_training_readiness(client, today, lookback_days)
        except Exception as e:
            data["training_readiness"] = {"error": str(e)}

    # Training status, load, ACWR, VO2 max all from a single get_training_status() call
    if metrics.get("training_status") or metrics.get("training_load") or metrics.get("vo2max"):
        result = _fetch_training_and_vo2(client, today)
        data["vo2max"] = result["vo2max"]
        data["training_status"] = result["training_status"]
        data["training_load"] = result["training_load"]

    # LT HR is not exposed by garminconnect on this device — read from config if manually set.
    lt_hr = g_cfg.get("lactate_threshold_hr")
    data["lactate_threshold"] = {"hr": int(lt_hr)} if lt_hr else None

    # get_user_summary: enriches body battery and respiration with today's precise values
    try:
        summary = client.get_user_summary(today.isoformat())
        if isinstance(summary, dict):
            data["today_summary"] = {
                "body_battery_wake": summary.get("bodyBatteryAtWakeTime"),
                "body_battery_high": summary.get("bodyBatteryHighestValue"),
                "body_battery_low": summary.get("bodyBatteryLowestValue"),
                "body_battery_now": summary.get("bodyBatteryMostRecentValue"),
                "avg_waking_respiration": summary.get("avgWakingRespirationValue"),
                "avg_spo2": summary.get("averageSpo2"),
                "lowest_spo2": summary.get("lowestSpo2"),
                "resting_hr": summary.get("restingHeartRate"),
                "avg_stress": summary.get("averageStressLevel"),
                "steps": summary.get("totalSteps"),
            }
        else:
            data["today_summary"] = None
    except Exception:
        data["today_summary"] = None

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
    _append_fitness_history(config, today, data)
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

    # ── Today's waking vitals (from get_user_summary — more accurate than nightly averages) ──
    ts = data.get("today_summary") or {}
    waking_resp = ts.get("avg_waking_respiration")
    avg_spo2 = ts.get("avg_spo2")
    low_spo2 = ts.get("lowest_spo2")
    if waking_resp is not None or avg_spo2 is not None:
        vitals_parts = []
        if waking_resp is not None:
            flag = " ⚠" if waking_resp > 18 else ""
            vitals_parts.append(f"waking resp={waking_resp:.1f} br/min{flag}")
        if avg_spo2 is not None:
            flag = " ⚠" if avg_spo2 < 95 else ""
            vitals_parts.append(f"avg SpO2={avg_spo2:.1f}%{flag}")
        if low_spo2 is not None and low_spo2 < 94:
            vitals_parts.append(f"low SpO2={low_spo2:.1f}% ⚠")
        lines.append(f"Today's waking vitals: {' | '.join(vitals_parts)}")

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
        ts = data.get("today_summary") or {}
        wake_val = ts.get("body_battery_wake") or today_bb.get("start", "?")
        now_val = ts.get("body_battery_now") or today_bb.get("end", "?")
        high_val = ts.get("body_battery_high")
        bb_line = f"Body Battery (today): wake={wake_val} | current={now_val}"
        if high_val is not None:
            bb_line += f" | daily_high={high_val}"
        lines.append(bb_line)
        trend_str = _body_battery_trend(bb_list)
        if trend_str:
            lines.append(trend_str)
        if len(bb_list) > 1:
            history = ", ".join(
                f"{b['date']}: {b.get('start', '?')}→{b.get('end', '?')}" for b in bb_list[1:]
            )
            lines.append(f"  Prior days (wake→end): {history}")

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

    # ── VO2 Max + trend ──
    if data.get("vo2max") is not None:
        vo2_line = f"VO2 Max estimate: {data['vo2max']:.1f} ml/kg/min"
        # Append trend from fitness history (show last 4 distinct values)
        try:
            fh = load_fitness_history(config, days=90)
            vo2_vals = [(h["date"], h["vo2max"]) for h in fh if h.get("vo2max") is not None]
            if len(vo2_vals) >= 2:
                oldest, newest = vo2_vals[-1][1], vo2_vals[0][1]
                delta = newest - oldest
                direction = "↑" if delta > 0.5 else ("↓" if delta < -0.5 else "→")
                trend_str = " | ".join(f"{v:.1f}" for _, v in reversed(vo2_vals[:4]))
                vo2_line += f" | 90-day trend {direction}: {trend_str}"
        except Exception:
            pass
        lines.append(vo2_line)

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
        # Training status trend from fitness history
        try:
            fh = load_fitness_history(config, days=60)
            status_vals = [(h["date"], h["training_status"]) for h in fh if h.get("training_status")]
            if len(status_vals) >= 2:
                recent = status_vals[:6]
                trend_str = " → ".join(f"{v}" for _, v in reversed(recent))
                lines.append(f"  Training Status trend (60d): {trend_str}")
        except Exception:
            pass
        lf = ts.get("load_focus")
        if lf and isinstance(lf, dict):
            lf_parts = []
            # Field names from actual API response
            field_labels = [
                ("aerobicLow", "aerobic base"),
                ("aerobicHigh", "aerobic high"),
                ("anaerobic", "anaerobic"),
            ]
            for key, label in field_labels:
                v = lf.get(key)
                if v is not None:
                    lf_parts.append(f"{label}={v:.0f}")
            feedback = lf.get("feedback")
            if feedback:
                lf_parts.append(f"({feedback.lower().replace('_', ' ')})")
            if lf_parts:
                lines.append(f"  Load Focus (monthly load): {', '.join(lf_parts)}")

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


def format_garmin_body_state(data: dict, units: str = "imperial") -> str:
    """
    Condensed readiness snapshot — sleep summary, HRV, RHR, body battery,
    stress, training readiness, training status, and ACWR only.
    No activity lap data. Used for today_readiness_check and post_workout_checkin
    where activity detail is irrelevant and would waste context.
    """
    config = load_config()
    lines = []

    # Sleep — summary line only (no per-night detail)
    if "sleep" in data and isinstance(data["sleep"], list) and data["sleep"]:
        s_data = data["sleep"]
        lines.append(f"Sleep trend: {_sleep_trend_summary(s_data)}")
        last = s_data[0]
        total_hrs = round(last["total_seconds"] / 3600, 1) if last.get("total_seconds") else "?"
        deep = round(last["deep_seconds"] / 3600, 1) if last.get("deep_seconds") else "?"
        rem = round(last["rem_seconds"] / 3600, 1) if last.get("rem_seconds") else "?"
        lines.append(f"  Last night: {total_hrs}h total | score={last.get('score', '?')} | deep={deep}h | REM={rem}h")
        resp_flags = [s for s in s_data if s.get("avg_respiration") and s["avg_respiration"] > 18]
        spo2_flags = [s for s in s_data if s.get("avg_spo2") and s["avg_spo2"] < 95]
        if resp_flags:
            lines.append(f"  ⚠ Elevated respiration: {', '.join(s['date'] for s in resp_flags[:2])}")
        if spo2_flags:
            lines.append(f"  ⚠ Low SpO2: {', '.join(s['date'] for s in spo2_flags[:2])}")

    # Waking vitals
    ts = data.get("today_summary") or {}
    waking_resp = ts.get("avg_waking_respiration")
    avg_spo2 = ts.get("avg_spo2")
    if waking_resp is not None or avg_spo2 is not None:
        parts = []
        if waking_resp is not None:
            parts.append(f"waking resp={waking_resp:.1f} br/min" + (" ⚠" if waking_resp > 18 else ""))
        if avg_spo2 is not None:
            parts.append(f"avg SpO2={avg_spo2:.1f}%" + (" ⚠" if avg_spo2 < 95 else ""))
        lines.append(f"Today's waking vitals: {' | '.join(parts)}")

    # HRV — latest + 10-day avg only
    if "hrv" in data and isinstance(data["hrv"], list) and data["hrv"]:
        lines.append(f"HRV: {_hrv_trend_summary(data['hrv'])}")

    # Resting HR — latest + avg only
    if "resting_hr" in data and isinstance(data["resting_hr"], list) and data["resting_hr"]:
        rhr_list = data["resting_hr"]
        vals = [r["rhr"] for r in rhr_list]
        avg_rhr = sum(vals) / len(vals)
        latest = vals[0]
        delta = latest - avg_rhr
        direction = "↑ elevated" if delta > 2 else ("↓ low" if delta < -2 else "→ normal")
        lines.append(f"Resting HR: latest={latest} bpm | {len(vals)}-day avg={avg_rhr:.0f} bpm | {direction}")

    # Body battery — today only
    if "body_battery" in data and isinstance(data["body_battery"], list) and data["body_battery"]:
        bb = data["body_battery"][0]
        wake_val = ts.get("body_battery_wake") or bb.get("start", "?")
        now_val = ts.get("body_battery_now") or bb.get("end", "?")
        high_val = ts.get("body_battery_high")
        bb_line = f"Body Battery: wake={wake_val} | current={now_val}"
        if high_val is not None:
            bb_line += f" | daily_high={high_val}"
        lines.append(bb_line)

    # Stress — today only
    if "stress" in data and isinstance(data["stress"], list) and data["stress"]:
        st = data["stress"][0]
        avgs = [s["average"] for s in data["stress"] if s.get("average") is not None]
        rolling = f" | {len(avgs)}-day avg={sum(avgs)/len(avgs):.0f}" if avgs else ""
        lines.append(f"Stress: avg={st.get('average', '?')} | max={st.get('max', '?')}{rolling}")

    # Training Readiness — today only
    if "training_readiness" in data and isinstance(data["training_readiness"], list) and data["training_readiness"]:
        tr = data["training_readiness"][0]
        tr_line = f"Training Readiness: score={tr.get('score', '?')}"
        if tr.get("level"):
            tr_line += f" ({tr['level']})"
        lines.append(tr_line)

    # Training Status + ACWR
    ts_data = data.get("training_status")
    if ts_data and isinstance(ts_data, dict):
        parts = []
        if ts_data.get("status"):
            parts.append(f"status={ts_data['status']}")
        if ts_data.get("recovery_hours") is not None:
            parts.append(f"recovery time={ts_data['recovery_hours']}h")
        if parts:
            lines.append(f"Training Status: {' | '.join(parts)}")

    tl = data.get("training_load")
    if tl and isinstance(tl, dict):
        tl_parts = []
        if tl.get("acute") is not None:
            tl_parts.append(f"acute={tl['acute']:.0f}")
        if tl.get("chronic") is not None:
            tl_parts.append(f"chronic={tl['chronic']:.0f}")
        if tl.get("acwr") is not None:
            acwr = tl["acwr"]
            flag = "↓ undertraining" if acwr < 0.8 else ("↑↑ high injury risk" if acwr > 1.5 else ("↑ elevated" if acwr > 1.3 else "optimal"))
            tl_parts.append(f"ACWR={acwr:.2f} ({flag})")
        if tl_parts:
            lines.append(f"Training Load: {' | '.join(tl_parts)}")

    return "\n".join(lines) if lines else "No body state data available."

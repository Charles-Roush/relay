import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv(override=True)

_CACHE_DIR = Path("logs")
_config: dict | None = None


def load_config() -> dict:
    global _config
    if _config is None:
        with open("config.yaml") as f:
            _config = yaml.safe_load(f)
    return _config


def _cache_path(d: date) -> Path:
    return _CACHE_DIR / f"garmin_cache_{d.isoformat()}.json"


def _load_cache(d: date) -> dict | None:
    path = _cache_path(d)
    if path.exists():
        return json.loads(path.read_text())
    return None


def _save_cache(d: date, data: dict):
    _CACHE_DIR.mkdir(exist_ok=True)
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
    activities_count = g_cfg["activities_count"]

    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    client.login()
    yesterday = today - timedelta(days=1)
    start_date = today - timedelta(days=lookback_days)

    data = {}

    if metrics.get("sleep"):
        try:
            raw = client.get_sleep_data(yesterday.isoformat())
            ds = raw.get("dailySleepDTO", {})
            data["sleep"] = {
                "date": yesterday.isoformat(),
                "total_seconds": ds.get("sleepTimeSeconds"),
                "deep_seconds": ds.get("deepSleepSeconds"),
                "rem_seconds": ds.get("remSleepSeconds"),
                "light_seconds": ds.get("lightSleepSeconds"),
                "awake_seconds": ds.get("awakeSleepSeconds"),
                "score": ds.get("sleepScores", {}).get("overall", {}).get("value"),
                "avg_respiration": ds.get("averageRespirationValue"),
                "avg_spo2": ds.get("averageSpO2Value"),
            }
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
                    if val:
                        hrv_values.append({"date": d, "hrv": val, "status": status})
                except Exception:
                    pass
            data["hrv"] = hrv_values
        except Exception as e:
            data["hrv"] = {"error": str(e)}

    if metrics.get("body_battery"):
        try:
            raw = client.get_body_battery(today.isoformat())
            start_val = raw[0][1] if raw else None
            end_val = raw[-1][1] if raw else None
            data["body_battery"] = {
                "date": today.isoformat(),
                "start_value": start_val,
                "end_value": end_val,
            }
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
            raw = client.get_stress_data(today.isoformat())
            avg = raw.get("overallStressLevel")
            max_stress = raw.get("maxStressLevel")
            data["stress"] = {
                "date": today.isoformat(),
                "average": avg,
                "max": max_stress,
            }
        except Exception as e:
            data["stress"] = {"error": str(e)}

    if metrics.get("activities"):
        try:
            raw = client.get_activities(0, activities_count)
            activities = []
            lap_fetch_cutoff = today - timedelta(days=2)  # fetch laps for last 2 days only

            for a in raw:
                avg_speed = a.get("averageSpeed")  # meters/second
                activity_date_str = a.get("startTimeLocal", "")[:10]
                activity_id = a.get("activityId")

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
                    "laps": [],
                }

                # Fetch lap detail for recent activities
                try:
                    activity_date = date.fromisoformat(activity_date_str)
                    if activity_id and activity_date >= lap_fetch_cutoff:
                        laps_raw = client.get_activity_splits(activity_id)
                        lap_list = laps_raw.get("lapDTOs") or laps_raw.get("laps", [])
                        laps = []
                        for i, lap in enumerate(lap_list):
                            lap_speed = lap.get("averageSpeed")
                            laps.append({
                                "lap": i + 1,
                                "distance_meters": lap.get("distance"),
                                "duration_seconds": lap.get("duration"),
                                "avg_hr": lap.get("averageHR"),
                                "max_hr": lap.get("maxHR"),
                                "avg_speed_ms": lap_speed,
                                "avg_cadence": lap.get("averageRunningCadenceInStepsPerMinute"),
                                "elevation_gain": lap.get("elevationGain"),
                            })
                        entry["laps"] = laps
                except Exception:
                    pass  # laps are best-effort, never block the main fetch

                activities.append(entry)
            data["activities"] = activities
        except Exception as e:
            data["activities"] = {"error": str(e)}

    _save_cache(today, data)
    return data


def _hrv_trend_summary(hrv_values: list[dict]) -> str:
    """Compute 7-day HRV avg, deviation from baseline, and trend arrow."""
    vals = [h["hrv"] for h in hrv_values]
    if not vals:
        return "no data"
    avg = sum(vals) / len(vals)
    latest = vals[0]  # most recent is index 0 (last night first)
    delta = latest - avg
    if delta > 5:
        trend = "↑ above baseline"
    elif delta < -5:
        trend = "↓ below baseline (recovery flag)"
    else:
        trend = "→ near baseline"
    status = hrv_values[0].get("status", "")
    status_str = f", Garmin status: {status}" if status else ""
    return (
        f"Latest: {latest}ms | {len(vals)}-day avg: {avg:.0f}ms | "
        f"Deviation: {delta:+.0f}ms {trend}{status_str}"
    )


def _pace_str(avg_speed_ms: float | None, units: str) -> str:
    """Convert avg speed (m/s) to pace string."""
    if not avg_speed_ms or avg_speed_ms <= 0:
        return "?"
    if units == "imperial":
        # min per mile
        seconds_per_mile = 1609.34 / avg_speed_ms
        mins = int(seconds_per_mile // 60)
        secs = int(seconds_per_mile % 60)
        return f"{mins}:{secs:02d}/mi"
    else:
        seconds_per_km = 1000 / avg_speed_ms
        mins = int(seconds_per_km // 60)
        secs = int(seconds_per_km % 60)
        return f"{mins}:{secs:02d}/km"


def format_garmin_data(data: dict, units: str = "imperial") -> str:
    lines = []

    if "sleep" in data:
        s = data["sleep"]
        if "error" not in s:
            total_hrs = round(s["total_seconds"] / 3600, 1) if s.get("total_seconds") else "?"
            deep = round(s["deep_seconds"] / 3600, 1) if s.get("deep_seconds") else "?"
            rem = round(s["rem_seconds"] / 3600, 1) if s.get("rem_seconds") else "?"
            awake = round(s["awake_seconds"] / 3600, 1) if s.get("awake_seconds") else "?"
            extras = []
            if s.get("avg_respiration"):
                extras.append(f"respiration: {s['avg_respiration']:.1f} br/min")
            if s.get("avg_spo2"):
                extras.append(f"SpO2: {s['avg_spo2']:.1f}%")
            extra_str = f" | {', '.join(extras)}" if extras else ""
            lines.append(
                f"Sleep ({s['date']}): {total_hrs}h total | score={s.get('score', '?')} | "
                f"deep={deep}h | REM={rem}h | awake={awake}h{extra_str}"
            )

    if "hrv" in data and isinstance(data["hrv"], list) and data["hrv"]:
        trend = _hrv_trend_summary(data["hrv"])
        lines.append(f"HRV: {trend}")
        daily = ", ".join(f"{h['date']}: {h['hrv']}ms" for h in data["hrv"])
        lines.append(f"  Daily readings (recent first): {daily}")

    if "body_battery" in data:
        bb = data["body_battery"]
        if "error" not in bb:
            lines.append(
                f"Body Battery: started day at {bb.get('start_value', '?')} | "
                f"current: {bb.get('end_value', '?')}"
            )

    if "steps" in data and isinstance(data["steps"], list):
        step_strs = [f"{s['date']}: {s['steps']:,}" for s in data["steps"]]
        lines.append("Steps: " + ", ".join(step_strs))

    if "stress" in data:
        st = data["stress"]
        if "error" not in st:
            lines.append(f"Stress: avg={st.get('average', '?')} | max={st.get('max', '?')}")

    if "activities" in data and isinstance(data["activities"], list):
        lines.append(f"\nRecent activities ({len(data['activities'])} shown):")
        for a in data["activities"]:
            dist = a.get("distance_meters")
            if dist:
                if units == "imperial":
                    dist_str = f"{dist / 1609.34:.2f} mi"
                else:
                    dist_str = f"{dist / 1000:.2f} km"
            else:
                dist_str = "?"

            dur = a.get("duration_seconds")
            dur_str = f"{int(dur // 60)}:{int(dur % 60):02d}" if dur else "?"

            pace = _pace_str(a.get("avg_speed_ms"), units)
            elev = a.get("elevation_gain")
            elev_str = f"{elev:.0f}ft gain" if elev and units == "imperial" else (f"{elev:.0f}m gain" if elev else "")
            cadence = a.get("avg_cadence")
            cadence_str = f"cadence={cadence:.0f}spm" if cadence else ""
            te = ""
            if a.get("aerobic_te") is not None:
                te = f"aerobic TE={a['aerobic_te']:.1f}"
            if a.get("anaerobic_te") is not None:
                te += f" / anaerobic TE={a['anaerobic_te']:.1f}"

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
            lines.append(f"  [{a['date']} {a.get('time','')}] {name}: {details}")

            # Lap breakdown for recent activities
            laps = a.get("laps", [])
            if laps:
                lines.append(f"    Laps:")
                for lap in laps:
                    lap_dist = lap.get("distance_meters")
                    if lap_dist:
                        if units == "imperial":
                            lap_dist_str = f"{lap_dist / 1609.34:.2f} mi"
                        else:
                            lap_dist_str = f"{lap_dist / 1000:.2f} km"
                    else:
                        lap_dist_str = "?"
                    lap_dur = lap.get("duration_seconds")
                    lap_dur_str = f"{int(lap_dur // 60)}:{int(lap_dur % 60):02d}" if lap_dur else "?"
                    lap_pace = _pace_str(lap.get("avg_speed_ms"), units)
                    lap_elev = lap.get("elevation_gain")
                    lap_elev_str = f"+{lap_elev:.0f}ft" if lap_elev and units == "imperial" else (f"+{lap_elev:.0f}m" if lap_elev else "")
                    lap_details = " | ".join(filter(None, [
                        lap_dist_str,
                        lap_dur_str,
                        f"pace={lap_pace}",
                        f"avg HR={lap.get('avg_hr', '?')}",
                        f"max HR={lap.get('max_hr', '?')}",
                        lap_elev_str,
                    ]))
                    lines.append(f"      Lap {lap['lap']}: {lap_details}")

    return "\n".join(lines) if lines else "No Garmin data available."

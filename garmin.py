import os
from datetime import date, timedelta

import yaml
from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv(override=True)


def load_config():
    with open("config.yaml") as f:
        return yaml.safe_load(f)


def fetch_garmin_data() -> dict:
    config = load_config()
    g_cfg = config["garmin"]
    metrics = g_cfg["metrics"]
    lookback_days = g_cfg["lookback_days"]
    activities_count = g_cfg["activities_count"]

    client = Garmin(os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD"))
    client.login()

    today = date.today()
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
                    if val:
                        hrv_values.append({"date": d, "hrv": val})
                except Exception:
                    pass
            data["hrv"] = hrv_values
        except Exception as e:
            data["hrv"] = {"error": str(e)}

    if metrics.get("body_battery"):
        try:
            raw = client.get_body_battery(today.isoformat())
            # body battery returns a list of [timestamp, value] pairs
            start_val = raw[0][1] if raw else None
            data["body_battery"] = {"date": today.isoformat(), "start_value": start_val}
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
            data["stress"] = {"date": today.isoformat(), "average": avg}
        except Exception as e:
            data["stress"] = {"error": str(e)}

    if metrics.get("activities"):
        try:
            raw = client.get_activities(0, activities_count)
            activities = []
            for a in raw:
                activities.append({
                    "type": a.get("activityType", {}).get("typeKey"),
                    "date": a.get("startTimeLocal", "")[:10],
                    "duration_seconds": a.get("duration"),
                    "distance_meters": a.get("distance"),
                    "avg_hr": a.get("averageHR"),
                    "tss": a.get("trainingStressScore"),
                })
            data["activities"] = activities
        except Exception as e:
            data["activities"] = {"error": str(e)}

    return data


def format_garmin_data(data: dict, units: str = "imperial") -> str:
    lines = []

    if "sleep" in data:
        s = data["sleep"]
        if "error" not in s:
            total_hrs = round(s["total_seconds"] / 3600, 1) if s.get("total_seconds") else "?"
            lines.append(f"Sleep ({s['date']}): {total_hrs}h total, score={s.get('score', '?')}, "
                         f"deep={round(s['deep_seconds']/3600,1) if s.get('deep_seconds') else '?'}h, "
                         f"REM={round(s['rem_seconds']/3600,1) if s.get('rem_seconds') else '?'}h")

    if "hrv" in data and isinstance(data["hrv"], list):
        vals = [f"{h['date']}: {h['hrv']}" for h in data["hrv"]]
        lines.append("HRV (last night first): " + ", ".join(vals))

    if "body_battery" in data:
        bb = data["body_battery"]
        if "error" not in bb:
            lines.append(f"Body Battery: {bb.get('start_value', '?')} at start of day")

    if "steps" in data and isinstance(data["steps"], list):
        step_strs = [f"{s['date']}: {s['steps']}" for s in data["steps"]]
        lines.append("Steps: " + ", ".join(step_strs))

    if "stress" in data:
        st = data["stress"]
        if "error" not in st:
            lines.append(f"Stress avg: {st.get('average', '?')}")

    if "activities" in data and isinstance(data["activities"], list):
        lines.append("Recent activities:")
        for a in data["activities"]:
            dist = a.get("distance_meters")
            if dist:
                if units == "imperial":
                    dist_str = f"{round(dist / 1609.34, 2)} mi"
                else:
                    dist_str = f"{round(dist / 1000, 2)} km"
            else:
                dist_str = "?"
            dur = a.get("duration_seconds")
            dur_str = f"{int(dur//60)}min" if dur else "?"
            lines.append(f"  {a['date']} {a['type']}: {dist_str}, {dur_str}, HR={a.get('avg_hr','?')}, TSS={a.get('tss','?')}")

    return "\n".join(lines) if lines else "No Garmin data available."

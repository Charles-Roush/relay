"""
Fetches historical weather conditions for a given location and time using Open-Meteo.
Free API, no key required. Used to provide context for run effort interpretation.
"""
import urllib.request
import json
from datetime import datetime, timezone


def get_weather_for_activity(
    lat: float,
    lon: float,
    activity_datetime_utc: str,
    units: str = "imperial",
) -> dict | None:
    """
    Fetch hourly weather at the time and location of an activity.

    Args:
        lat: Latitude of activity start point.
        lon: Longitude of activity start point.
        activity_datetime_utc: ISO datetime string (UTC), e.g. "2026-06-08T14:30:00".
        units: "imperial" (°F, mph) or "metric" (°C, km/h).

    Returns a dict with keys: temp, humidity, wind_speed, conditions (str), or None on failure.
    """
    try:
        dt = datetime.fromisoformat(activity_datetime_utc.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        date_str = dt.date().isoformat()
        hour = dt.hour

        temp_unit = "fahrenheit" if units == "imperial" else "celsius"
        wind_unit = "mph" if units == "imperial" else "kmh"

        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={date_str}&end_date={date_str}"
            f"&hourly=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&temperature_unit={temp_unit}&wind_speed_unit={wind_unit}"
            f"&timezone=UTC"
        )

        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read())

        hourly = body.get("hourly", {})
        times = hourly.get("time", [])
        if not times:
            return None

        # Find the index matching our hour
        idx = None
        for i, t in enumerate(times):
            if t.endswith(f"T{hour:02d}:00"):
                idx = i
                break
        if idx is None:
            idx = min(hour, len(times) - 1)

        temp = hourly.get("temperature_2m", [None])[idx]
        humidity = hourly.get("relative_humidity_2m", [None])[idx]
        wind = hourly.get("wind_speed_10m", [None])[idx]
        wcode = hourly.get("weather_code", [None])[idx]

        conditions = _weather_code_desc(wcode)
        temp_unit_str = "°F" if units == "imperial" else "°C"
        wind_unit_str = "mph" if units == "imperial" else "km/h"

        return {
            "temp": temp,
            "temp_unit": temp_unit_str,
            "humidity": humidity,
            "wind_speed": wind,
            "wind_unit": wind_unit_str,
            "conditions": conditions,
            "summary": _weather_summary(temp, humidity, wind, conditions, units),
        }
    except Exception:
        return None


def _weather_code_desc(code: int | None) -> str:
    if code is None:
        return "unknown"
    if code == 0:
        return "clear sky"
    elif code in (1, 2, 3):
        return "partly cloudy"
    elif code in (45, 48):
        return "fog"
    elif code in (51, 53, 55):
        return "drizzle"
    elif code in (61, 63, 65, 80, 81, 82):
        return "rain"
    elif code in (71, 73, 75, 77, 85, 86):
        return "snow"
    elif code in (95, 96, 99):
        return "thunderstorm"
    return "mixed conditions"


def _weather_summary(
    temp: float | None,
    humidity: float | None,
    wind: float | None,
    conditions: str,
    units: str,
) -> str:
    parts = []
    if temp is not None:
        unit_str = "°F" if units == "imperial" else "°C"
        parts.append(f"{temp:.0f}{unit_str}")
    if humidity is not None:
        flag = " (high humidity)" if humidity > 70 else ""
        parts.append(f"{humidity:.0f}% humidity{flag}")
    if wind is not None and wind > (10 if units == "imperial" else 16):
        unit_str = "mph" if units == "imperial" else "km/h"
        parts.append(f"{wind:.0f}{unit_str} wind")
    if conditions and conditions != "clear sky":
        parts.append(conditions)
    return ", ".join(parts) if parts else "conditions unavailable"

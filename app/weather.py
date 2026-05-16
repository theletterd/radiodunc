from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_WEATHER_CODE_LABELS = {
    0: "clear skies",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "heavy freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "heavy freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "heavy rain showers",
    82: "violent rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "severe thunderstorms with hail",
}


def fetch_weather_summary(location: str) -> str | None:
    geocode_url = (
        "https://geocoding-api.open-meteo.com/v1/search?"
        f"name={quote_plus(location)}&count=1&language=en&format=json"
    )
    try:
        with urllib.request.urlopen(geocode_url, timeout=15) as response:  # noqa: S310
            geocode_data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        logger.warning("Weather geocoding failed for location=%s", location)
        return None

    results = geocode_data.get("results")
    if not isinstance(results, list) or not results:
        return None
    top_result = results[0]
    if not isinstance(top_result, dict):
        return None
    latitude = top_result.get("latitude")
    longitude = top_result.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None

    weather_url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={latitude}&longitude={longitude}&current=temperature_2m,weather_code,wind_speed_10m"
        "&temperature_unit=fahrenheit&wind_speed_unit=mph"
    )
    try:
        with urllib.request.urlopen(weather_url, timeout=20) as response:  # noqa: S310
            weather_data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        logger.warning("Weather lookup failed for location=%s", location)
        return None

    current = weather_data.get("current")
    if not isinstance(current, dict):
        return None
    temp_f = current.get("temperature_2m")
    code = current.get("weather_code")
    wind = current.get("wind_speed_10m")
    if not isinstance(temp_f, (int, float)) or not isinstance(code, (int, float)):
        return None
    temp_c = (float(temp_f) - 32) * 5 / 9
    condition = _WEATHER_CODE_LABELS.get(int(code), "mixed conditions")
    wind_text = f", winds around {round(wind)} mph" if isinstance(wind, (int, float)) else ""
    return (
        f"Current conditions in {location}: {condition}, about {round(temp_f)}°F ({round(temp_c)}°C)"
        f"{wind_text}."
    )

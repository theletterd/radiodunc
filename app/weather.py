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


def fetch_weather_summary(
    location: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str | None:
    logger.info("Starting weather lookup for requested location=%s", location)

    if latitude is not None and longitude is not None:
        logger.info(
            "Using pinned coordinates for location=%s lat=%s lon=%s (geocoding skipped)",
            location, latitude, longitude,
        )
    else:
        geocode_url = (
            "https://geocoding-api.open-meteo.com/v1/search?"
            f"name={quote_plus(location)}&count=1&language=en&format=json"
        )
        logger.debug("Weather geocoding URL built for location=%s: %s", location, geocode_url)
        try:
            with urllib.request.urlopen(geocode_url, timeout=15) as response:  # noqa: S310
                geocode_data = json.loads(response.read().decode("utf-8"))
                logger.debug("Weather geocoding response received for location=%s", location)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            logger.warning("Weather geocoding failed for location=%s", location)
            return None

        results = geocode_data.get("results")
        if not isinstance(results, list) or not results:
            logger.warning("Weather geocoding returned no results for location=%s", location)
            return None
        logger.info("Weather geocoding returned %d result(s) for location=%s", len(results), location)
        top_result = results[0]
        if not isinstance(top_result, dict):
            logger.warning("Weather geocoding top result was not a dict for location=%s", location)
            return None
        matched_name = top_result.get("name")
        matched_admin = top_result.get("admin1")
        matched_country = top_result.get("country")
        latitude = top_result.get("latitude")
        longitude = top_result.get("longitude")
        if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
            logger.warning("Weather geocoding missing coordinates for location=%s", location)
            return None
        logger.info(
            "Weather geocoding resolved location=%s to match=%s, admin=%s, country=%s at lat=%s lon=%s",
            location, matched_name, matched_admin, matched_country, latitude, longitude,
        )

    weather_url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={latitude}&longitude={longitude}&current=temperature_2m,weather_code,wind_speed_10m"
        "&wind_speed_unit=mph"
    )
    logger.debug(
        "Weather forecast URL built for location=%s (lat=%s lon=%s): %s",
        location,
        latitude,
        longitude,
        weather_url,
    )
    try:
        with urllib.request.urlopen(weather_url, timeout=20) as response:  # noqa: S310
            weather_data = json.loads(response.read().decode("utf-8"))
            logger.debug("Weather forecast response received for location=%s", location)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        logger.warning("Weather lookup failed for location=%s", location)
        return None

    current = weather_data.get("current")
    if not isinstance(current, dict):
        logger.warning("Weather lookup returned invalid current block for location=%s", location)
        return None
    temp_c = current.get("temperature_2m")
    code = current.get("weather_code")
    wind = current.get("wind_speed_10m")
    if not isinstance(temp_c, (int, float)) or not isinstance(code, (int, float)):
        logger.warning(
            "Weather lookup missing required metrics for location=%s temp=%r code=%r",
            location,
            temp_c,
            code,
        )
        return None
    condition = _WEATHER_CODE_LABELS.get(int(code), "mixed conditions")
    wind_text = f", winds around {round(wind)} mph" if isinstance(wind, (int, float)) else ""
    summary = (
        f"Current conditions in {location}: {condition}, about {round(temp_c)}°C{wind_text}."
    )
    logger.info(
        "Weather lookup complete for location=%s condition=%s temp_c=%s wind_mph=%s",
        location,
        condition,
        round(temp_c),
        round(wind) if isinstance(wind, (int, float)) else None,
    )
    logger.debug("Weather summary generated for location=%s: %s", location, summary)
    return summary

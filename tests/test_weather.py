"""Tests for the weather lookup + 30-min in-memory cache."""

import json
from unittest.mock import patch

import pytest

import app.weather as _weather_mod
from app.weather import fetch_weather_summary


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _forecast(temp_c: float = 8.0, code: int = 3, wind: float = 3.0) -> bytes:
    return json.dumps({
        "current": {"temperature_2m": temp_c, "weather_code": code, "wind_speed_10m": wind},
    }).encode("utf-8")


@pytest.fixture(autouse=True)
def _reset_weather_cache():
    _weather_mod._summary_cache.clear()
    yield
    _weather_mod._summary_cache.clear()


def test_fetch_weather_summary_uses_pinned_coords_and_returns_summary():
    with patch("app.weather.urllib.request.urlopen", return_value=_FakeResponse(_forecast())) as mock_open:
        result = fetch_weather_summary("Happy Valley, OR", latitude=45.4468, longitude=-122.5329)
    assert result is not None
    assert "Happy Valley, OR" in result
    assert "8°C" in result
    # Pinned coords → exactly one urlopen call (geocoding skipped).
    assert mock_open.call_count == 1


def test_fetch_weather_summary_caches_successful_lookup():
    with patch("app.weather.urllib.request.urlopen", return_value=_FakeResponse(_forecast())) as mock_open:
        first = fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
        second = fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
    assert second == first
    assert mock_open.call_count == 1  # second served from cache


def test_fetch_weather_summary_does_not_cache_failures():
    import urllib.error
    with patch("app.weather.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")) as mock_open:
        fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
        fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
    assert mock_open.call_count == 2  # both calls retry


def test_fetch_weather_summary_cache_keyed_per_location():
    with patch("app.weather.urllib.request.urlopen", return_value=_FakeResponse(_forecast())) as mock_open:
        fetch_weather_summary("Loc A", latitude=10.0, longitude=20.0)
        fetch_weather_summary("Loc B", latitude=11.0, longitude=21.0)
    assert mock_open.call_count == 2


def test_fetch_weather_summary_cache_expires():
    import time as _time_mod
    with patch("app.weather.urllib.request.urlopen", return_value=_FakeResponse(_forecast())):
        fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
    # Simulate the cache being 31 min old.
    key = ("Pinned, OR", 45.0, -122.0)
    cached_at, summary = _weather_mod._summary_cache[key]
    _weather_mod._summary_cache[key] = (cached_at - (31 * 60), summary)

    with patch("app.weather.urllib.request.urlopen", return_value=_FakeResponse(_forecast(temp_c=99))) as mock_open:
        result = fetch_weather_summary("Pinned, OR", latitude=45.0, longitude=-122.0)
    assert "99°C" in result  # refreshed
    assert mock_open.call_count == 1

import pytest

from app.config import AppConfig, StationPreset, _validate_and_format_config


def test_validate_and_format_config_rejects_blank_station_preset_format():
    raw = {
        "music_folder": "~/Music",
        "station_generation_count": 3,
        "alerts": {
            "weather_location": "Seattle, WA",
            "local_time_zone": "America/Los_Angeles",
            "news": {"enabled": True, "categories": ["local"], "briefing_minutes": 15},
        },
        "station_presets": [
            {
                "format": "   ",
                "tagline": "Tagline",
                "dj_name_prefix": "DJ",
                "dj_style": "warm",
            }
        ],
    }

    with pytest.raises(ValueError, match=r"Invalid config in radio_config\.json: station_presets\.0\.format"):
        _validate_and_format_config("radio_config.json", raw)


def test_station_preset_voice_hint_is_normalized_to_none_when_blank():
    preset = StationPreset(
        format="Indie Discovery",
        tagline="Fresh cuts",
        dj_name_prefix="DJ",
        dj_style="warm",
        voice_hint="   ",
    )

    assert preset.voice_hint is None


def test_app_config_requires_at_least_one_station_preset():
    with pytest.raises(ValueError, match="must include at least one preset"):
        AppConfig(station_presets=[])

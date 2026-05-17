import pytest

from app.config import AppConfig, StationConfig, _validate_and_format_config, load_config, save_config


def test_validate_and_format_config_rejects_blank_station_format():
    raw = {
        "music_folder": "~/Music",
        "alerts": {
            "weather_location": "Seattle, WA",
            "local_time_zone": "America/Los_Angeles",
            "news": {"enabled": True, "categories": ["local"], "briefing_minutes": 15},
        },
        "station": {
            "name": "Test",
            "tagline": "Test tag",
            "format": "   ",
            "dj_name": "DJ",
            "dj_style": "warm",
        },
    }

    with pytest.raises(ValueError, match=r"Invalid config in radio_config\.json: station\.format"):
        _validate_and_format_config("radio_config.json", raw)


def test_station_voice_is_normalized_to_none_when_blank():
    station = StationConfig(voice="   ")
    assert station.voice is None


def test_app_config_defaults_include_station():
    cfg = AppConfig()
    assert cfg.station.name
    assert cfg.station.dj_name


def test_load_config_reads_openai_api_key_from_dotenv(tmp_path, monkeypatch):
    (tmp_path / "radio_config.json").write_text('{"music_folder":"~/Music"}', encoding="utf-8")
    (tmp_path / ".env").write_text('OPENAI_API_KEY="sk-test-123"\n', encoding="utf-8")
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "radio_config.json")
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", tmp_path / "example-radio_config.json")
    monkeypatch.setattr("app.config.DOTENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    loaded = load_config()
    assert loaded.openai_api_key == "sk-test-123"


def test_save_config_does_not_persist_openai_api_key(tmp_path, monkeypatch):
    config_path = tmp_path / "radio_config.json"
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    cfg = AppConfig(openai_api_key="sk-should-not-write")
    save_config(cfg)
    raw = config_path.read_text(encoding="utf-8")
    assert "openai_api_key" not in raw

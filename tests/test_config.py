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


# ── save_config atomicity + load_config resilience ──────────────────────────

def test_save_config_writes_atomically_via_tempfile(tmp_path, monkeypatch):
    """Bytes either look fully-valid or stay as the previous version — never
    zero-byte mid-write. Verified by checking the temp file lifecycle."""
    from app.config import AppConfig, StationConfig, save_config
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "radio_config.json")
    cfg = AppConfig(station=StationConfig(name="Atomic FM"))
    save_config(cfg)
    final = tmp_path / "radio_config.json"
    assert final.exists()
    # Temp file should be gone after the rename.
    assert not (tmp_path / "radio_config.json.tmp").exists()
    # File is valid JSON.
    import json as _json
    assert _json.loads(final.read_text())["station"]["name"] == "Atomic FM"


def test_load_config_recovers_from_empty_file(tmp_path, monkeypatch, caplog):
    """An empty (zero-byte) config — possible from a pre-fix non-atomic crash —
    should not 500. It should rebootstrap from the example template."""
    import logging as _logging
    from app.config import AppConfig, load_config
    config_path = tmp_path / "radio_config.json"
    example_path = tmp_path / "example-radio_config.json"
    config_path.write_text("")  # zero-byte
    example_path.write_text(
        '{"station": {"name": "Bootstrap FM", "format": "Eclectic"}}'
    )
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", example_path)
    caplog.set_level(_logging.WARNING)
    cfg = load_config()
    assert cfg.station.name == "Bootstrap FM"
    # The file is repopulated so subsequent reads succeed.
    assert config_path.read_text().strip() != ""


def test_load_config_recovers_from_malformed_file(tmp_path, monkeypatch):
    from app.config import load_config
    config_path = tmp_path / "radio_config.json"
    example_path = tmp_path / "example-radio_config.json"
    config_path.write_text("{not valid json")
    example_path.write_text('{"station": {"name": "Recovery FM", "format": "Eclectic"}}')
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", example_path)
    cfg = load_config()
    assert cfg.station.name == "Recovery FM"


def test_load_config_whitespace_only_treated_as_empty(tmp_path, monkeypatch):
    from app.config import load_config
    config_path = tmp_path / "radio_config.json"
    example_path = tmp_path / "example-radio_config.json"
    config_path.write_text("   \n\n  \t")
    example_path.write_text('{"station": {"name": "Whitespace FM", "format": "Eclectic"}}')
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", example_path)
    assert load_config().station.name == "Whitespace FM"

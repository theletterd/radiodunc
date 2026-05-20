import json
import uuid

import pytest

from app.config import AppConfig, DJ, Show, StationConfig, _validate_and_format_config, load_config, save_config


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


# ── DJ model ─────────────────────────────────────────────────────────────────

def test_dj_constructs_with_required_fields():
    dj = DJ(id=str(uuid.uuid4()), name="Saturday Sam", personality="laid-back and punny")
    assert dj.name == "Saturday Sam"
    assert dj.voice is None
    assert dj.voice_instructions is None
    assert dj.prompt_template is None


def test_dj_strips_name_whitespace():
    dj = DJ(id=str(uuid.uuid4()), name="  Sam  ", personality="punny")
    assert dj.name == "Sam"


def test_dj_rejects_blank_name():
    with pytest.raises(Exception, match="blank"):
        DJ(id=str(uuid.uuid4()), name="   ", personality="punny")


def test_dj_normalizes_blank_voice_to_none():
    dj = DJ(id=str(uuid.uuid4()), name="Sam", personality="punny", voice="  ")
    assert dj.voice is None


# ── Show model ────────────────────────────────────────────────────────────────

def test_show_constructs_with_minimal_fields():
    show = Show(id=str(uuid.uuid4()))
    assert show.name is None
    assert show.dj_id is None
    assert show.shifts == []


def test_show_blank_name_normalized_to_none():
    show = Show(id=str(uuid.uuid4()), name="  ")
    assert show.name is None


def test_show_accepts_name_and_dj_id():
    dj_id = str(uuid.uuid4())
    show = Show(id=str(uuid.uuid4()), name="Late Night Sessions", dj_id=dj_id)
    assert show.name == "Late Night Sessions"
    assert show.dj_id == dj_id


# ── Migration: dj_roster → djs + shows ───────────────────────────────────────

def _station_with_persona(**persona_fields):
    """Build a StationConfig dict with a single dj_roster entry."""
    persona = {"name": "Saturday Sam", "personality": "laid-back and punny", **persona_fields}
    return StationConfig.model_validate({"dj_roster": [persona]})


def test_migration_creates_one_dj_and_one_show():
    station = _station_with_persona()
    assert len(station.djs) == 1
    assert len(station.shows) == 1
    # dj_roster is kept in-memory so the legacy resolver (pick_active_persona)
    # keeps working until the slice-2 resolver swap. save_config zeroes it out
    # in the written JSON so the next load finds djs/shows directly.
    assert len(station.dj_roster) == 1


def test_migration_dj_fields_match_persona():
    station = _station_with_persona(
        voice="onyx",
        voice_instructions="Smooth and cool.",
        prompt_template="Play it {dj_name}.",
    )
    dj = station.djs[0]
    assert dj.name == "Saturday Sam"
    assert dj.personality == "laid-back and punny"
    assert dj.voice == "onyx"
    assert dj.voice_instructions == "Smooth and cool."
    assert dj.prompt_template == "Play it {dj_name}."


def test_migration_show_links_to_dj_via_id():
    station = _station_with_persona()
    dj = station.djs[0]
    show = station.shows[0]
    assert show.dj_id == dj.id
    assert show.name is None  # legacy personas had no show name


def test_migration_show_inherits_shifts():
    station = StationConfig.model_validate({
        "dj_roster": [{
            "name": "Night Owl",
            "personality": "dark and jazzy",
            "shifts": [
                {"day": "friday", "start_hour": 22, "end_hour": 2},
                {"day": "saturday", "start_hour": 22, "end_hour": 2},
            ],
        }]
    })
    shifts = station.shows[0].shifts
    assert len(shifts) == 2
    assert shifts[0].day == "friday"
    assert shifts[1].day == "saturday"


def test_migration_handles_legacy_persona_day_fields():
    """DJPersona.migrate_legacy_fields (days/start_hour/end_hour → shifts) should
    run before our DJ-Show migration so the Show ends up with proper DJShift objects."""
    station = StationConfig.model_validate({
        "dj_roster": [{
            "name": "Weekender",
            "personality": "upbeat",
            "days": ["saturday", "sunday"],
            "start_hour": 10,
            "end_hour": 14,
        }]
    })
    shifts = station.shows[0].shifts
    assert len(shifts) == 2
    assert {s.day for s in shifts} == {"saturday", "sunday"}
    assert all(s.start_hour == 10 and s.end_hour == 14 for s in shifts)


def test_migration_is_idempotent_when_djs_already_populated():
    """If djs is already set, don't re-migrate the roster (safety net)."""
    dj_id = str(uuid.uuid4())
    show_id = str(uuid.uuid4())
    station = StationConfig.model_validate({
        "djs": [{"id": dj_id, "name": "Existing DJ", "personality": "smooth"}],
        "shows": [{"id": show_id, "dj_id": dj_id, "shifts": []}],
        "dj_roster": [{"name": "Legacy Sam", "personality": "punny"}],
    })
    # The djs/shows from the dict should survive untouched.
    assert len(station.djs) == 1
    assert station.djs[0].name == "Existing DJ"
    # dj_roster was NOT cleared — the condition skipped migration.
    assert len(station.dj_roster) == 1


def test_migration_no_op_when_roster_empty():
    station = StationConfig.model_validate({"dj_roster": []})
    assert station.djs == []
    assert station.shows == []


def test_migration_multiple_personas_become_multiple_djs_and_shows():
    station = StationConfig.model_validate({
        "dj_roster": [
            {"name": "Sam", "personality": "punny"},
            {"name": "Jess", "personality": "edgy"},
        ]
    })
    assert len(station.djs) == 2
    assert len(station.shows) == 2
    assert {dj.name for dj in station.djs} == {"Sam", "Jess"}
    # Each show points to a distinct DJ.
    dj_ids = {dj.id for dj in station.djs}
    show_dj_ids = {show.dj_id for show in station.shows}
    assert dj_ids == show_dj_ids


def test_migration_generates_valid_uuid4s():
    station = _station_with_persona()
    dj = station.djs[0]
    show = station.shows[0]
    # Should not raise — both are valid UUIDs.
    uuid.UUID(dj.id, version=4)
    uuid.UUID(show.id, version=4)


# ── Round-trip: save → load preserves djs + shows ────────────────────────────

def test_round_trip_djs_and_shows_survive_save_load(tmp_path, monkeypatch):
    """djs/shows written by save_config should survive a load_config round-trip."""
    config_path = tmp_path / "radio_config.json"
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", tmp_path / "example-radio_config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    dj_id = str(uuid.uuid4())
    show_id = str(uuid.uuid4())
    cfg = AppConfig(station=StationConfig(
        djs=[DJ(id=dj_id, name="Neon Kix", personality="hyped and neon")],
        shows=[Show(id=show_id, name="The Neon Hour", dj_id=dj_id,
                    shifts=[])],
    ))
    save_config(cfg)

    loaded = load_config()
    assert len(loaded.station.djs) == 1
    assert loaded.station.djs[0].name == "Neon Kix"
    assert loaded.station.djs[0].id == dj_id
    assert len(loaded.station.shows) == 1
    assert loaded.station.shows[0].name == "The Neon Hour"
    assert loaded.station.shows[0].dj_id == dj_id


def test_migrated_config_saves_without_dj_roster(tmp_path, monkeypatch):
    """After migration, save_config should write dj_roster: [] (not the old entries)."""
    config_path = tmp_path / "radio_config.json"
    monkeypatch.setattr("app.config.CONFIG_PATH", config_path)
    monkeypatch.setattr("app.config.EXAMPLE_CONFIG_PATH", tmp_path / "example-radio_config.json")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Write a legacy config with dj_roster populated.
    legacy = {
        "station": {
            "dj_roster": [{"name": "Old Sam", "personality": "classic"}]
        }
    }
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = load_config()
    # Migration should have run.
    assert len(loaded.station.djs) == 1

    save_config(loaded)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    # The legacy list should be empty in the saved file.
    assert saved["station"]["dj_roster"] == []
    # The new fields should be present.
    assert len(saved["station"]["djs"]) == 1
    assert saved["station"]["djs"][0]["name"] == "Old Sam"

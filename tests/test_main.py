from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.database import Base
from app.main import (
    get_player_state,
    generate_stations_endpoint,
    set_favorite_station,
    generate_station_queue,
    get_config,
    healthcheck,
    list_stations,
    list_tracks,
    scan_library_endpoint,
    update_config,
    update_player_state,
    generate_station_dj_script,
    synthesize_station_dj_clip,
    player_play,
    player_next,
    player_stop,
    player_current_media,
)
from app.models import DJClip, FavoriteStation, PlayerState, RecentStation, Station, Track
from app.schemas import DJClipSynthesizeRequest, DJScriptGenerateRequest, FavoriteStationRequest, LibraryScanRequest, PlayerPlayRequest, PlayerStateUpdateRequest, QueueGenerateRequest, StationGenerateRequest


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def test_healthcheck_returns_ok():
    assert healthcheck() == {"status": "ok"}


def test_ui_index_file_exists():
    html = Path("app/ui/index.html").read_text(encoding="utf-8")
    assert "RadioDunc" in html




def test_get_and_put_config_round_trip(monkeypatch):
    cfg = AppConfig(music_folder="/tmp/music")
    saved = {}

    monkeypatch.setattr("app.main.load_config", lambda: cfg)

    def fake_save_config(incoming: AppConfig):
        saved["config"] = incoming

    monkeypatch.setattr("app.main.save_config", fake_save_config)

    returned = get_config()
    assert returned.music_folder == "/tmp/music"

    updated = cfg.model_copy(update={"music_folder": "/new/music"})
    result = update_config(updated)

    assert result.music_folder == "/new/music"
    assert saved["config"].music_folder == "/new/music"


def test_scan_library_endpoint_uses_payload_folder(monkeypatch):
    db = _make_db_session()

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))

    def fake_scan_library(folder_path, _db):
        assert folder_path == "/explicit"
        return {"scanned": 1, "imported": 1, "skipped_duplicates": 0, "errors": []}

    monkeypatch.setattr("app.main.scan_library", fake_scan_library)

    response = scan_library_endpoint(LibraryScanRequest(folder_path="/explicit"), db)

    assert response["folder_path"] == "/explicit"


def test_scan_library_endpoint_maps_file_not_found_to_400(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))
    monkeypatch.setattr("app.main.scan_library", lambda _folder_path, _db: (_ for _ in ()).throw(FileNotFoundError("missing folder")))

    with pytest.raises(HTTPException) as exc:
        scan_library_endpoint(LibraryScanRequest(), db)

    assert exc.value.status_code == 400
    assert exc.value.detail == "missing folder"


def test_scan_library_endpoint_maps_unexpected_error_to_500(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/fallback"))
    monkeypatch.setattr("app.main.scan_library", lambda _folder_path, _db: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(HTTPException) as exc:
        scan_library_endpoint(LibraryScanRequest(), db)

    assert exc.value.status_code == 500
    assert exc.value.detail == "Failed to scan library: boom"


def test_tracks_endpoint_orders_results():
    db = _make_db_session()
    db.add_all(
        [
            Track(file_path="/m/2.mp3", title="Song B", artist="B", album="Z"),
            Track(file_path="/m/1.mp3", title="Song A", artist="A", album="Y"),
        ]
    )
    db.commit()

    results = list_tracks(db)

    assert [row.artist for row in results] == ["A", "B"]


def test_generate_stations_endpoint_success_and_value_error(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())

    monkeypatch.setattr(
        "app.main.generate_stations",
        lambda _db, _config, count: {"generated": count, "library_summary": {"track_count": 10}, "seed": None},
    )
    ok = generate_stations_endpoint(StationGenerateRequest(count=2), db)
    assert ok["generated"] == 2

    def raises_value_error(_db, _config, _count):
        raise ValueError("No tracks found")

    monkeypatch.setattr("app.main.generate_stations", raises_value_error)
    with pytest.raises(HTTPException) as exc:
        generate_stations_endpoint(StationGenerateRequest(count=2), db)
    assert exc.value.status_code == 400


def test_stations_endpoint_orders_by_created_at_desc():
    db = _make_db_session()
    now = datetime.utcnow()
    db.add_all(
        [
            Station(name="Older", created_at=now - timedelta(minutes=5)),
            Station(name="Newer", created_at=now),
        ]
    )
    db.commit()

    results = list_stations(db)

    assert [row.name for row in results] == ["Newer", "Older"]


def test_generate_station_queue_endpoint_maps_value_error(monkeypatch):
    db = _make_db_session()

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())

    def fake_queue(**_kwargs):
        return {
            "station_id": 1,
            "station_name": "X FM",
            "queue_size": 1,
            "seed": None,
            "artist_repeat_window": 2,
            "used_station_alignment": False,
            "tracks": [],
        }

    monkeypatch.setattr("app.main.build_station_queue", fake_queue)
    ok = generate_station_queue(1, QueueGenerateRequest(size=1), db)
    assert ok["station_id"] == 1

    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(HTTPException) as exc:
        generate_station_queue(1, QueueGenerateRequest(size=1), db)
    assert exc.value.status_code == 400


def test_player_state_defaults_and_updates():
    db = _make_db_session()
    station = Station(name="Solo FM")
    db.add(station)
    db.commit()
    db.refresh(station)

    initial = get_player_state(db)
    assert initial.station_id is None
    assert initial.is_playing is False
    assert initial.volume == 80
    assert initial.favorites == []

    updated = update_player_state(PlayerStateUpdateRequest(station_id=station.id, is_playing=True, volume=65), db)
    assert updated.station_id == station.id
    assert updated.is_playing is True
    assert updated.volume == 65
    assert updated.station is not None
    assert updated.station.name == "Solo FM"
    assert updated.recent_station_ids == [station.id]

    assert db.query(RecentStation).count() == 1


def test_set_favorite_station_adds_and_removes():
    db = _make_db_session()
    station = Station(name="Fav FM")
    db.add(station)
    db.commit()
    db.refresh(station)

    set_favorite_station(station.id, FavoriteStationRequest(favorite=True), db)
    assert db.query(FavoriteStation).count() == 1

    set_favorite_station(station.id, FavoriteStationRequest(favorite=False), db)
    assert db.query(FavoriteStation).count() == 0


def test_generate_station_dj_script_happy_path_and_not_found():
    db = _make_db_session()
    station = Station(name="Night Drive", tagline="Smooth roads.", dj_name="DJ Nova", dj_style="laid-back")
    track1 = Track(file_path="/m/a.mp3", title="A Song", artist="A Artist")
    track2 = Track(file_path="/m/b.mp3", title="B Song", artist="B Artist")
    db.add_all([station, track1, track2])
    db.commit()
    db.refresh(station)
    db.refresh(track1)
    db.refresh(track2)

    result = generate_station_dj_script(
        station.id,
        DJScriptGenerateRequest(previous_track_id=track1.id, next_track_id=track2.id, include_weather=True, max_sentences=3),
        db,
    )
    assert result.station_id == station.id
    assert len(result.sentences) == 3
    assert "Night Drive" in result.script_text

    with pytest.raises(HTTPException) as exc:
        generate_station_dj_script(9999, DJScriptGenerateRequest(), db)
    assert exc.value.status_code == 404


def test_synthesize_station_dj_clip_creates_and_caches():
    db = _make_db_session()
    station = Station(name="Clip FM")
    db.add(station)
    db.commit()
    db.refresh(station)

    payload = DJClipSynthesizeRequest(script_text="Hello from Clip FM", voice="default")
    first = synthesize_station_dj_clip(station.id, payload, db)
    second = synthesize_station_dj_clip(station.id, payload, db)

    assert first.cached is False
    assert second.cached is True
    assert first.audio_path == second.audio_path
    assert db.query(DJClip).count() == 1


def test_player_play_next_stop_flow(monkeypatch):
    db = _make_db_session()
    station = Station(name="Flow FM", dj_name="DJ Flow")
    track1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    track2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([station, track1, track2])
    db.commit()
    db.refresh(station)
    db.refresh(track1)
    db.refresh(track2)

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())
    monkeypatch.setattr(
        "app.main.build_station_queue",
        lambda **_kwargs: {"tracks": [track1, track2]},
    )

    played = player_play(PlayerPlayRequest(station_id=station.id, queue_size=2), db)
    assert played.state.is_playing is True
    assert played.state.queue_depth == 4
    assert played.state.current_track is not None
    assert played.state.current_track.title == "One"

    advanced = player_next(db)
    assert advanced.state.now_playing_type == "dj"

    stopped = player_stop(db)
    assert stopped.state.is_playing is False


def test_player_current_media_returns_track_file(tmp_path, monkeypatch):
    db = _make_db_session()
    station = Station(name="Media FM", dj_name="DJ Media")
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake-audio")
    track = Track(file_path=str(audio), title="Song", artist="Artist")
    db.add_all([station, track])
    db.commit()
    db.refresh(station)
    db.refresh(track)

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder=str(tmp_path)))
    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: {"tracks": [track]})

    player_play(PlayerPlayRequest(station_id=station.id, queue_size=1), db)
    response = player_current_media(db)

    assert response.path == str(audio)


def test_player_current_media_serves_dj_clip():
    db = _make_db_session()
    state = PlayerState(current_station_id=None, is_playing=True, queue_json='[{"type":"dj","label":"break","script_text":"DJ break"}]', queue_index=0)
    db.add(state)
    db.commit()

    response = player_current_media(db)
    assert response.path.endswith('.wav')


def test_player_current_media_rejects_path_outside_allowed_root(monkeypatch):
    db = _make_db_session()
    station = Station(name="Unsafe FM", dj_name="DJ Unsafe")
    track = Track(file_path="/tmp/not-allowed.mp3", title="Nope", artist="X")
    db.add_all([station, track])
    db.commit()
    db.refresh(station)

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder="/workspace/radiodunc/tests"))
    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: {"tracks": [track]})

    player_play(PlayerPlayRequest(station_id=station.id, queue_size=1), db)
    with pytest.raises(HTTPException) as exc:
        player_current_media(db)
    assert exc.value.status_code == 403

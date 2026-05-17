import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig, StationConfig
from app.database import Base
from app.main import (
    get_player_state,
    get_config,
    healthcheck,
    root,
    list_tracks,
    scan_library_endpoint,
    search_library,
    queue_inject,
    update_config,
    update_player_state,
    generate_dj_script_endpoint,
    synthesize_dj_clip,
    player_play,
    player_next,
    player_stop,
    player_queue,
    delete_queue_item,
    media_track,
    media_dj_clip,
)
from app.models import DJClip, PlayerState, Track
from app.schemas import (
    DJClipSynthesizeRequest,
    DJScriptGenerateRequest,
    DJScriptResponse,
    LibraryScanRequest,
    PlayerPlayRequest,
    PlayerStateUpdateRequest,
    QueueInjectRequest,
)


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def test_healthcheck_returns_ok():
    assert healthcheck() == {"status": "ok"}


def test_root_redirects_to_ui():
    response = root()
    assert response.status_code == 307
    assert response.headers.get("location") == "/ui/"


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


def test_player_state_defaults_and_updates(monkeypatch):
    db = _make_db_session()

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Solo FM")),
    )

    initial = get_player_state(db)
    assert initial.is_playing is False
    assert initial.volume == 80
    assert initial.station is not None
    assert initial.station.name == "Solo FM"

    updated = update_player_state(PlayerStateUpdateRequest(is_playing=True, volume=65), db)
    assert updated.is_playing is True
    assert updated.volume == 65
    assert updated.station.name == "Solo FM"


def test_dj_script_endpoint_happy_path(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Night Drive", tagline="Smooth roads.", dj_name="DJ Nova", dj_style="laid-back")),
    )
    track1 = Track(file_path="/m/a.mp3", title="A Song", artist="A Artist")
    track2 = Track(file_path="/m/b.mp3", title="B Song", artist="B Artist")
    db.add_all([track1, track2])
    db.commit()
    db.refresh(track1)
    db.refresh(track2)

    result = generate_dj_script_endpoint(
        DJScriptGenerateRequest(previous_track_id=track1.id, next_track_id=track2.id, include_weather=True, max_sentences=3),
        db,
    )
    assert len(result.sentences) == 3
    assert "Night Drive" in result.script_text


def test_synthesize_dj_clip_creates_and_caches(monkeypatch):
    db = _make_db_session()
    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(station=StationConfig(name="Clip FM")))

    payload = DJClipSynthesizeRequest(script_text="Hello from Clip FM", voice="default")
    first = synthesize_dj_clip(payload, db)
    second = synthesize_dj_clip(payload, db)

    assert first.cached is False
    assert second.cached is True
    assert first.audio_path == second.audio_path
    assert db.query(DJClip).count() == 1


def _fake_dj_next(db, station_name, dj_name, tmp_path, monkeypatch):
    """Helper: wire up DJ script + clip mocks needed by player_next."""
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    clip = DJClip(
        script_text="Welcome back.",
        audio_path=str(wav),
        voice="default",
        script_hash="testhash001",
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)
    monkeypatch.setattr(
        "app.main.generate_dj_script",
        lambda *_a, **_k: DJScriptResponse(
            station_name=station_name,
            dj_name=dj_name or "DJ",
            sentences=["Welcome back."],
            script_text="Welcome back.",
        ),
    )
    monkeypatch.setattr(
        "app.main.get_or_create_dj_clip",
        lambda *_a, **_k: (clip, str(wav), False),
    )
    return clip


def test_player_play_next_stop_flow(monkeypatch, tmp_path):
    db = _make_db_session()
    track1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    track2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([track1, track2])
    db.commit()
    db.refresh(track1)
    db.refresh(track2)

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Flow FM", dj_name="DJ Flow")),
    )
    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: {"tracks": [track1, track2]})
    _fake_dj_next(db, "Flow FM", "DJ Flow", tmp_path, monkeypatch)

    played = player_play(PlayerPlayRequest(queue_size=2), db)
    assert played.state.is_playing is True
    assert played.state.queue_depth == 2
    assert played.state.current_track is not None
    assert played.state.current_track.title == "One"

    nexted = player_next(None, db)
    assert nexted.current_track_url == f"/media/track/{track2.id}"
    assert nexted.dj_clip_url == "/media/dj-clip/testhash001"

    stopped = player_stop(db)
    assert stopped.state.is_playing is False


def test_player_next_returns_urls_and_advances_queue(monkeypatch, tmp_path):
    db = _make_db_session()
    track1 = Track(file_path="/m/a.mp3", title="Alpha", artist="A", duration_seconds=210.0)
    track2 = Track(file_path="/m/b.mp3", title="Beta", artist="B", duration_seconds=180.0)
    track3 = Track(file_path="/m/c.mp3", title="Gamma", artist="C", duration_seconds=200.0)
    db.add_all([track1, track2, track3])
    db.commit()
    db.refresh(track1)
    db.refresh(track2)
    db.refresh(track3)

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Next FM", dj_name="DJ Next")),
    )
    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: {"tracks": [track1, track2, track3]})
    _fake_dj_next(db, "Next FM", "DJ Next", tmp_path, monkeypatch)

    player_play(PlayerPlayRequest(queue_size=3), db)
    result = player_next(None, db)

    assert result.current_track_url == f"/media/track/{track2.id}"
    assert result.dj_clip_url == "/media/dj-clip/testhash001"
    assert result.next_track_url == f"/media/track/{track3.id}"
    assert result.next_track_metadata is not None
    assert result.next_track_metadata.title == "Gamma"
    assert result.dj_script == "Welcome back."

    state = db.query(PlayerState).first()
    assert state.queue_index == 1
    assert state.current_track_id == track2.id


def test_player_next_at_end_of_queue_raises_400(monkeypatch, tmp_path):
    db = _make_db_session()
    track = Track(file_path="/m/only.mp3", title="Only", artist="A")
    db.add(track)
    db.commit()
    db.refresh(track)

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="End FM", dj_name="DJ End")),
    )
    monkeypatch.setattr("app.main.build_station_queue", lambda **_kwargs: {"tracks": [track]})
    _fake_dj_next(db, "End FM", "DJ End", tmp_path, monkeypatch)

    player_play(PlayerPlayRequest(queue_size=1), db)

    with pytest.raises(HTTPException) as exc:
        player_next(None, db)
    assert exc.value.status_code == 400


def test_player_next_no_queue_raises_400():
    db = _make_db_session()
    state = PlayerState(is_playing=False, queue_json=None)
    db.add(state)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        player_next(None, db)
    assert exc.value.status_code == 400


def test_media_track_serves_file(monkeypatch, tmp_path):
    db = _make_db_session()
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake-mp3")
    track = Track(file_path=str(audio), title="My Song", artist="X")
    db.add(track)
    db.commit()
    db.refresh(track)

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder=str(tmp_path)))

    response = media_track(track.id, db)
    assert response.body == b"fake-mp3"
    assert response.media_type == "audio/mpeg"


def test_media_track_not_found_raises_404():
    db = _make_db_session()
    with pytest.raises(HTTPException) as exc:
        media_track(9999, db)
    assert exc.value.status_code == 404


def test_media_dj_clip_serves_file(monkeypatch, tmp_path):
    db = _make_db_session()
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF")
    clip = DJClip(
        script_text="Hello world",
        audio_path=str(wav),
        voice="default",
        script_hash="deadbeef1234",
    )
    db.add(clip)
    db.commit()

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig(music_folder=str(tmp_path)))

    response = media_dj_clip("deadbeef1234", db)
    assert response.path == str(wav)


def test_media_dj_clip_not_found_raises_404():
    db = _make_db_session()
    with pytest.raises(HTTPException) as exc:
        media_dj_clip("nosuchhash", db)
    assert exc.value.status_code == 404


def _make_queue_state(db, queue, index=0):
    """Insert a PlayerState with the given queue list and return it."""
    state = PlayerState(
        is_playing=True,
        queue_json=json.dumps(queue),
        queue_index=index,
    )
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def test_player_queue_returns_upcoming_items():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Artist A - Song 1"},
        {"type": "track", "track_id": 2, "label": "Artist B - Song 2"},
        {"type": "track", "track_id": 3, "label": "Artist C - Song 3"},
        {"type": "track", "track_id": 4, "label": "Artist D - Song 4"},
        {"type": "track", "track_id": 5, "label": "Artist E - Song 5"},
        {"type": "track", "track_id": 6, "label": "Artist F - Song 6"},
    ]
    _make_queue_state(db, queue, index=0)

    result = player_queue(db)

    assert result.queue_position == 0
    assert result.queue_depth == 6
    assert len(result.items) == 5  # up to 5 upcoming
    assert result.items[0].position == 1
    assert result.items[0].track_id == 2
    assert result.items[0].label == "Artist B - Song 2"
    assert result.items[4].position == 5
    assert result.items[4].track_id == 6


def test_player_queue_empty_when_at_end():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Only Track"},
    ]
    _make_queue_state(db, queue, index=0)

    result = player_queue(db)

    assert result.items == []


def test_player_queue_skips_non_track_items():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Track One"},
        {"type": "dj", "label": "DJ break", "script_text": "hello"},
        {"type": "track", "track_id": 3, "label": "Track Three"},
    ]
    _make_queue_state(db, queue, index=0)

    result = player_queue(db)

    assert len(result.items) == 1
    assert result.items[0].track_id == 3


def test_delete_queue_item_removes_future_track():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Current"},
        {"type": "track", "track_id": 2, "label": "Next"},
        {"type": "track", "track_id": 3, "label": "After"},
    ]
    _make_queue_state(db, queue, index=0)

    delete_queue_item(1, db)

    state = db.query(PlayerState).first()
    remaining = json.loads(state.queue_json)
    assert len(remaining) == 2
    assert remaining[1]["track_id"] == 3


def test_delete_queue_item_rejects_current_position():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Current"},
        {"type": "track", "track_id": 2, "label": "Next"},
    ]
    _make_queue_state(db, queue, index=0)

    with pytest.raises(HTTPException) as exc:
        delete_queue_item(0, db)
    assert exc.value.status_code == 404


def test_delete_queue_item_rejects_out_of_range():
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1, "label": "Only"},
    ]
    _make_queue_state(db, queue, index=0)

    with pytest.raises(HTTPException) as exc:
        delete_queue_item(5, db)
    assert exc.value.status_code == 404


# ── /library/search tests ─────────────────────────────────────────────────────

def test_search_library_returns_matching_tracks():
    db = _make_db_session()
    db.add_all([
        Track(file_path="/m/a.mp3", title="Blue Monday", artist="New Order"),
        Track(file_path="/m/b.mp3", title="Blue Lines", artist="Massive Attack"),
        Track(file_path="/m/c.mp3", title="Something Else", artist="Other Artist"),
    ])
    db.commit()

    results = search_library(q="blue", db=db)
    titles = {t.title for t in results}
    assert titles == {"Blue Monday", "Blue Lines"}
    assert all(t.id is not None for t in results)


def test_search_library_matches_artist():
    db = _make_db_session()
    db.add_all([
        Track(file_path="/m/x.mp3", title="A Song", artist="Radiohead"),
        Track(file_path="/m/y.mp3", title="Another Song", artist="Radio GA GA Band"),
        Track(file_path="/m/z.mp3", title="Unrelated", artist="Unrelated Artist"),
    ])
    db.commit()

    results = search_library(q="radio", db=db)
    assert len(results) == 2


def test_search_library_empty_query_returns_empty():
    db = _make_db_session()
    db.add(Track(file_path="/m/a.mp3", title="Something", artist="Artist"))
    db.commit()

    assert search_library(q="", db=db) == []
    assert search_library(q="   ", db=db) == []


def test_search_library_limits_to_ten_results():
    db = _make_db_session()
    for i in range(15):
        db.add(Track(file_path=f"/m/{i}.mp3", title=f"Song {i}", artist="Same Artist"))
    db.commit()

    results = search_library(q="same artist", db=db)
    assert len(results) <= 10


# ── /player/queue/inject tests ────────────────────────────────────────────────

def test_queue_inject_inserts_after_current(monkeypatch, tmp_path):
    db = _make_db_session()
    track1 = Track(file_path="/m/1.mp3", title="First", artist="A")
    track2 = Track(file_path="/m/2.mp3", title="Second", artist="B")
    track3 = Track(file_path="/m/3.mp3", title="Third", artist="C")
    db.add_all([track1, track2, track3])
    db.commit()
    db.refresh(track1); db.refresh(track2); db.refresh(track3)

    queue = [
        {"type": "track", "track_id": track1.id, "label": "A - First"},
        {"type": "track", "track_id": track3.id, "label": "C - Third"},
    ]
    state = PlayerState(is_playing=True, queue_json=json.dumps(queue), queue_index=0)
    db.add(state)
    db.commit()

    result = queue_inject(QueueInjectRequest(track_id=track2.id), db)

    assert result.position == 1
    assert result.label == "B - Second"
    assert result.queue_depth == 3

    db.refresh(state)
    updated_queue = json.loads(state.queue_json)
    assert len(updated_queue) == 3
    assert updated_queue[1]["track_id"] == track2.id


def test_queue_inject_track_not_found_raises_404():
    db = _make_db_session()
    state = PlayerState(is_playing=True, queue_json='[{"type":"track","track_id":1,"label":"X"}]', queue_index=0)
    db.add(state)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        queue_inject(QueueInjectRequest(track_id=9999), db)
    assert exc.value.status_code == 404


def test_queue_inject_no_queue_raises_400():
    db = _make_db_session()
    track = Track(file_path="/m/t.mp3", title="T", artist="A")
    db.add(track)
    db.commit()
    db.refresh(track)

    # No PlayerState at all
    with pytest.raises(HTTPException) as exc:
        queue_inject(QueueInjectRequest(track_id=track.id), db)
    assert exc.value.status_code == 400


def test_queue_inject_empty_queue_raises_400():
    db = _make_db_session()
    track = Track(file_path="/m/t.mp3", title="T", artist="A")
    db.add(track)
    db.commit()
    db.refresh(track)

    state = PlayerState(is_playing=True, queue_json='[]', queue_index=0)
    db.add(state)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        queue_inject(QueueInjectRequest(track_id=track.id), db)
    assert exc.value.status_code == 400


# ── DJ clip prefetch ──────────────────────────────────────────────────────────

def test_player_next_uses_prefetched_clip_when_available(monkeypatch, tmp_path):
    from app.main import _prefetch_cache

    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    # Seed a pre-existing clip in the DB and a prefetch cache entry pointing to it.
    wav = tmp_path / "prefetched.wav"
    wav.write_bytes(b"RIFF")
    cached_clip = DJClip(
        script_text="Prefetched script", audio_path=str(wav),
        voice="default", script_hash="prefetchhash",
    )
    db.add(cached_clip)
    db.commit()

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Pre FM", dj_name="DJ Pre")),
    )
    monkeypatch.setattr("app.main.build_station_queue", lambda **_k: {"tracks": [t1, t2]})

    # If the prefetch is honored, generate_dj_script should NOT be called.
    call_count = {"n": 0}
    def _should_not_be_called(*_a, **_k):
        call_count["n"] += 1
        return DJScriptResponse(station_name="x", dj_name="y", sentences=["x"], script_text="x")
    monkeypatch.setattr("app.main.generate_dj_script", _should_not_be_called)

    player_play(PlayerPlayRequest(queue_size=2), db)
    _prefetch_cache[1] = {"script_text": "Prefetched script", "clip_hash": "prefetchhash"}

    result = player_next(None, db)

    assert result.dj_clip_url == f"/media/dj-clip/prefetchhash"
    assert call_count["n"] == 0
    assert 1 not in _prefetch_cache  # popped after use


def test_player_next_skip_bypasses_prefetch_cache(monkeypatch, tmp_path):
    from app.main import _prefetch_cache
    from app.schemas import PlayerNextRequest

    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    monkeypatch.setattr(
        "app.main.load_config",
        lambda: AppConfig(station=StationConfig(name="Skip FM", dj_name="DJ Skip")),
    )
    monkeypatch.setattr("app.main.build_station_queue", lambda **_k: {"tracks": [t1, t2]})
    _fake_dj_next(db, "Skip FM", "DJ Skip", tmp_path, monkeypatch)

    player_play(PlayerPlayRequest(queue_size=2), db)
    _prefetch_cache[1] = {"script_text": "stale", "clip_hash": "shouldnotuse"}

    # reason="skip" should ignore the prefetch and generate fresh.
    result = player_next(PlayerNextRequest(reason="skip"), db)

    assert result.dj_clip_url == "/media/dj-clip/testhash001"
    # Cache entry remains (we only pop on hit, not on skip-bypass)
    assert _prefetch_cache.get(1) == {"script_text": "stale", "clip_hash": "shouldnotuse"}
    _prefetch_cache.pop(1, None)  # cleanup

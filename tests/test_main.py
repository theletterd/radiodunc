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
    player_play,
    player_next,
    player_stinger_url,
    player_stop,
    player_queue,
    delete_queue_item,
    media_track,
    media_dj_clip,
    media_dj_icon,
    generate_dj_avatar_endpoint,
)
from app.models import DJClip, PlayerState, Track
from app.schemas import (
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


# ── DJ avatars ──────────────────────────────────────────────────────────────

def _make_config_with_dj(dj_id: str, name: str = "Sam"):
    from app.config import DJ, StationConfig
    dj = DJ(id=dj_id, name=name, personality="warm and witty")
    return AppConfig(station=StationConfig(djs=[dj]))


def test_media_dj_icon_serves_file(monkeypatch, tmp_path):
    """A DJ with a generated avatar gets the PNG bytes back."""
    icon_dir = tmp_path / "dj_icons"
    icon_dir.mkdir()
    (icon_dir / "dj-123.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-pixels")
    monkeypatch.setattr("app.main.DJ_AVATAR_DIR", icon_dir)

    resp = media_dj_icon("dj-123")
    assert resp.path == str(icon_dir / "dj-123.png")
    assert resp.media_type == "image/png"


def test_media_dj_icon_missing_raises_404(monkeypatch, tmp_path):
    """A DJ with no generated AND no seed avatar returns 404 (so the
    front-end falls back to the coloured-initial placeholder)."""
    monkeypatch.setattr("app.main.DJ_AVATAR_DIR", tmp_path)
    monkeypatch.setattr("app.main.DJ_AVATAR_SEED_DIR", tmp_path / "no-seed-here")
    with pytest.raises(HTTPException) as exc:
        media_dj_icon("never-generated")
    assert exc.value.status_code == 404


def test_media_dj_icon_falls_back_to_seed_when_generated_missing(monkeypatch, tmp_path):
    """Fresh clones don't have anything in generated/ yet. The seed dir
    (committed alongside example-radio_config.json's roster) provides a
    default avatar for every DJ that ships with the repo, so the UI
    isn't a sea of coloured placeholders on first run."""
    generated_dir = tmp_path / "generated"
    seed_dir = tmp_path / "seed"
    generated_dir.mkdir()
    seed_dir.mkdir()
    # Only the seed file exists.
    (seed_dir / "dj-shipped.png").write_bytes(b"\x89PNG\r\n\x1a\nseed-pixels")
    monkeypatch.setattr("app.main.DJ_AVATAR_DIR", generated_dir)
    monkeypatch.setattr("app.main.DJ_AVATAR_SEED_DIR", seed_dir)

    resp = media_dj_icon("dj-shipped")
    assert resp.path == str(seed_dir / "dj-shipped.png")
    assert resp.media_type == "image/png"


def test_media_dj_icon_generated_shadows_seed(monkeypatch, tmp_path):
    """When a user regenerates a seed DJ's avatar, the new generated/
    file takes precedence over the bundled seed copy — same DJ id,
    fresher pixels."""
    generated_dir = tmp_path / "generated"
    seed_dir = tmp_path / "seed"
    generated_dir.mkdir()
    seed_dir.mkdir()
    (seed_dir / "dj-1.png").write_bytes(b"old-seed-bytes")
    (generated_dir / "dj-1.png").write_bytes(b"freshly-regenerated-bytes")
    monkeypatch.setattr("app.main.DJ_AVATAR_DIR", generated_dir)
    monkeypatch.setattr("app.main.DJ_AVATAR_SEED_DIR", seed_dir)

    resp = media_dj_icon("dj-1")
    # FileResponse points at the generated copy, not the seed.
    assert resp.path == str(generated_dir / "dj-1.png")


def test_generate_dj_avatar_endpoint_unknown_dj_returns_404(monkeypatch):
    """POSTing to /djs/<id>/avatar for a dj_id not in config raises 404."""
    monkeypatch.setattr("app.main.load_config", lambda: _make_config_with_dj("dj-real"))
    with pytest.raises(HTTPException) as exc:
        generate_dj_avatar_endpoint("dj-imaginary")
    assert exc.value.status_code == 404


def test_generate_dj_avatar_endpoint_success(monkeypatch, tmp_path):
    """Happy path: endpoint finds the DJ, calls the generator, returns a
    URL + timestamp. Generator is stubbed so the test doesn't hit OpenAI."""
    cfg = _make_config_with_dj("dj-abc", name="Avatar Andy")
    monkeypatch.setattr("app.main.load_config", lambda: cfg)
    # Pretend the generator succeeded and wrote a file.
    written = []
    def fake_gen(dj, config):
        written.append(dj.id)
        return tmp_path / f"{dj.id}.png"
    monkeypatch.setattr("app.main.generate_dj_avatar", fake_gen)

    result = generate_dj_avatar_endpoint("dj-abc")
    assert result["url"] == "/media/dj-icon/dj-abc"
    assert isinstance(result["generated_at"], int)
    assert written == ["dj-abc"]


def test_generate_dj_avatar_endpoint_failure_returns_502(monkeypatch):
    """When the generator returns None (e.g. no API key, OpenAI hiccup), the
    endpoint surfaces a 502 rather than swallowing the failure silently."""
    cfg = _make_config_with_dj("dj-xyz")
    monkeypatch.setattr("app.main.load_config", lambda: cfg)
    monkeypatch.setattr("app.main.generate_dj_avatar", lambda dj, config: None)

    with pytest.raises(HTTPException) as exc:
        generate_dj_avatar_endpoint("dj-xyz")
    assert exc.value.status_code == 502


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


def test_player_queue_includes_track_metadata_for_hover_card():
    """The Up Next hover card needs more than the label — album/year/genre/
    duration/bitrate plus the file path, which is the only field guaranteed
    to exist on a sparsely-tagged rip."""
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="Current", artist="A")
    t2 = Track(
        file_path="/m/penny.mp3", title="Penny Lane", artist="The Beatles",
        album="Magical Mystery Tour", year="1967", genre="Rock",
        duration_seconds=175.4, bitrate=320,
    )
    db.add_all([t1, t2])
    db.commit()
    for t in (t1, t2): db.refresh(t)

    _make_queue_state(db, [
        {"type": "track", "track_id": t1.id, "label": "A - Current"},
        {"type": "track", "track_id": t2.id, "label": "The Beatles - Penny Lane"},
    ], index=0)

    item = player_queue(db).items[0]

    assert item.track_id == t2.id
    assert item.file_path == "/m/penny.mp3"
    assert item.album == "Magical Mystery Tour"
    assert item.year == "1967"
    assert item.genre == "Rock"
    assert item.duration_seconds == 175.4
    assert item.bitrate == 320


def test_player_queue_metadata_is_none_for_missing_track():
    """A track deleted from the library since being queued should still appear
    in the list (with its stored label) rather than vanishing — the hover card
    just has nothing to show."""
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="Current", artist="A")
    db.add(t1)
    db.commit()
    db.refresh(t1)

    _make_queue_state(db, [
        {"type": "track", "track_id": t1.id, "label": "A - Current"},
        {"type": "track", "track_id": 9999, "label": "Since Deleted"},
    ], index=0)

    items = player_queue(db).items

    assert len(items) == 1
    assert items[0].label == "Since Deleted"
    assert items[0].file_path is None
    assert items[0].duration_seconds is None


def test_player_queue_fetches_metadata_in_one_query(monkeypatch):
    """Guard against an N+1: the queue is routinely 30+ deep and grows with
    'Add more', so metadata must come from a single IN query."""
    db = _make_db_session()
    tracks = [Track(file_path=f"/m/{i}.mp3", title=f"T{i}", artist="A") for i in range(12)]
    db.add_all(tracks)
    db.commit()
    for t in tracks: db.refresh(t)

    _make_queue_state(db, [
        {"type": "track", "track_id": t.id, "label": f"A - T{t.id}"} for t in tracks
    ], index=0)

    calls = {"n": 0}
    real_query = db.query
    def counting_query(model, *a, **k):
        if model is Track:
            calls["n"] += 1
        return real_query(model, *a, **k)
    monkeypatch.setattr(db, "query", counting_query)

    result = player_queue(db)

    assert len(result.items) == 11
    assert all(i.file_path for i in result.items)
    assert calls["n"] == 1


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


def test_queue_inject_appends_at_end_when_position_is_end(monkeypatch, tmp_path):
    """The "Add to playlist" button in the search results sends position='end',
    which appends the requested track to the tail of the queue (heard
    eventually) rather than slotting it right after the currently-playing
    one. The track is still flagged requested=True so the DJ banter
    acknowledges it when the queue gets there."""
    from app.prefetch import _prefetch_cache, _prefetch_lock
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="Current", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Already Queued", artist="B")
    t3 = Track(file_path="/m/3.mp3", title="Also Queued", artist="C")
    t4 = Track(file_path="/m/4.mp3", title="Appended", artist="D")
    db.add_all([t1, t2, t3, t4])
    db.commit()
    for t in (t1, t2, t3, t4): db.refresh(t)

    queue = [
        {"type": "track", "track_id": t1.id, "label": "A - Current"},
        {"type": "track", "track_id": t2.id, "label": "B - Already Queued"},
        {"type": "track", "track_id": t3.id, "label": "C - Also Queued"},
    ]
    state = PlayerState(is_playing=True, queue_json=json.dumps(queue), queue_index=0)
    db.add(state)
    db.commit()

    # Pre-seed the prefetch cache so we can verify position='end' DOESN'T
    # clear it (the immediate-next-track wasn't displaced).
    with _prefetch_lock:
        _prefetch_cache[1] = {"script_text": "for t2", "clip_hash": "hash-t2"}

    result = queue_inject(
        QueueInjectRequest(track_id=t4.id, position="end"), db,
    )

    # Appended at the tail (index 3), queue is now 4 deep.
    assert result.position == 3
    assert result.label == "D - Appended"
    assert result.queue_depth == 4

    db.refresh(state)
    updated = json.loads(state.queue_json)
    assert len(updated) == 4
    assert updated[3]["track_id"] == t4.id
    # requested flag preserved across the position variants.
    assert updated[3]["requested"] is True
    # Original queue order is untouched.
    assert [item["track_id"] for item in updated[:3]] == [t1.id, t2.id, t3.id]
    # Prefetch cache survives — t2 is still next, the prefetch is still valid.
    assert _prefetch_cache.get(1) == {"script_text": "for t2", "clip_hash": "hash-t2"}

    # Cleanup so other tests start clean.
    with _prefetch_lock:
        _prefetch_cache.clear()


def test_queue_inject_position_next_still_clears_prefetch_cache(monkeypatch):
    """Counterpart: the default position='next' DOES clear the prefetch
    cache because what was queue[idx+1] is no longer next — the prefetched
    clip is for the wrong track now."""
    from app.prefetch import _prefetch_cache, _prefetch_lock
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="Current", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Was Next", artist="B")
    t3 = Track(file_path="/m/3.mp3", title="Jumping In", artist="C")
    db.add_all([t1, t2, t3])
    db.commit()
    for t in (t1, t2, t3): db.refresh(t)

    queue = [
        {"type": "track", "track_id": t1.id, "label": "A - Current"},
        {"type": "track", "track_id": t2.id, "label": "B - Was Next"},
    ]
    state = PlayerState(is_playing=True, queue_json=json.dumps(queue), queue_index=0)
    db.add(state)
    db.commit()

    with _prefetch_lock:
        _prefetch_cache[1] = {"script_text": "for t2", "clip_hash": "hash-t2"}

    queue_inject(QueueInjectRequest(track_id=t3.id), db)  # position defaults to "next"

    # Prefetched clip was for t2-as-next; t3 took that slot, so the clip is stale.
    assert _prefetch_cache == {}


def test_queue_inject_position_defaults_to_next():
    """Schema default keeps backwards compatibility — old clients (and any
    server-side caller) that don't pass position still get the historical
    "insert after current" behaviour."""
    req = QueueInjectRequest(track_id=42)
    assert req.position == "next"


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
    from app.prefetch import _prefetch_cache

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
    from app.prefetch import _prefetch_cache
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


# ── Segment attachment helpers ──


def _attach_config():
    return AppConfig(station=StationConfig(name="Attach FM", dj_name="DJ Attach"))


class _FakeClip:
    def __init__(self, script_hash="fakehash", script_text="fake script"):
        self.script_hash = script_hash
        self.script_text = script_text


# _attach_news


def test_attach_news_returns_none_when_no_clip(monkeypatch):
    from app.main import _attach_news

    monkeypatch.setattr("app.main.get_news_clip", lambda cfg: None)

    assert _attach_news(_attach_config()) == (None, None)


def test_attach_news_returns_url_and_script_on_success(monkeypatch):
    from app.main import _attach_news
    import time as _t

    entry = {
        "generated_at": _t.time() - 5,
        "clip_hash": "newshash123",
        "script_text": "Here is the news.",
    }
    monkeypatch.setattr("app.main.get_news_clip", lambda cfg: entry)

    url, script = _attach_news(_attach_config())
    assert url == "/media/dj-clip/newshash123"
    assert script == "Here is the news."


# _attach_ad


def test_attach_ad_pool_full_returns_random_existing_clip(monkeypatch, tmp_path):
    from app.main import _attach_ad

    db = _make_db_session()
    for i in range(3):
        db.add(DJClip(
            script_text=f"ad script {i}",
            audio_path=str(tmp_path / f"ad{i}.wav"),
            voice="echo",
            script_hash=f"adhash{i}",
            is_ad=True,
        ))
    db.commit()

    cfg = _attach_config()
    cfg.alerts.ads.pool_size = 2  # 3 cached >= 2 triggers pool-full path

    monkeypatch.setattr("app.main.random.choice", lambda seq: seq[0])
    monkeypatch.setattr(
        "app.main.generate_ad_script",
        lambda *a, **k: pytest.fail("generate_ad_script should not be called"),
    )

    url, script = _attach_ad(db, cfg.station, cfg, provider=None)
    assert url == "/media/dj-clip/adhash0"
    assert script == "ad script 0"


def test_attach_ad_generate_returns_none_when_script_is_none(monkeypatch):
    from app.main import _attach_ad

    db = _make_db_session()
    cfg = _attach_config()
    cfg.alerts.ads.pool_size = 100  # empty DB < pool_size → generate path

    monkeypatch.setattr("app.main.generate_ad_script", lambda station, config: None)

    url, script = _attach_ad(db, cfg.station, cfg, provider=None)
    assert url is None
    assert script is None


def test_attach_ad_generate_succeeds(monkeypatch):
    from app.main import _attach_ad

    db = _make_db_session()
    cfg = _attach_config()
    cfg.alerts.ads.pool_size = 100

    monkeypatch.setattr("app.main.generate_ad_script", lambda station, config: "buy stuff")
    clip = _FakeClip(script_hash="genadhash", script_text="buy stuff")
    monkeypatch.setattr(
        "app.main.get_or_create_dj_clip",
        lambda db, **kwargs: (clip, "", False),
    )

    url, script = _attach_ad(db, cfg.station, cfg, provider=None)
    assert url == "/media/dj-clip/genadhash"
    assert script == "buy stuff"


def test_attach_ad_retries_with_default_voice_on_runtime_error(monkeypatch):
    from app.main import _attach_ad

    db = _make_db_session()
    cfg = _attach_config()
    cfg.alerts.ads.pool_size = 100

    monkeypatch.setattr("app.main.generate_ad_script", lambda station, config: "buy stuff")

    calls = []
    clip = _FakeClip(script_hash="retryadhash", script_text="buy stuff")

    def fake_get_or_create(db, **kwargs):
        calls.append(kwargs.get("voice"))
        if len(calls) == 1:
            raise RuntimeError("voice unsupported")
        return clip, "", False

    monkeypatch.setattr("app.main.get_or_create_dj_clip", fake_get_or_create)

    url, script = _attach_ad(db, cfg.station, cfg, provider=None)
    assert url == "/media/dj-clip/retryadhash"
    assert script == "buy stuff"
    assert len(calls) == 2
    assert calls[1] is None  # second attempt used voice=None


# _attach_station_id


def test_attach_station_id_returns_none_when_disabled(monkeypatch):
    from app.main import _attach_station_id

    db = _make_db_session()
    cfg = _attach_config()
    cfg.alerts.station_id.enabled = False

    monkeypatch.setattr(
        "app.main.get_station_id_phrases",
        lambda config: pytest.fail("phrases should not be fetched when disabled"),
    )

    assert _attach_station_id(db, cfg.station, voice=None, config=cfg, provider=None) is None


def test_attach_station_id_returns_none_when_no_phrases(monkeypatch):
    from app.main import _attach_station_id

    db = _make_db_session()
    cfg = _attach_config()

    monkeypatch.setattr("app.main.get_station_id_phrases", lambda config: [])

    assert _attach_station_id(db, cfg.station, voice="echo", config=cfg, provider=None) is None


def test_attach_station_id_returns_url_on_success(monkeypatch):
    from app.main import _attach_station_id

    db = _make_db_session()
    cfg = _attach_config()

    monkeypatch.setattr("app.main.get_station_id_phrases", lambda config: ["You're tuned to Attach FM"])
    monkeypatch.setattr("app.main.random.choice", lambda seq: seq[0])
    clip = _FakeClip(script_hash="sidhash", script_text="You're tuned to Attach FM")
    monkeypatch.setattr(
        "app.main.get_or_create_dj_clip",
        lambda db, **kwargs: (clip, "", True),
    )

    url = _attach_station_id(db, cfg.station, voice="echo", config=cfg, provider=None)
    assert url == "/media/dj-clip/sidhash"


def test_attach_station_id_returns_none_on_runtime_error(monkeypatch):
    from app.main import _attach_station_id

    db = _make_db_session()
    cfg = _attach_config()

    monkeypatch.setattr("app.main.get_station_id_phrases", lambda config: ["phrase"])

    def boom(db, **kwargs):
        raise RuntimeError("synthesis failed")

    monkeypatch.setattr("app.main.get_or_create_dj_clip", boom)

    assert _attach_station_id(db, cfg.station, voice="echo", config=cfg, provider=None) is None


# ── Skip-stinger endpoint ─────────────────────────────────────────────────────

def _seed_station_id_clip(db, *, script_hash: str, audio_path: str) -> DJClip:
    clip = DJClip(
        script_text="This is RadioDunc.",
        audio_path=audio_path,
        voice="verse",
        script_hash=script_hash,
        is_ad=False,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip


def test_player_stinger_url_returns_null_when_pool_empty():
    db = _make_db_session()
    result = player_stinger_url(db)
    assert result.clip_url is None


def test_player_stinger_url_returns_station_id_clip_url():
    db = _make_db_session()
    _seed_station_id_clip(db, script_hash="stingerhash1", audio_path="generated/station_ids/stingerhash1.mp3")
    result = player_stinger_url(db)
    assert result.clip_url == "/media/dj-clip/stingerhash1"


def test_player_stinger_url_ignores_clips_outside_station_ids_subdir():
    db = _make_db_session()
    # A regular transition clip — should NOT be picked.
    _seed_station_id_clip(db, script_hash="djhash1", audio_path="generated/transitions/djhash1.mp3")
    # An ad clip — should NOT be picked either.
    _seed_station_id_clip(db, script_hash="adhash1", audio_path="generated/ads/adhash1.mp3")
    result = player_stinger_url(db)
    assert result.clip_url is None


def test_player_stinger_url_picks_from_multiple_station_id_clips():
    """With multiple available, the endpoint returns one of them (random)."""
    db = _make_db_session()
    hashes = ["sid1", "sid2", "sid3"]
    for h in hashes:
        _seed_station_id_clip(db, script_hash=h, audio_path=f"generated/station_ids/{h}.mp3")
    seen = set()
    for _ in range(20):
        url = player_stinger_url(db).clip_url
        assert url is not None
        seen.add(url)
    # In 20 picks across 3 clips, we should see at least 2 different hashes.
    assert len(seen) >= 2, f"expected variety across 3 clips, only got: {seen}"


# ── Cache warmup ──────────────────────────────────────────────────────────────

def test_warm_caches_runs_phrases_and_news_and_seeds_stinger(monkeypatch):
    from app.main import _warm_caches_background, SessionLocal

    calls = {"phrases": 0, "news": 0, "clip": 0}
    seeded_clip = None

    monkeypatch.setattr("app.main.get_station_id_phrases", lambda cfg: calls.__setitem__("phrases", calls["phrases"] + 1) or ["This is Test FM."])
    monkeypatch.setattr("app.main.get_news_clip", lambda cfg: calls.__setitem__("news", calls["news"] + 1) or {"clip_hash": "n1", "script_text": "Bulletin.", "generated_at": 0})
    monkeypatch.setattr("app.main.build_tts_provider", lambda cfg: None)

    db = _make_db_session()
    monkeypatch.setattr("app.main.SessionLocal", lambda: db)

    def fake_get_or_create(*args, **kwargs):
        nonlocal seeded_clip
        seeded_clip = DJClip(
            script_text=kwargs.get("script_text", "x"),
            audio_path="generated/station_ids/warmedhash.mp3",
            voice="verse",
            script_hash="warmedhash",
        )
        db.add(seeded_clip)
        db.commit()
        db.refresh(seeded_clip)
        calls["clip"] += 1
        return seeded_clip, seeded_clip.audio_path, False

    monkeypatch.setattr("app.main.get_or_create_dj_clip", fake_get_or_create)

    cfg = AppConfig(station=StationConfig(name="Test FM"))
    _warm_caches_background(cfg)

    assert calls["phrases"] == 1
    assert calls["news"] == 1
    assert calls["clip"] == 1  # stinger was seeded because pool was empty
    assert seeded_clip is not None


def test_warm_caches_skips_stinger_when_pool_already_has_one(monkeypatch):
    from app.main import _warm_caches_background

    db = _make_db_session()
    # Pre-seed the pool — warmup should NOT generate another clip.
    db.add(DJClip(
        script_text="existing", audio_path="generated/station_ids/x.mp3",
        voice="verse", script_hash="existinghash",
    ))
    db.commit()

    monkeypatch.setattr("app.main.SessionLocal", lambda: db)
    monkeypatch.setattr("app.main.get_station_id_phrases", lambda cfg: ["Phrase."])
    monkeypatch.setattr("app.main.get_news_clip", lambda cfg: None)
    monkeypatch.setattr("app.main.build_tts_provider", lambda cfg: None)

    create_count = 0
    def fake_get_or_create(*args, **kwargs):
        nonlocal create_count
        create_count += 1
        return None, "", False
    monkeypatch.setattr("app.main.get_or_create_dj_clip", fake_get_or_create)

    _warm_caches_background(AppConfig(station=StationConfig(name="Test FM")))
    assert create_count == 0  # existing stinger satisfies the pool check


def test_warm_caches_swallows_exceptions(monkeypatch):
    """Warmup must never propagate — playback shouldn't fail because the warmup did."""
    from app.main import _warm_caches_background
    monkeypatch.setattr("app.main.get_station_id_phrases", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("app.main.get_news_clip", lambda cfg: None)
    # No exception should escape.
    _warm_caches_background(AppConfig(station=StationConfig(name="Test FM")))


# ── TTS preview endpoint ─────────────────────────────────────────────────────

def test_tts_preview_returns_clip_url_on_success(monkeypatch):
    from app.main import tts_preview
    from app.schemas import TTSPreviewRequest

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())
    monkeypatch.setattr("app.main.build_tts_provider", lambda cfg: None)
    fake_clip = DJClip(
        script_text="Hello", audio_path="generated/previews/abc.mp3",
        voice="verse", script_hash="abc",
    )
    monkeypatch.setattr("app.main.get_or_create_dj_clip",
                        lambda *a, **kw: (fake_clip, fake_clip.audio_path, False))

    db = _make_db_session()
    result = tts_preview(TTSPreviewRequest(text="Hello world", voice="verse"), db)
    assert result.clip_url == "/media/dj-clip/abc"


def test_tts_preview_passes_clip_type_previews(monkeypatch):
    from app.main import tts_preview
    from app.schemas import TTSPreviewRequest

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())
    monkeypatch.setattr("app.main.build_tts_provider", lambda cfg: None)
    captured = {}
    def fake_create(*a, **kw):
        captured.update(kw)
        clip = DJClip(script_text="x", audio_path="p", voice="v", script_hash="h")
        return clip, "p", False
    monkeypatch.setattr("app.main.get_or_create_dj_clip", fake_create)

    tts_preview(TTSPreviewRequest(text="Sample", voice="sage", voice_instructions="warm"), _make_db_session())
    assert captured["clip_type"] == "previews"
    assert captured["voice"] == "sage"
    assert captured["voice_instructions"] == "warm"
    assert captured["script_text"] == "Sample"


def test_tts_preview_raises_502_when_provider_fails(monkeypatch):
    from app.main import tts_preview
    from app.schemas import TTSPreviewRequest

    monkeypatch.setattr("app.main.load_config", lambda: AppConfig())
    monkeypatch.setattr("app.main.build_tts_provider", lambda cfg: None)
    def fake_create(*a, **kw):
        raise RuntimeError("openai down")
    monkeypatch.setattr("app.main.get_or_create_dj_clip", fake_create)

    with pytest.raises(HTTPException) as exc:
        tts_preview(TTSPreviewRequest(text="x"), _make_db_session())
    assert exc.value.status_code == 502


# ── PUT /config cache-invalidation hook ───────────────────────────────────────
#
# update_config calls _on_config_changed(old, new) after save_config. The hook
# clears in-memory derived caches selectively — these tests pin down WHICH
# field changes trigger WHICH invalidations.


def _wire_update_config(monkeypatch, old_cfg: AppConfig):
    """Stub load_config to return old_cfg and capture save_config calls."""
    saved: dict = {}
    monkeypatch.setattr("app.main.load_config", lambda: old_cfg)
    monkeypatch.setattr("app.main.save_config", lambda c: saved.setdefault("config", c))
    return saved


def test_config_change_station_name_clears_news_and_prefetch(monkeypatch):
    import app.news_cache as nc
    from app.prefetch import _prefetch_cache

    old = AppConfig(station=StationConfig(name="Old FM"))
    new = old.model_copy(update={"station": StationConfig(name="New FM")})

    _wire_update_config(monkeypatch, old)
    nc._news_cache = {"generated_at": 0, "clip_hash": "h", "script_text": "s"}
    _prefetch_cache[5] = {"script_text": "stale", "clip_hash": "h"}

    update_config(new)

    assert nc._news_cache is None
    assert _prefetch_cache == {}


def test_config_change_no_change_invalidates_nothing(monkeypatch):
    """Saving the same config back (e.g. a no-op PUT from the settings UI)
    should be a complete no-op for every cache."""
    import app.news_cache as nc
    from app.prefetch import _prefetch_cache

    cfg = AppConfig()
    _wire_update_config(monkeypatch, cfg)
    cached_news = {"generated_at": 0, "clip_hash": "h", "script_text": "s"}
    nc._news_cache = cached_news
    _prefetch_cache[1] = {"script_text": "x", "clip_hash": "h"}

    update_config(cfg.model_copy(deep=True))

    assert nc._news_cache is cached_news
    assert _prefetch_cache == {1: {"script_text": "x", "clip_hash": "h"}}
    # cleanup
    nc._news_cache = None
    _prefetch_cache.clear()


def test_config_change_tts_voice_invalidates_news_and_prefetch(monkeypatch):
    """Switching openai_tts_voice changes how every generated clip SOUNDS, so
    any pre-synthesised audio in memory is stale."""
    import app.news_cache as nc
    from app.prefetch import _prefetch_cache

    old = AppConfig(openai_tts_voice="verse")
    new = old.model_copy(update={"openai_tts_voice": "sage"})

    _wire_update_config(monkeypatch, old)
    nc._news_cache = {"generated_at": 0, "clip_hash": "h", "script_text": "s"}
    _prefetch_cache[1] = {"script_text": "x", "clip_hash": "h"}

    update_config(new)

    assert nc._news_cache is None
    assert _prefetch_cache == {}


def test_config_change_hook_failure_does_not_break_put(monkeypatch):
    """If the cache-invalidation hook raises, the PUT still returns the new config —
    the on-disk save already succeeded and we must not 500 over a cache flush."""
    import app.news_cache as nc
    from app.prefetch import _prefetch_cache

    old = AppConfig()
    new = old.model_copy(update={"openai_tts_voice": "sage"})
    _wire_update_config(monkeypatch, old)

    # Trip the hook by making one of its internal calls raise. _log_event runs
    # after each cache invalidation inside the try-block, so this guarantees an
    # exception lands in the handler.
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr("app.main._log_event", boom)

    result = update_config(new)
    assert result.openai_tts_voice == "sage"
    # cleanup
    nc._news_cache = None
    _prefetch_cache.clear()


# ── On-air avatar plumbing ─────────────────────────────────────────────────

def test_station_out_includes_active_dj_id_when_provided():
    """_station_out passes the active_dj_id through when the caller knows
    which DJ is currently on air. The client uses this to build the on-air
    avatar URL — active_station's model_copy flattens the override into
    dj_name/personality and loses the id, so we surface it separately."""
    from app.main import _station_out
    from app.config import StationConfig
    station = StationConfig(name="Test FM", dj_name="Sam", personality="warm")
    out = _station_out(station, active_dj_id="dj-currently-on-air")
    assert out.active_dj_id == "dj-currently-on-air"


def test_station_out_active_dj_id_defaults_to_none():
    """When no active DJ is supplied (or the Default DJ is hosting), the
    field comes back as None so the client falls back to a neutral badge."""
    from app.main import _station_out
    from app.config import StationConfig
    station = StationConfig(name="Test FM", dj_name="Sam", personality="warm")
    out = _station_out(station)
    assert out.active_dj_id is None


def test_active_dj_returns_dj_for_matching_show(monkeypatch):
    """active_dj is a thin wrapper over pick_active_persona that handles the
    timezone-aware now-defaulting active_station does. End-to-end check that
    a Show whose shifts cover 'now' yields the right DJ."""
    from datetime import datetime
    from app.config import DJ, DJShift, Show, StationConfig
    from app.dj_scripts import active_dj
    dj = DJ(id="dj-monday-noon", name="Noonish", personality="bright")
    show = Show(
        id="show-1", dj_id=dj.id,
        shifts=[DJShift(day="monday", start_hour=0, end_hour=23)],
    )
    station = StationConfig(djs=[dj], shows=[show])
    cfg = AppConfig(station=station)
    monday_noon = datetime(2026, 5, 18, 12, 0)
    result = active_dj(station, cfg, now=monday_noon)
    assert result is not None
    assert result.id == "dj-monday-noon"


def test_active_dj_returns_none_for_default_dj_slot():
    """A matching Show with dj_id=None means 'Default DJ hosts this slot' —
    active_dj returns None so the client knows to render the neutral badge,
    not look up a non-existent avatar."""
    from datetime import datetime
    from app.config import DJShift, Show, StationConfig
    from app.dj_scripts import active_dj
    show = Show(
        id="show-default", dj_id=None,
        shifts=[DJShift(day="monday", start_hour=0, end_hour=23)],
    )
    station = StationConfig(djs=[], shows=[show])
    cfg = AppConfig(station=station)
    monday_noon = datetime(2026, 5, 18, 12, 0)
    assert active_dj(station, cfg, now=monday_noon) is None

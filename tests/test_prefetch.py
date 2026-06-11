"""Tests for app/prefetch.py — the DJ-clip prefetch cache and worker.

Split out of test_main.py when the cache moved to its own module. The
/player/prefetch endpoint (which stays in main.py) is tested here too,
since scheduling the worker is the endpoint's whole job.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig, StationConfig
from app.database import Base
from app.main import player_prefetch
from app.models import PlayerState, Track
from app.prefetch import _prefetch_cache, prefetch_dj_clip
from app.schemas import DJScriptResponse


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


@pytest.fixture
def _reset_prefetch_cache():
    """Reset the module-level prefetch cache before and after each test."""
    _prefetch_cache.clear()
    yield
    _prefetch_cache.clear()


class _FakeThread:
    """Captures construction args; .start() is a no-op so no real thread runs."""
    instances: list["_FakeThread"] = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.started = False
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True


@pytest.fixture
def _fake_thread(monkeypatch):
    _FakeThread.instances = []
    monkeypatch.setattr("app.main.threading.Thread", _FakeThread)
    return _FakeThread


def test_player_prefetch_returns_idle_when_not_playing(_reset_prefetch_cache, _fake_thread):
    db = _make_db_session()
    state = PlayerState(is_playing=False, queue_json='[{"type":"track","track_id":1}]', queue_index=0)
    db.add(state)
    db.commit()

    result = player_prefetch(db)

    assert result == {"status": "idle"}
    assert _fake_thread.instances == []


def test_player_prefetch_returns_idle_when_queue_empty(_reset_prefetch_cache, _fake_thread):
    db = _make_db_session()
    # is_playing=True but queue_json is empty string -> falsy
    state = PlayerState(is_playing=True, queue_json="", queue_index=0)
    db.add(state)
    db.commit()

    result = player_prefetch(db)

    assert result == {"status": "idle"}
    assert _fake_thread.instances == []


def test_player_prefetch_returns_end_of_queue_at_last_item(_reset_prefetch_cache, _fake_thread):
    db = _make_db_session()
    queue = [{"type": "track", "track_id": 1}, {"type": "track", "track_id": 2}]
    state = PlayerState(is_playing=True, queue_json=json.dumps(queue), queue_index=1)
    db.add(state)
    db.commit()

    result = player_prefetch(db)

    assert result == {"status": "end_of_queue"}
    assert _fake_thread.instances == []


def test_player_prefetch_schedules_thread_with_correct_args(_reset_prefetch_cache, _fake_thread):
    db = _make_db_session()
    queue = [
        {"type": "track", "track_id": 1},
        {"type": "track", "track_id": 2},
        {"type": "track", "track_id": 3},
    ]
    state = PlayerState(is_playing=True, queue_json=json.dumps(queue), queue_index=0)
    db.add(state)
    db.commit()

    result = player_prefetch(db)

    assert result == {"status": "scheduled"}
    assert len(_fake_thread.instances) == 1
    t = _fake_thread.instances[0]
    assert t.started is True
    assert t.target is prefetch_dj_clip
    assert t.args == (1, queue, 0)
    assert t.daemon is True


def _patch_session_local(monkeypatch, db):
    """Make prefetch_dj_clip's SessionLocal() return our test session."""
    monkeypatch.setattr("app.prefetch.SessionLocal", lambda: _NonClosingSession(db))


class _NonClosingSession:
    """Wraps a Session so that .close() is a no-op (the test owns the lifecycle)."""
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def test_prefetch_worker_bails_when_target_idx_out_of_range(_reset_prefetch_cache):
    queue = [{"type": "track", "track_id": 1}]
    prefetch_dj_clip(target_idx=5, queue=queue, base_idx=0)

    assert _prefetch_cache == {}


def test_prefetch_worker_bails_when_target_not_track(_reset_prefetch_cache):
    queue = [{"type": "track", "track_id": 1}, {"type": "news"}]
    prefetch_dj_clip(target_idx=1, queue=queue, base_idx=0)

    assert _prefetch_cache == {}


class _FakeClip:
    def __init__(self, script_hash):
        self.script_hash = script_hash


def test_prefetch_worker_populates_cache_on_success(monkeypatch, _reset_prefetch_cache):
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    _patch_session_local(monkeypatch, db)
    monkeypatch.setattr(
        "app.prefetch.load_config",
        lambda: AppConfig(station=StationConfig(name="Pre FM", dj_name="DJ Pre")),
    )
    monkeypatch.setattr(
        "app.prefetch.generate_dj_script",
        lambda *_a, **_k: DJScriptResponse(
            station_name="Pre FM", dj_name="DJ Pre",
            sentences=["Hi"], script_text="Hi there.",
        ),
    )
    monkeypatch.setattr(
        "app.prefetch.get_or_create_dj_clip",
        lambda *_a, **_k: (_FakeClip(script_hash="abc123"), "/path/clip.wav", False),
    )
    monkeypatch.setattr("app.prefetch.build_tts_provider", lambda _cfg: object())

    queue = [
        {"type": "track", "track_id": t1.id},
        {"type": "track", "track_id": t2.id},
    ]
    prefetch_dj_clip(target_idx=1, queue=queue, base_idx=0)

    assert _prefetch_cache[1] == {"script_text": "Hi there.", "clip_hash": "abc123"}


def test_prefetch_worker_uses_request_reason_for_requested_track(monkeypatch, _reset_prefetch_cache):
    """Regression: the prefetch path used to always pass reason='auto' to
    generate_dj_script, so caller-requested tracks (queued via the search
    bar with requested=True) lost their "audience request" framing if the
    prefetch happened to fire on them. The DJ would greet a requested
    track exactly like a natural-advance one — no "we've got a request
    coming in" framing — because the prefetch baked the wrong reason
    into the cached clip.

    Now the prefetch reads target_item.get("requested") and switches to
    reason="request" accordingly, so the cached clip is the right kind of
    banter from the start.
    """
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="Current", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Requested", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    _patch_session_local(monkeypatch, db)
    monkeypatch.setattr(
        "app.prefetch.load_config",
        lambda: AppConfig(station=StationConfig(name="Req FM", dj_name="DJ Req")),
    )

    captured = {}
    def _capture_script(_station, payload, _prev, _next, *, config=None):
        captured["reason"] = payload.reason
        return DJScriptResponse(
            station_name="Req FM", dj_name="DJ Req",
            sentences=["Hi"], script_text="Hi.",
        )
    monkeypatch.setattr("app.prefetch.generate_dj_script", _capture_script)
    monkeypatch.setattr(
        "app.prefetch.get_or_create_dj_clip",
        lambda *_a, **_k: (_FakeClip(script_hash="reqhash"), "/path/clip.wav", False),
    )
    monkeypatch.setattr("app.prefetch.build_tts_provider", lambda _cfg: object())

    # Critically: target is the requested-flag track.
    queue = [
        {"type": "track", "track_id": t1.id},
        {"type": "track", "track_id": t2.id, "requested": True},
    ]
    prefetch_dj_clip(target_idx=1, queue=queue, base_idx=0)

    assert captured.get("reason") == "request"


def test_prefetch_worker_uses_auto_reason_for_natural_advance(monkeypatch, _reset_prefetch_cache):
    """Counterpart to the above: ordinary queue items (no requested flag)
    still take the reason='auto' path, so we don't regress the default
    natural-advance banter into something more elaborate than it needs."""
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    _patch_session_local(monkeypatch, db)
    monkeypatch.setattr(
        "app.prefetch.load_config",
        lambda: AppConfig(station=StationConfig(name="Auto FM", dj_name="DJ Auto")),
    )

    captured = {}
    def _capture_script(_station, payload, _prev, _next, *, config=None):
        captured["reason"] = payload.reason
        return DJScriptResponse(
            station_name="Auto FM", dj_name="DJ Auto",
            sentences=["Hi"], script_text="Hi.",
        )
    monkeypatch.setattr("app.prefetch.generate_dj_script", _capture_script)
    monkeypatch.setattr(
        "app.prefetch.get_or_create_dj_clip",
        lambda *_a, **_k: (_FakeClip(script_hash="autohash"), "/path/clip.wav", False),
    )
    monkeypatch.setattr("app.prefetch.build_tts_provider", lambda _cfg: object())

    queue = [
        {"type": "track", "track_id": t1.id},
        {"type": "track", "track_id": t2.id},  # no `requested` key at all
    ]
    prefetch_dj_clip(target_idx=1, queue=queue, base_idx=0)

    assert captured.get("reason") == "auto"


def test_prefetch_worker_retries_with_voice_none_on_runtime_error(monkeypatch, _reset_prefetch_cache):
    db = _make_db_session()
    t1 = Track(file_path="/m/1.mp3", title="One", artist="A")
    t2 = Track(file_path="/m/2.mp3", title="Two", artist="B")
    db.add_all([t1, t2])
    db.commit()
    db.refresh(t1); db.refresh(t2)

    _patch_session_local(monkeypatch, db)
    monkeypatch.setattr(
        "app.prefetch.load_config",
        lambda: AppConfig(station=StationConfig(name="Retry FM", dj_name="DJ Retry", voice="alloy")),
    )
    monkeypatch.setattr(
        "app.prefetch.generate_dj_script",
        lambda *_a, **_k: DJScriptResponse(
            station_name="Retry FM", dj_name="DJ Retry",
            sentences=["Hi"], script_text="Retry script.",
        ),
    )
    monkeypatch.setattr("app.prefetch.build_tts_provider", lambda _cfg: object())

    calls = []

    def fake_get_or_create(_db, *, script_text, voice, provider, clip_type, voice_instructions=None):
        calls.append({"voice": voice, "voice_instructions": voice_instructions})
        if len(calls) == 1:
            raise RuntimeError("voice rejected")
        return (_FakeClip(script_hash="retryhash"), "/path/clip.wav", False)

    monkeypatch.setattr("app.prefetch.get_or_create_dj_clip", fake_get_or_create)

    queue = [
        {"type": "track", "track_id": t1.id},
        {"type": "track", "track_id": t2.id},
    ]
    prefetch_dj_clip(target_idx=1, queue=queue, base_idx=0)

    assert len(calls) == 2
    assert calls[0]["voice"] == "alloy"
    assert calls[1]["voice"] is None
    assert _prefetch_cache[1] == {"script_text": "Retry script.", "clip_hash": "retryhash"}

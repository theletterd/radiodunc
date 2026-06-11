"""Tests for app/news_cache.py — the news bulletin clip cache.

Split out of test_main.py when the cache moved to its own module.
_attach_news (which stays in main.py but is the cache's main consumer)
is tested here too, since its behaviour is all about cache interaction.
"""

import time as _time_mod

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.news_cache as nc
from app.config import AppConfig, StationConfig
from app.database import Base


def _make_db_session():
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return TestingSessionLocal()


def _attach_config():
    return AppConfig(station=StationConfig(name="Attach FM", dj_name="DJ Attach"))


class _FakeClip:
    def __init__(self, script_hash="fakehash", script_text="fake script"):
        self.script_hash = script_hash
        self.script_text = script_text


@pytest.fixture
def _reset_news_cache():
    """Reset the module-level news cache state before and after each test."""
    nc._news_cache = None
    nc._news_refresh_in_flight = False
    yield
    nc._news_cache = None
    nc._news_refresh_in_flight = False


def _news_entry(generated_at, hash_suffix="abc"):
    return {
        "generated_at": generated_at,
        "clip_hash": f"hash-{hash_suffix}",
        "script_text": "Top of the hour news...",
    }


class _ImmediateThread:
    """Fake threading.Thread that runs the target synchronously on .start()."""

    instances: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.started = False
        _ImmediateThread.instances.append(self)

    def start(self):
        self.started = True
        self.target(*self.args, **self.kwargs)


def _news_config():
    return AppConfig(station=StationConfig(name="News FM", dj_name="DJ News"))


def test_news_cache_empty_returns_none_and_queues_refresh(monkeypatch, _reset_news_cache):
    """get_news_clip never blocks on cold start — it returns None and queues
    the cache to be warmed in the background. The caller (_attach_news) treats
    None as 'skip the news segment this round'."""
    
    build_calls = []
    monkeypatch.setattr(
        "app.news_cache.build_news_clip",
        lambda config: build_calls.append(config) or _news_entry(_time_mod.time(), "new"),
    )
    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    cfg = _news_config()
    result = nc.get_news_clip(cfg)

    assert result is None
    # A background refresh was queued (and ran synchronously via the fake Thread),
    # so the NEXT call will hit a populated cache.
    assert len(_ImmediateThread.instances) == 1
    assert _ImmediateThread.instances[0].started is True
    assert build_calls == [cfg]
    assert nc._news_cache is not None  # populated by the refresh


def test_news_cache_fresh_returns_cached_without_build(monkeypatch, _reset_news_cache):
    
    cached = _news_entry(generated_at=_time_mod.time() - 60, hash_suffix="cached")
    nc._news_cache = cached

    calls = []
    monkeypatch.setattr(
        "app.news_cache.build_news_clip",
        lambda config: calls.append(config) or _news_entry(_time_mod.time(), "new"),
    )

    result = nc.get_news_clip(_news_config())

    assert result is cached
    assert calls == []


def test_news_cache_aging_returns_cached_and_spawns_refresh(monkeypatch, _reset_news_cache):
    
    cached = _news_entry(generated_at=_time_mod.time() - 25 * 60, hash_suffix="stale")
    nc._news_cache = cached

    refreshed = _news_entry(generated_at=_time_mod.time(), hash_suffix="refreshed")
    build_calls = []

    def fake_build(config):
        build_calls.append(config)
        return refreshed

    monkeypatch.setattr("app.news_cache.build_news_clip", fake_build)

    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    cfg = _news_config()
    result = nc.get_news_clip(cfg)

    # Caller gets the stale cached entry…
    assert result is cached
    # …but a background refresh ran (synchronously, via the fake thread) and
    # updated the cache for the next call.
    assert len(_ImmediateThread.instances) == 1
    assert _ImmediateThread.instances[0].started is True
    assert build_calls == [cfg]
    assert nc._news_cache is refreshed
    # The background helper clears the in-flight flag in its finally clause.
    assert nc._news_refresh_in_flight is False


def test_news_cache_aging_skips_refresh_when_already_in_flight(monkeypatch, _reset_news_cache):
    
    cached = _news_entry(generated_at=_time_mod.time() - 25 * 60, hash_suffix="stale")
    nc._news_cache = cached
    nc._news_refresh_in_flight = True

    build_calls = []
    monkeypatch.setattr(
        "app.news_cache.build_news_clip",
        lambda config: build_calls.append(config) or _news_entry(_time_mod.time(), "new"),
    )

    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    result = nc.get_news_clip(_news_config())

    assert result is cached
    assert _ImmediateThread.instances == []
    assert build_calls == []


def test_news_cache_expired_returns_none_and_queues_refresh(monkeypatch, _reset_news_cache):
    """A cache older than NEWS_EXPIRE_AFTER_S (30 min) is treated as missing —
    don't serve stale-stale news. Same path as the empty-cache case: return
    None so the segment is skipped this round, refresh in the background."""
    
    cached = _news_entry(generated_at=_time_mod.time() - 35 * 60, hash_suffix="old")
    nc._news_cache = cached

    fresh = _news_entry(generated_at=_time_mod.time(), hash_suffix="fresh")
    build_calls = []
    monkeypatch.setattr(
        "app.news_cache.build_news_clip",
        lambda config: build_calls.append(config) or fresh,
    )
    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    cfg = _news_config()
    result = nc.get_news_clip(cfg)

    # Caller gets nothing (skip this transition's news segment) …
    assert result is None
    # … but the background refresh fires immediately and warms the cache for next time.
    assert len(_ImmediateThread.instances) == 1
    assert build_calls == [cfg]
    assert nc._news_cache is fresh


def test_attach_news_blocks_on_miss_and_uses_fresh_entry(monkeypatch, _reset_news_cache):
    from app.main import _attach_news
    """On cache miss, _attach_news waits briefly on the just-spawned refresh
    rather than skipping immediately. Here the refresh runs synchronously via
    the fake Thread, so the wait returns instantly with a fresh entry."""
    
    fresh = _news_entry(_time_mod.time(), "freshly-built")
    monkeypatch.setattr("app.news_cache.build_news_clip", lambda config: fresh)
    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    cfg = _news_config()
    cfg.alerts.news.rss_url = "https://example.test/rss"
    clip_url, script_text = _attach_news(cfg)

    assert clip_url == f"/media/dj-clip/{fresh['clip_hash']}"
    assert script_text == fresh["script_text"]
    assert nc._news_cache is fresh


def test_attach_news_returns_none_when_refresh_yields_nothing(monkeypatch, _reset_news_cache):
    from app.main import _attach_news
    """RSS down / build fails — refresh completes with no entry. _attach_news
    should bail out (None, None) without waiting the full timeout."""
    
    monkeypatch.setattr("app.news_cache.build_news_clip", lambda config: None)
    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    cfg = _news_config()
    cfg.alerts.news.rss_url = "https://example.test/rss"

    t0 = _time_mod.time()
    clip_url, script_text = _attach_news(cfg)
    elapsed = _time_mod.time() - t0

    assert (clip_url, script_text) == (None, None)
    # Should return ~immediately (the in-flight flag clears synchronously via
    # the fake thread); definitely not the full NEWS_BLOCK_ON_MISS_S window.
    assert elapsed < 1.0


def test_attach_news_block_on_miss_respects_timeout(monkeypatch, _reset_news_cache):
    from app.main import _attach_news
    """If no refresh ever signals done, _wait_for_fresh_news must give up at
    the deadline rather than hanging the request forever."""
    
    # Pretend a refresh is already in flight (so _attach_news won't spawn a
    # new one) and the done event stays clear. The wait should hit timeout.
    nc._news_refresh_in_flight = True
    nc._news_refresh_done.clear()
    monkeypatch.setattr("app.main.NEWS_BLOCK_ON_MISS_S", 0.05)

    cfg = _news_config()
    cfg.alerts.news.rss_url = "https://example.test/rss"

    t0 = _time_mod.time()
    clip_url, script_text = _attach_news(cfg)
    elapsed = _time_mod.time() - t0

    assert (clip_url, script_text) == (None, None)
    assert 0.05 <= elapsed < 1.0


def test_news_cache_expired_when_refresh_already_in_flight(monkeypatch, _reset_news_cache):
    """Don't pile up parallel refreshes when one is already running."""
    
    cached = _news_entry(generated_at=_time_mod.time() - 35 * 60, hash_suffix="old")
    nc._news_cache = cached
    nc._news_refresh_in_flight = True

    build_calls = []
    monkeypatch.setattr(
        "app.news_cache.build_news_clip",
        lambda config: build_calls.append(config) or _news_entry(_time_mod.time(), "x"),
    )
    _ImmediateThread.instances = []
    monkeypatch.setattr("app.news_cache.threading.Thread", _ImmediateThread)

    result = nc.get_news_clip(_news_config())

    assert result is None  # skip this round regardless
    assert _ImmediateThread.instances == []  # no second refresh queued
    assert build_calls == []




# _build_news_clip


def test_build_news_clip_returns_none_when_script_missing(monkeypatch):
    
    monkeypatch.setattr("app.news_cache.generate_news_script", lambda config, newsreader_name=None: None)
    monkeypatch.setattr("app.news_cache.build_tts_provider", lambda cfg: None)
    monkeypatch.setattr(
        "app.news_cache.get_or_create_dj_clip",
        lambda *a, **k: pytest.fail("should not synthesize when script is None"),
    )

    assert nc.build_news_clip(_attach_config()) is None


def test_build_news_clip_happy_path(monkeypatch):
    
    monkeypatch.setattr(
        "app.news_cache.generate_news_script",
        lambda config, newsreader_name=None: "Top stories today...",
    )
    monkeypatch.setattr("app.news_cache.build_tts_provider", lambda cfg: "fake-provider")
    session = _make_db_session()
    monkeypatch.setattr("app.news_cache.SessionLocal", lambda: session)

    clip = _FakeClip(script_hash="newsclip1", script_text="Top stories today...")
    captured = {}

    def fake_create(db, **kwargs):
        captured.update(kwargs)
        return clip, "", False

    monkeypatch.setattr("app.news_cache.get_or_create_dj_clip", fake_create)

    entry = nc.build_news_clip(_attach_config())
    assert entry is not None
    assert entry["clip_hash"] == "newsclip1"
    assert entry["script_text"] == "Top stories today..."
    assert isinstance(entry["generated_at"], float)
    assert captured["clip_type"] == "news"


def test_build_news_clip_retries_with_default_voice_on_runtime_error(monkeypatch):
    
    monkeypatch.setattr(
        "app.news_cache.generate_news_script",
        lambda config, newsreader_name=None: "Top stories today...",
    )
    monkeypatch.setattr("app.news_cache.build_tts_provider", lambda cfg: "fake-provider")
    session = _make_db_session()
    monkeypatch.setattr("app.news_cache.SessionLocal", lambda: session)

    calls = []
    clip = _FakeClip(script_hash="retrynews", script_text="Top stories today...")

    def fake_create(db, **kwargs):
        calls.append(kwargs.get("voice"))
        if len(calls) == 1:
            raise RuntimeError("voice unsupported")
        return clip, "", False

    monkeypatch.setattr("app.news_cache.get_or_create_dj_clip", fake_create)

    entry = nc.build_news_clip(_attach_config())
    assert entry is not None
    assert entry["clip_hash"] == "retrynews"
    assert len(calls) == 2
    assert calls[1] is None

"""News bulletin clip cache (the always-async, background-TTL-refresh one).

News bulletins are expensive to produce (LLM + TTS, ~5 s). We keep one ready
at all times: hand back the cached clip immediately, and refresh in the
background once it crosses the "stale" threshold so the next request still
gets something recent without paying the latency. get_news_clip itself never
blocks — on miss it returns None and queues a refresh. The caller
(_attach_news in main.py) is where we trade latency for delivery: it waits
up to NEWS_BLOCK_ON_MISS_S on the just-spawned refresh before skipping, so
sparse-cadence stations don't go ages without news whenever the cache
expires between hits. The warmup that fires on player_play still seeds the
cache so the typical fast path hits the fresh branch.

This module caches the rendered bulletin *clip*; the upstream RSS headline
cache lives in news.py.

Process-local state — correct only under a single-worker server. See
TODO.md "single-worker assumption".
"""

import logging
import random
import threading
import time

from .config import AppConfig
from .database import SessionLocal
from .dj_scripts import generate_news_script
from .logging_setup import log_event
from .tts import build_tts_provider, get_or_create_dj_clip

logger = logging.getLogger(__name__)

NEWS_STALE_AFTER_S  = 20 * 60   # spawn background refresh after this
NEWS_EXPIRE_AFTER_S = 30 * 60   # past this, the cached clip is too old to serve
# When a cadence hit finds the cache empty/expired, _attach_news will briefly
# block waiting on the just-spawned refresh rather than skipping outright.
# Capped tight: this runs inside an API request, and the alternative (skip)
# is already acceptable. 8 s comfortably covers a ~5 s build with margin.
NEWS_BLOCK_ON_MISS_S = 8.0

_news_cache: dict | None = None
_news_cache_lock = threading.Lock()
_news_refresh_in_flight = False
# Cleared when a refresh starts, set when it finishes (success OR failure).
# wait_for_fresh_news uses this to wait briefly on an in-flight refresh
# before giving up.
_news_refresh_done = threading.Event()
_news_refresh_done.set()  # no refresh running at startup


def build_news_clip(config: AppConfig) -> dict | None:
    """Generate a fresh news script + TTS clip. Returns the cache entry or None."""
    news_cfg = config.alerts.news
    news_voice_cfg = random.choice(news_cfg.voices) if news_cfg.voices else None
    voice = news_voice_cfg.voice if news_voice_cfg else None
    instructions = news_voice_cfg.voice_instructions if news_voice_cfg else None
    name = news_voice_cfg.name if news_voice_cfg else None

    script = generate_news_script(config, newsreader_name=name)
    if not script:
        return None

    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))

    db = SessionLocal()
    try:
        try:
            clip, _, _ = get_or_create_dj_clip(
                db, script_text=script, voice=voice,
                voice_instructions=instructions, provider=provider,
                clip_type="news",
            )
        except RuntimeError:
            logger.warning("News clip synthesis failed with voice=%r; retrying with default", voice)
            clip, _, _ = get_or_create_dj_clip(
                db, script_text=script, voice=None, provider=provider, clip_type="news",
            )
        if clip is None:
            return None
        return {
            "generated_at": time.time(),
            "clip_hash": clip.script_hash,
            "script_text": script,
        }
    finally:
        db.close()


def _refresh_news_background(config: AppConfig) -> None:
    global _news_cache, _news_refresh_in_flight
    try:
        entry = build_news_clip(config)
        if entry:
            with _news_cache_lock:
                _news_cache = entry
            log_event("news.cache.refreshed")
    except Exception:  # noqa: BLE001
        logger.exception("Background news refresh failed")
    finally:
        with _news_cache_lock:
            _news_refresh_in_flight = False
        _news_refresh_done.set()


def _spawn_news_refresh(config: AppConfig, reason: str, age_s: int | None = None) -> None:
    """Kick off a background refresh of the news cache if one isn't already
    in flight. Lock is held just long enough to flip the in-flight flag —
    the thread is started OUTSIDE the lock so _refresh_news_background can
    re-acquire it without contending (and so a test's synchronous fake
    Thread can't deadlock here)."""
    global _news_refresh_in_flight
    should_spawn = False
    with _news_cache_lock:
        if not _news_refresh_in_flight:
            _news_refresh_in_flight = True
            should_spawn = True
            _news_refresh_done.clear()
    if should_spawn:
        threading.Thread(
            target=_refresh_news_background, args=(config,), daemon=True,
        ).start()
        fields = {"reason": reason}
        if age_s is not None:
            fields["age_s"] = age_s
        log_event("news.cache.refresh_scheduled", **fields)


def get_news_clip(config: AppConfig) -> dict | None:
    """Return a usable cached news clip, or None if the cache is missing/expired.

    Never blocks on regeneration. Strategy:
      - fresh (< NEWS_STALE_AFTER_S): return cached
      - aging (NEWS_STALE_AFTER_S .. NEWS_EXPIRE_AFTER_S): return cached AND
        spawn a background refresh
      - expired (> NEWS_EXPIRE_AFTER_S): return None AND spawn a refresh —
        the caller skips the news segment this round; next cadence has fresh
      - empty: same as expired — return None, queue a refresh

    The warmup that fires on player_play seeds the cache so the user's first
    news transition almost always hits the fresh path.
    """
    now = time.time()
    with _news_cache_lock:
        cached = _news_cache
    age = (now - cached["generated_at"]) if cached else None

    if cached is None:
        log_event("news.cache.miss", reason="empty")
        _spawn_news_refresh(config, reason="empty")
        return None

    if age > NEWS_EXPIRE_AFTER_S:
        log_event("news.cache.miss", reason="expired", age_s=round(age))
        _spawn_news_refresh(config, reason="expired", age_s=round(age))
        return None

    if age > NEWS_STALE_AFTER_S:
        _spawn_news_refresh(config, reason="stale", age_s=round(age))

    return cached


def wait_for_fresh_news(timeout_s: float) -> dict | None:
    """Block up to `timeout_s` for an in-flight news refresh to finish.

    Returns the cache entry if it lands fresh (not expired) within the
    timeout, otherwise None. Callers should only invoke this after a
    get_news_clip miss, which guarantees a refresh has been spawned (or one
    was already running) — so the wait actually has something to wait on.
    """
    deadline = time.time() + timeout_s
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            log_event("news.cache.block_on_miss.timeout", timeout_s=timeout_s)
            return None
        _news_refresh_done.wait(timeout=remaining)
        with _news_cache_lock:
            cached = _news_cache
            in_flight = _news_refresh_in_flight
        if cached:
            age = time.time() - cached["generated_at"]
            if age <= NEWS_EXPIRE_AFTER_S:
                return cached
        if not in_flight:
            # Refresh completed but yielded nothing usable (RSS down, build
            # failed). No point waiting further — the done event has been set
            # and no other thread will reset it without our spawning a new
            # refresh, which we shouldn't do from here.
            log_event("news.cache.block_on_miss.empty_after_refresh")
            return None


def invalidate() -> None:
    """Drop the cached bulletin (config changed under it)."""
    global _news_cache
    with _news_cache_lock:
        _news_cache = None

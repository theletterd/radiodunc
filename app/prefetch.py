"""DJ-clip prefetch cache (the "late-generate" half of transition latency).

After each player_next, main.py kicks off a background thread running
prefetch_dj_clip, which pre-generates the *following* DJ clip. If the user
presses Next again (or the track auto-advances), player_next finds the
cached clip via take_prefetched and skips the 1–3 s TTS round trip.

Keyed by target queue position. Cache entries are popped on use (so a stale
prefetch from a previous play session can't collide). Queue mutations and
config changes that invalidate the pending clip call clear().

Process-local state — correct only under a single-worker server. See
TODO.md "single-worker assumption".
"""

import logging
import threading
import time

from .config import load_config
from .database import SessionLocal
from .dj_scripts import active_station, generate_dj_script
from .logging_setup import log_event
from .models import Track
from .schemas import DJScriptGenerateRequest
from .tts import build_tts_provider, get_or_create_dj_clip

logger = logging.getLogger(__name__)

_prefetch_cache: dict[int, dict] = {}
_prefetch_lock = threading.Lock()


def prefetch_dj_clip(target_idx: int, queue: list, base_idx: int) -> None:
    """Background-thread job: build script + synthesize DJ clip for transition to target_idx."""
    try:
        if target_idx >= len(queue):
            return
        target_item = queue[target_idx]
        if target_item.get("type") != "track" or target_item.get("track_id") is None:
            return

        db = SessionLocal()
        try:
            config = load_config()
            previous_item = queue[base_idx] if 0 <= base_idx < len(queue) else None
            previous_track = None
            if previous_item and previous_item.get("type") == "track":
                previous_track = db.query(Track).filter(Track.id == previous_item["track_id"]).first()
            next_track = db.query(Track).filter(Track.id == target_item["track_id"]).first()
            if next_track is None:
                return

            station = active_station(config.station, config)

            def _on_cadence(every: int) -> bool:
                return every > 0 and target_idx % every == 0

            include_weather = config.alerts.weather.enabled and _on_cadence(config.alerts.weather.every_n_breaks)
            news_break_follows = config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks)
            ad_break_follows = config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks)
            # Default to "auto" because prefetch assumes a natural track-end
            # advance. If the upcoming track was queued via the search bar
            # (requested=True on the queue item), use reason="request" so the
            # generated banter actually mentions it — otherwise the DJ greets
            # a caller-requested track as if it were just another auto-advance,
            # silently dropping the request acknowledgement. The skip path
            # invalidates the prefetch cache entirely (different prompt
            # context) so we never need to handle "skip" here.
            reason = "request" if target_item.get("requested") else "auto"

            script_response = generate_dj_script(
                station,
                DJScriptGenerateRequest(
                    max_sentences=3,
                    reason=reason,
                    include_weather=include_weather,
                    news_break_follows=news_break_follows,
                    ad_break_follows=ad_break_follows,
                ),
                previous_track,
                next_track,
                config=config,
            )

            try:
                provider = build_tts_provider(config)
            except ValueError:
                provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))

            voice = station.voice or None
            instructions = station.voice_instructions or None
            t0 = time.perf_counter()
            try:
                clip, _path, dj_cached = get_or_create_dj_clip(
                    db, script_text=script_response.script_text, voice=voice, provider=provider,
                    voice_instructions=instructions, clip_type="transitions",
                )
            except RuntimeError:
                clip, _path, dj_cached = get_or_create_dj_clip(
                    db, script_text=script_response.script_text, voice=None, provider=provider,
                    clip_type="transitions",
                )
            elapsed = time.perf_counter() - t0
            logger.debug("DJ clip ready", extra={"elapsed_s": round(elapsed, 2), "cached": dj_cached})
            if clip is None:
                return

            with _prefetch_lock:
                _prefetch_cache[target_idx] = {
                    "script_text": script_response.script_text,
                    "clip_hash": clip.script_hash,
                }
            log_event("player.next.prefetched", level=logging.DEBUG, target_idx=target_idx)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("DJ clip prefetch failed for target_idx=%s", target_idx)


def take_prefetched(target_idx: int) -> dict | None:
    with _prefetch_lock:
        return _prefetch_cache.pop(target_idx, None)


def clear() -> None:
    """Drop all pending prefetched clips (queue mutated or config changed)."""
    with _prefetch_lock:
        _prefetch_cache.clear()

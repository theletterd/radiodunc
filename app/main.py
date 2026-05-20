import json
import logging
import os
import random
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from .config import AppConfig, StationConfig, load_config, save_config
from .database import Base, SessionLocal, engine, get_db
from .models import DJClip, PlayerState, Track
from .dj_scripts import (
    active_station,
    generate_ad_script,
    generate_dj_script,
    generate_news_script,
    get_station_id_phrases,
)
from .scanner import scan_library
from .schemas import (
    LibraryScanRequest,
    QueueInjectRequest,
    QueueInjectResponse,
    PlayerStateResponse,
    PlayerStateUpdateRequest,
    PlayerPlayRequest,
    PlayerActionResponse,
    PlayerNextRequest,
    DJScriptGenerateRequest,
    DJScriptResponse,
    StingerUrlResponse,
    TTSPreviewRequest,
    TTSPreviewResponse,
    StationOut,
    TrackOut,
    PlayerNextResponse,
    QueueItemOut,
    QueuePreviewResponse,
    QueueReorderRequest,
    LibraryStatusResponse,
    QueueExtendRequest,
    QueueExtendResponse,
)
from .scheduler import build_station_queue
from .tts import build_tts_provider, get_or_create_dj_clip

Base.metadata.create_all(bind=engine)


def _migrate_drop_legacy_schema() -> None:
    """Drop legacy multi-station tables and obsolete PlayerState columns."""
    with engine.begin() as conn:
        for table in ("stations", "favorite_stations", "recent_stations"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(player_state)")).fetchall()
        }
        legacy_columns = [
            "current_station_id",
            "timeline_started_at_epoch",
            "current_item_started_at_epoch",
            "current_item_expected_end_at_epoch",
            "current_sequence_id",
            "playout_mode",
        ]
        for col in legacy_columns:
            if col in columns:
                try:
                    conn.execute(text(f"ALTER TABLE player_state DROP COLUMN {col}"))
                except Exception:  # noqa: BLE001
                    pass  # SQLite < 3.35; leave the column in place

        # Add is_ad column to dj_clips if missing.
        dj_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(dj_clips)")).fetchall()}
        if "is_ad" not in dj_cols:
            conn.execute(text("ALTER TABLE dj_clips ADD COLUMN is_ad BOOLEAN NOT NULL DEFAULT 0"))


_migrate_drop_legacy_schema()

logger = logging.getLogger(__name__)

# Capture reserved fields both before and after a format call so that
# attributes set as side-effects of format() (e.g. `message`) are excluded.
_RESERVED_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys()) | {"message"}


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_LOG_RECORD_FIELDS and not key.startswith("_")
        }
        if not extras:
            return base
        extra_text = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} {extra_text}"


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = ContextFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        return

    for handler in root_logger.handlers:
        handler.setLevel(level)
        handler.setFormatter(formatter)


def _log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.log(level, "%s %s", event, details)


_configure_logging()

app = FastAPI(title="RadioDunc", version="0.3.0")
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")


@app.middleware("http")
async def log_requests(request, call_next):
    started = time.perf_counter()
    _log_event("request.start", level=logging.DEBUG, method=request.method, path=request.url.path, client=request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception("request.error method=%s path=%s elapsed_ms=%s", request.method, request.url.path, elapsed_ms)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _log_event("request.end", level=logging.DEBUG, method=request.method, path=request.url.path, status=response.status_code, elapsed_ms=elapsed_ms)
    return response


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


@app.get("/config", response_model=AppConfig)
def get_config():
    return load_config()


# Config fields whose values feed into a *generated* DJ/news/ad/stinger clip
# (script wording, TTS voice, model, key). When any of these changes, every
# in-memory artefact built against the old values is stale and must be
# rebuilt. Used by _on_config_changed below.
_GENERATION_TOPLEVEL_FIELDS = (
    "tts_provider",
    "script_provider",
    "openai_text_model",
    "openai_text_temperature",
    "openai_tts_model",
    "openai_tts_voice",
)


def _on_config_changed(old: AppConfig, new: AppConfig) -> None:
    """Invalidate caches whose contents derive from config.

    Called from update_config AFTER save_config succeeds. Comparisons are done
    on the dumped pydantic dicts so nested submodels diff cleanly. Each cache
    is keyed off the narrowest possible field set so tweaking risque_chance
    doesn't drop the news bulletin etc.

    Only the in-memory caches that bake config values into their contents are
    handled here — disk-backed caches (station-ID stinger phrases) and
    TTL-bounded caches (weather summary, 30 min) are intentionally left to
    expire naturally, since the cost of an extra LLM/HTTP round-trip is
    small and the eviction logic isn't worth the surface area.

    Failures here are logged and swallowed — a botched cache flush must NOT
    make the PUT /config call fail (the new config is already on disk).
    """
    try:
        old_d = old.model_dump()
        new_d = new.model_dump()
        old_station = old_d.get("station", {})
        new_station = new_d.get("station", {})
        old_alerts = old_d.get("alerts", {})
        new_alerts = new_d.get("alerts", {})

        generation_changed = any(
            old_d.get(f) != new_d.get(f) for f in _GENERATION_TOPLEVEL_FIELDS
        )

        # ── DJ-clip prefetch ─────────────────────────────────────────────
        # The prefetched clip was synthesised against the OLD station persona,
        # cadence settings, and TTS voice. Any change to station, alerts, or
        # generation knobs makes it stale.
        prefetch_inputs_changed = (
            generation_changed
            or old_station != new_station
            or old_alerts != new_alerts
        )
        if prefetch_inputs_changed:
            with _prefetch_lock:
                _prefetch_cache.clear()
            _log_event("config.cache.invalidated", cache="prefetch")

        # ── News bulletin cache ──────────────────────────────────────────
        # Renaming the station can leave the cached bulletin saying "the OLD
        # station name" in its intro/outro. Newsreader voices, RSS source,
        # headline count, prompt template, and any text/TTS generation knob
        # all change what the next bulletin sounds like.
        news_relevant_old = {
            "name": old_station.get("name"),
            "spoken_name": old_station.get("spoken_name"),
            "news": old_alerts.get("news"),
        }
        news_relevant_new = {
            "name": new_station.get("name"),
            "spoken_name": new_station.get("spoken_name"),
            "news": new_alerts.get("news"),
        }
        if generation_changed or news_relevant_old != news_relevant_new:
            global _news_cache
            with _news_cache_lock:
                _news_cache = None
            _log_event("config.cache.invalidated", cache="news")
    except Exception:  # noqa: BLE001
        logger.exception("Config-change cache invalidation hook raised; ignoring")


@app.put("/config", response_model=AppConfig)
def update_config(config: AppConfig):
    old_config = load_config()
    save_config(config)
    _on_config_changed(old_config, config)
    return config


@app.get("/station", response_model=StationOut)
def get_station():
    config = load_config()
    return _station_out(active_station(config.station, config))


def _track_label(track: Track) -> str:
    """Display label for a track. Falls back to filename stem when metadata is missing."""
    if track.artist and track.title:
        return f"{track.artist} - {track.title}"
    if track.artist or track.title:
        return f"{track.artist or 'Unknown'} - {track.title or 'Untitled'}"
    if track.file_path:
        return Path(track.file_path).stem
    return f"Track {track.id}"


def _station_out(station: StationConfig) -> StationOut:
    return StationOut(
        name=station.name,
        tagline=station.tagline,
        format=station.format,
        description=station.description,
        era=station.era,
        genre_focus=list(station.genre_focus),
        dj_name=station.dj_name,
        personality=station.personality,
    )


@app.post("/library/scan")
def scan_library_endpoint(payload: LibraryScanRequest, db: Session = Depends(get_db)):
    config = load_config()
    _log_event("library.scan.requested", requested_folder=payload.folder_path or "<config-default>")
    target_folder = payload.folder_path or config.music_folder
    try:
        result = scan_library(target_folder, db)
        _log_event("library.scan.completed", folder=target_folder, total_tracks=result.get("total_tracks"), new_tracks=result.get("new_tracks"))
        return {"folder_path": target_folder, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to scan library: {exc}") from exc


@app.get("/library/status", response_model=LibraryStatusResponse)
def library_status(db: Session = Depends(get_db)):
    track_count = db.query(Track).count()
    last_scan_dt = db.query(func.max(Track.created_at)).scalar()
    last_scan_at = last_scan_dt.isoformat() if last_scan_dt is not None else None
    return LibraryStatusResponse(track_count=track_count, last_scan_at=last_scan_at)


@app.get("/tracks", response_model=list[TrackOut])
def list_tracks(db: Session = Depends(get_db)):
    return db.query(Track).order_by(Track.artist.asc(), Track.album.asc(), Track.title.asc()).all()


@app.get("/library/search", response_model=list[TrackOut])
def search_library(q: str = "", db: Session = Depends(get_db)):
    if not q.strip():
        return []
    pattern = f"%{q}%"
    return (
        db.query(Track)
        .filter(Track.title.ilike(pattern) | Track.artist.ilike(pattern))
        .order_by(Track.artist.asc(), Track.title.asc())
        .limit(10)
        .all()
    )


@app.post("/player/queue/inject", response_model=QueueInjectResponse)
def queue_inject(payload: QueueInjectRequest, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == payload.track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track {payload.track_id} not found")

    state = db.query(PlayerState).order_by(PlayerState.id.asc()).first()
    if state is None or not state.queue_json:
        raise HTTPException(status_code=400, detail="No active player queue")

    queue = json.loads(state.queue_json)
    if not queue:
        raise HTTPException(status_code=400, detail="Queue is empty")

    label = _track_label(track)
    item = {"type": "track", "track_id": track.id, "label": label, "requested": True}
    insert_at = state.queue_index + 1
    queue.insert(insert_at, item)
    state.queue_json = json.dumps(queue)
    db.commit()

    with _prefetch_lock:
        _prefetch_cache.clear()

    _log_event("queue.inject", level=logging.DEBUG, track_id=track.id, position=insert_at, queue_depth=len(queue))
    return QueueInjectResponse(position=insert_at, label=label, queue_depth=len(queue))


def _get_or_create_player_state(db: Session) -> PlayerState:
    state = db.query(PlayerState).order_by(PlayerState.id.asc()).first()
    if state:
        return state
    state = PlayerState()
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def _build_player_state_response(db: Session, state: PlayerState) -> PlayerStateResponse:
    config = load_config()
    queue = json.loads(state.queue_json) if state.queue_json else []
    now = queue[state.queue_index] if queue and 0 <= state.queue_index < len(queue) else None
    current_track = None
    if state.current_track_id is not None:
        current_track = db.query(Track).filter(Track.id == state.current_track_id).first()

    return PlayerStateResponse(
        is_playing=state.is_playing,
        volume=state.volume,
        station=_station_out(active_station(config.station, config)),
        current_track=current_track,
        queue_depth=len(queue),
        queue_position=state.queue_index,
        now_playing_type=now.get("type") if now else None,
        now_playing_label=now.get("label") if now else None,
        last_error=state.last_error,
    )


@app.get("/player/status", response_model=PlayerStateResponse)
def player_status(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.get("/player/queue", response_model=QueuePreviewResponse)
def player_queue(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    current_pos = state.queue_index
    upcoming_items: list[QueueItemOut] = []
    for i in range(current_pos + 1, len(queue)):
        item = queue[i]
        if item.get("type") == "track" and item.get("track_id") is not None:
            upcoming_items.append(
                QueueItemOut(
                    position=i,
                    track_id=item["track_id"],
                    label=item.get("label", f"Track {item['track_id']}"),
                )
            )
    return QueuePreviewResponse(
        items=upcoming_items,
        queue_position=current_pos,
        queue_depth=len(queue),
    )


@app.delete("/player/queue/{position}", status_code=204)
def delete_queue_item(
    position: int,
    db: Session = Depends(get_db),
):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    if position <= state.queue_index or position >= len(queue):
        raise HTTPException(
            status_code=404,
            detail="Position out of range or refers to current/past track",
        )
    queue.pop(position)
    state.queue_json = json.dumps(queue)
    db.commit()
    with _prefetch_lock:
        _prefetch_cache.clear()


@app.post("/player/queue/reorder", status_code=204)
def reorder_queue_item(payload: QueueReorderRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    current = state.queue_index
    for pos in (payload.from_position, payload.to_position):
        if pos <= current or pos >= len(queue):
            raise HTTPException(status_code=400, detail=f"Position {pos} is out of reorderable range")
    if payload.from_position == payload.to_position:
        return
    item = queue.pop(payload.from_position)
    queue.insert(payload.to_position, item)
    state.queue_json = json.dumps(queue)
    db.commit()
    with _prefetch_lock:
        _prefetch_cache.clear()
    _log_event("queue.reorder", level=logging.DEBUG, from_position=payload.from_position, to_position=payload.to_position)


@app.post("/player/queue/extend", response_model=QueueExtendResponse)
def queue_extend(payload: QueueExtendRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No active player queue")

    queue = json.loads(state.queue_json)
    config = load_config()

    already_queued = {item["track_id"] for item in queue if item.get("track_id")}

    try:
        candidates = build_station_queue(db, config, size=payload.count * 3, seed=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_tracks = [t for t in candidates["tracks"] if t.id not in already_queued][: payload.count]

    new_items = [{"type": "track", "track_id": t.id, "label": _track_label(t)} for t in new_tracks]
    queue.extend(new_items)
    state.queue_json = json.dumps(queue)
    db.commit()

    _log_event("queue.extend", level=logging.DEBUG, added=len(new_items), queue_depth=len(queue))
    return QueueExtendResponse(added=len(new_items), queue_depth=len(queue))


def _safe_media_path(raw_path: str, config: AppConfig) -> Path:
    media_path = Path(raw_path).expanduser().resolve()
    allowed_roots = [Path(config.music_folder).expanduser().resolve(), Path("generated_audio").resolve()]
    if not any(str(media_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Media path is outside allowed roots")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return media_path


_AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}

@app.get("/media/track/{track_id}")
def media_track(track_id: int, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found")
    config = load_config()
    media_path = _safe_media_path(track.file_path, config)
    try:
        data = media_path.read_bytes()
    except OSError as exc:
        logger.warning("Track temporarily unavailable path=%s err=%s", media_path, exc)
        raise HTTPException(status_code=503, detail="Track temporarily unavailable") from exc
    media_type = _AUDIO_MEDIA_TYPES.get(media_path.suffix.lower(), "application/octet-stream")
    return Response(content=data, media_type=media_type)


@app.get("/media/dj-clip/{clip_hash}")
def media_dj_clip(clip_hash: str, db: Session = Depends(get_db)):
    clip = db.query(DJClip).filter(DJClip.script_hash == clip_hash).first()
    if clip is None:
        raise HTTPException(status_code=404, detail="DJ clip not found")
    config = load_config()
    media_path = _safe_media_path(clip.audio_path, config)
    media_type = _AUDIO_MEDIA_TYPES.get(media_path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(media_path), media_type=media_type, filename=media_path.name)


def _warm_caches_background(config: AppConfig) -> None:
    """Pre-generate the things the first transition would otherwise have to wait for.

    Spawned from player_play in a daemon thread. By the time the user gets to
    their first DJ break / news segment / ad / skip, these are ready:
      1. Station-ID phrases (idempotent — only does work on a fresh station name)
      2. News bulletin (caches for 20–30 min via get_news_clip's own TTL)
      3. At least one station-ID stinger clip in the DB pool (needed by
         /player/stinger-url for the skip-stinger to have something to play)
    """
    try:
        if config.alerts.station_id.enabled:
            phrases = get_station_id_phrases(config)
        else:
            phrases = []

        if config.alerts.news.enabled:
            get_news_clip(config)

        # Ensure the skip-stinger pool has at least one clip. Without this the
        # user's first hit on Next has no stinger to cover the dead air.
        if config.alerts.station_id.enabled and phrases:
            db = SessionLocal()
            try:
                has_stinger = (
                    db.query(DJClip)
                    .filter(DJClip.audio_path.like("%/station_ids/%"))
                    .first()
                )
                if has_stinger is None:
                    station = active_station(config.station, config)
                    voice = station.voice or None
                    try:
                        provider = build_tts_provider(config)
                    except ValueError:
                        provider = build_tts_provider(
                            config.model_copy(update={"tts_provider": "tone"})
                        )
                    sid_text = random.choice(phrases)
                    try:
                        get_or_create_dj_clip(
                            db,
                            script_text=sid_text,
                            voice=voice,
                            voice_instructions=station.voice_instructions,
                            provider=provider,
                            clip_type="station_ids",
                        )
                        _log_event("warmup.stinger_seeded", phrase=sid_text[:60])
                    except RuntimeError:
                        logger.warning("Stinger warmup TTS failed; pool will fill on first ad break instead")
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        logger.exception("Cache warmup failed")


@app.post("/player/play", response_model=PlayerActionResponse)
def player_play(payload: PlayerPlayRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.play.requested", queue_size=payload.queue_size, seed=payload.seed)
    config = load_config()

    try:
        queue = build_station_queue(db=db, config=config, size=payload.queue_size, seed=payload.seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sequence = [
        {"type": "track", "track_id": track.id, "label": _track_label(track)}
        for track in queue["tracks"]
    ]

    state.is_playing = True
    state.queue_json = json.dumps(sequence)
    state.queue_index = 0
    state.current_track_id = sequence[0].get("track_id") if sequence else None
    state.last_error = None
    db.commit()
    db.refresh(state)
    _log_event("player.play.started", queue_items=len(sequence))

    # Fire-and-forget cache warmup so the first transition (DJ/news/ad/stinger)
    # doesn't pay LLM+TTS latency. Won't block playback if it fails.
    threading.Thread(target=_warm_caches_background, args=(config,), daemon=True).start()

    return PlayerActionResponse(state=_build_player_state_response(db, state), action="play")


# ── DJ clip prefetch ──────────────────────────────────────────────────────────
# After each player_next, we kick off a background thread that pre-generates
# the *following* DJ clip. If the user presses Next again (or the track auto-
# advances), player_next finds the cached clip and skips the 1–3s TTS round trip.
#
# Key by target queue position. Cache entries are popped on use (so a stale
# prefetch from a previous play session can't collide).
_prefetch_cache: dict[int, dict] = {}
_prefetch_lock = threading.Lock()


def _prefetch_dj_clip(target_idx: int, queue: list, base_idx: int) -> None:
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

            script_response = generate_dj_script(
                station,
                DJScriptGenerateRequest(
                    max_sentences=3,
                    reason="auto",  # prefetch assumes auto-advance; skip invalidates the cache
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
            _log_event("player.next.prefetched", level=logging.DEBUG, target_idx=target_idx)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("DJ clip prefetch failed for target_idx=%s", target_idx)


def _take_prefetched(target_idx: int) -> dict | None:
    with _prefetch_lock:
        return _prefetch_cache.pop(target_idx, None)


# ── News clip cache ───────────────────────────────────────────────────────────
# News bulletins are expensive to produce (LLM + TTS, ~5 s). We keep one ready
# at all times: hand back the cached clip immediately, and refresh in the
# background once it crosses the "stale" threshold so the next request still
# gets something recent without paying the latency. get_news_clip itself never
# blocks — on miss it returns None and queues a refresh. The caller
# (_attach_news) is where we trade latency for delivery: it waits up to
# NEWS_BLOCK_ON_MISS_S on the just-spawned refresh before skipping, so
# sparse-cadence stations don't go ages without news whenever the cache
# expires between hits. The warmup that fires on player_play still seeds
# the cache so the typical fast path hits the fresh branch.
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
# _attach_news uses this to wait briefly on an in-flight refresh before giving up.
_news_refresh_done = threading.Event()
_news_refresh_done.set()  # no refresh running at startup


def _build_news_clip(config: AppConfig) -> dict | None:
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
        entry = _build_news_clip(config)
        if entry:
            with _news_cache_lock:
                _news_cache = entry
            _log_event("news.cache.refreshed")
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
        _log_event("news.cache.refresh_scheduled", **fields)


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
        _log_event("news.cache.miss", reason="empty")
        _spawn_news_refresh(config, reason="empty")
        return None

    if age > NEWS_EXPIRE_AFTER_S:
        _log_event("news.cache.miss", reason="expired", age_s=round(age))
        _spawn_news_refresh(config, reason="expired", age_s=round(age))
        return None

    if age > NEWS_STALE_AFTER_S:
        _spawn_news_refresh(config, reason="stale", age_s=round(age))

    return cached


# ── Segment attachment helpers ────────────────────────────────────────────────
# Each returns the data needed by PlayerNextResponse for one optional segment.
# They keep player_next focused on orchestration: cadence checks and assembly.


def _attach_news(config: AppConfig) -> tuple[str | None, str | None]:
    """Return (clip_url, script_text) for the news segment, or (None, None).

    On cache miss, get_news_clip has already spawned a refresh. We give that
    refresh a bounded window to finish (NEWS_BLOCK_ON_MISS_S) before falling
    back to skipping the segment. This avoids the failure mode where sparse
    news cadence + an expired cache = no news for a long stretch.
    """
    entry = get_news_clip(config)
    if not entry:
        entry = _wait_for_fresh_news(NEWS_BLOCK_ON_MISS_S)
        if not entry:
            return None, None
        _log_event("news.cache.block_on_miss.satisfied", age_s=round(time.time() - entry["generated_at"]))
    age_s = round(time.time() - entry["generated_at"])
    _log_event("player.next.news_attached", level=logging.DEBUG, source=config.alerts.news.rss_url, age_s=age_s)
    return f"/media/dj-clip/{entry['clip_hash']}", entry["script_text"]


def _wait_for_fresh_news(timeout_s: float) -> dict | None:
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
            _log_event("news.cache.block_on_miss.timeout", timeout_s=timeout_s)
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
            _log_event("news.cache.block_on_miss.empty_after_refresh")
            return None


def _attach_ad(
    db: Session, station: StationConfig, config: AppConfig, provider,
) -> tuple[str | None, str | None]:
    """Return (clip_url, script_text). script_text may be set even if clip is None."""
    ads_cfg = config.alerts.ads
    ad_clip: DJClip | None = None
    ad_script_text: str | None = None
    ad_cached = False

    pool_count = db.query(DJClip).filter(DJClip.is_ad == True).count()  # noqa: E712
    if pool_count >= ads_cfg.pool_size:
        ad_clip = random.choice(db.query(DJClip).filter(DJClip.is_ad == True).all())  # noqa: E712
        ad_script_text = ad_clip.script_text
        ad_cached = True
        _log_event("player.next.ad_pool_hit", level=logging.DEBUG, pool_count=pool_count)
    else:
        ad_script_text = generate_ad_script(station, config)
        if ad_script_text:
            ad_voice_cfg = random.choice(ads_cfg.voices) if ads_cfg.voices else None
            ad_voice = ad_voice_cfg.voice if ad_voice_cfg else None
            ad_instructions = ad_voice_cfg.voice_instructions if ad_voice_cfg else None
            try:
                ad_clip, _, ad_cached = get_or_create_dj_clip(
                    db, script_text=ad_script_text, voice=ad_voice,
                    voice_instructions=ad_instructions, provider=provider,
                    is_ad=True, clip_type="ads",
                )
            except RuntimeError:
                logger.warning("Ad clip synthesis failed with voice=%r; retrying with default", ad_voice)
                ad_clip, _, ad_cached = get_or_create_dj_clip(
                    db, script_text=ad_script_text, voice=None, provider=provider,
                    is_ad=True, clip_type="ads",
                )

    if ad_clip is None:
        return None, ad_script_text

    _log_event("player.next.ad_attached", level=logging.DEBUG, ad_cached=ad_cached, pool_count=pool_count)
    return f"/media/dj-clip/{ad_clip.script_hash}", ad_script_text


def _attach_station_id(
    db: Session, station: StationConfig, voice: str | None, config: AppConfig, provider,
) -> str | None:
    """Return the clip_url for a station-ID stinger, or None if disabled / failed."""
    if not config.alerts.station_id.enabled:
        return None
    phrases = get_station_id_phrases(config)
    if not phrases:
        return None
    sid_text = random.choice(phrases)
    try:
        sid_clip, _, sid_cached = get_or_create_dj_clip(
            db, script_text=sid_text, voice=voice,
            voice_instructions=station.voice_instructions,
            provider=provider, clip_type="station_ids",
        )
    except RuntimeError:
        logger.warning("Station ID synthesis failed with voice=%r; skipping", voice)
        return None
    if sid_clip is None:
        return None
    _log_event("player.next.station_id_attached", level=logging.DEBUG, phrase=sid_text[:60], cached=sid_cached)
    return f"/media/dj-clip/{sid_clip.script_hash}"


@app.post("/player/next", response_model=PlayerNextResponse)
def player_next(payload: PlayerNextRequest | None = None, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    reason = payload.reason if payload else None
    _log_event("player.next.requested", level=logging.DEBUG, current_index=state.queue_index, reason=reason)

    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No queue available")

    queue = json.loads(state.queue_json)
    if not queue:
        raise HTTPException(status_code=400, detail="Queue is empty")

    config = load_config()
    current_idx = state.queue_index

    previous_track: Track | None = None
    if 0 <= current_idx < len(queue):
        item = queue[current_idx]
        if item.get("type") == "track" and item.get("track_id") is not None:
            previous_track = db.query(Track).filter(Track.id == item["track_id"]).first()

    next_idx = current_idx + 1
    if next_idx >= len(queue):
        raise HTTPException(status_code=400, detail="Already at end of queue")

    next_item = queue[next_idx]
    if next_item.get("type") != "track" or next_item.get("track_id") is None:
        raise HTTPException(status_code=400, detail="Next queue item is not a track")

    next_track = db.query(Track).filter(Track.id == next_item["track_id"]).first()
    if next_track is None:
        raise HTTPException(status_code=404, detail="Next track not found in library")

    station = active_station(config.station, config)

    # Cadence: include weather/news every Nth break (queue_index proxies the break count).
    def _on_cadence(every: int) -> bool:
        return every > 0 and next_idx % every == 0

    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
    voice = station.voice or None

    # Try the prefetch cache first (skip invalidates it — different prompt context).
    cached_prefetch = _take_prefetched(next_idx) if reason != "skip" else None
    clip = None
    script_text: str | None = None
    dj_cached = False
    if cached_prefetch:
        clip = db.query(DJClip).filter(DJClip.script_hash == cached_prefetch["clip_hash"]).first()
        if clip:
            script_text = cached_prefetch["script_text"]
            dj_cached = True
            _log_event("player.next.prefetch_hit", level=logging.DEBUG, target_idx=next_idx)

    if clip is None:
        include_weather = config.alerts.weather.enabled and _on_cadence(config.alerts.weather.every_n_breaks)
        news_break_follows = config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks)
        ad_break_follows = config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks)
        effective_reason = reason
        if next_item.get("requested") and reason != "skip":
            effective_reason = "request"

        script_response = generate_dj_script(
            station,
            DJScriptGenerateRequest(
                max_sentences=3,
                reason=effective_reason,
                include_weather=include_weather,
                news_break_follows=news_break_follows,
                ad_break_follows=ad_break_follows,
            ),
            previous_track,
            next_track,
            config=config,
        )
        script_text = script_response.script_text

        instructions = station.voice_instructions or None
        t0 = time.perf_counter()
        try:
            clip, _audio_path, dj_cached = get_or_create_dj_clip(
                db, script_text=script_text, voice=voice, provider=provider,
                voice_instructions=instructions, clip_type="transitions",
            )
        except RuntimeError:
            logger.warning("DJ clip synthesis failed with voice=%r; retrying with default voice", voice)
            clip, _audio_path, dj_cached = get_or_create_dj_clip(
                db, script_text=script_text, voice=None, provider=provider, clip_type="transitions",
            )
        elapsed = time.perf_counter() - t0
        logger.debug("DJ clip ready", extra={"elapsed_s": round(elapsed, 2), "cached": dj_cached})
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize DJ clip")

    # Optional segments. Each helper is responsible for its own logging, cache
    # interaction, and error handling; cadence checks stay here.
    news_clip_url: str | None = None
    news_script_text: str | None = None
    if config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks):
        news_clip_url, news_script_text = _attach_news(config)

    ad_clip_url: str | None = None
    ad_script_text: str | None = None
    if config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks):
        ad_clip_url, ad_script_text = _attach_ad(db, station, config, provider)

    # Station ID stinger throws back to music after any non-music segment
    # (news or ad). Without one after news, the bulletin runs straight into
    # the next track which feels jarring; the stinger acts as a soft handoff.
    station_id_clip_url: str | None = None
    if ad_clip_url or news_clip_url:
        station_id_clip_url = _attach_station_id(db, station, voice, config, provider)

    state.queue_index = next_idx
    state.current_track_id = next_track.id
    db.commit()

    look_ahead_track: Track | None = None
    look_ahead_idx = next_idx + 1
    if look_ahead_idx < len(queue):
        la_item = queue[look_ahead_idx]
        if la_item.get("type") == "track" and la_item.get("track_id") is not None:
            look_ahead_track = db.query(Track).filter(Track.id == la_item["track_id"]).first()

    _log_event(
        "player.next.completed",
        level=logging.DEBUG,
        new_index=next_idx,
        track_id=next_track.id,
        dj_cached=dj_cached,
        ad_attached=bool(ad_clip_url),
    )

    return PlayerNextResponse(
        current_track_url=f"/media/track/{next_track.id}",
        current_track_metadata=TrackOut.model_validate(next_track),
        current_track_label=_track_label(next_track),
        dj_clip_url=f"/media/dj-clip/{clip.script_hash}",
        ad_clip_url=ad_clip_url,
        ad_script=ad_script_text,
        news_clip_url=news_clip_url,
        news_script=news_script_text,
        station_id_clip_url=station_id_clip_url,
        next_track_url=f"/media/track/{look_ahead_track.id}" if look_ahead_track else None,
        next_track_metadata=TrackOut.model_validate(look_ahead_track) if look_ahead_track else None,
        dj_script=script_text or "",
    )


@app.post("/player/prefetch", status_code=202)
def player_prefetch(db: Session = Depends(get_db)):
    """Called by the client ~20 s before a track ends to pre-generate the next DJ clip."""
    state = _get_or_create_player_state(db)
    if not state.is_playing or not state.queue_json:
        return {"status": "idle"}
    queue = json.loads(state.queue_json)
    current_idx = state.queue_index
    prefetch_target = current_idx + 1
    if prefetch_target >= len(queue):
        return {"status": "end_of_queue"}
    threading.Thread(
        target=_prefetch_dj_clip,
        args=(prefetch_target, list(queue), current_idx),
        daemon=True,
    ).start()
    _log_event("player.prefetch.requested", level=logging.DEBUG, target_idx=prefetch_target)
    return {"status": "scheduled"}


@app.get("/player/stinger-url", response_model=StingerUrlResponse)
def player_stinger_url(db: Session = Depends(get_db)):
    """Return a random cached station-ID clip URL for the client to play during
    the dead-air gap after a user-initiated skip. No LLM/TTS work — just a DB pick."""
    clip = (
        db.query(DJClip)
        .filter(DJClip.audio_path.like("%/station_ids/%"))
        .order_by(func.random())
        .first()
    )
    if clip is None:
        return StingerUrlResponse()
    return StingerUrlResponse(clip_url=f"/media/dj-clip/{clip.script_hash}")


@app.post("/tts/preview", response_model=TTSPreviewResponse)
def tts_preview(payload: TTSPreviewRequest, db: Session = Depends(get_db)):
    """Synthesise an arbitrary sample line for previewing a voice + instructions.

    Reuses the same get_or_create_dj_clip cache, so identical (text, voice,
    instructions) triples produce one clip and replay instantly thereafter.
    Stored under generated_audio/previews/ to keep them separate from the
    on-air pools.
    """
    config = load_config()
    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
    try:
        clip, _, _ = get_or_create_dj_clip(
            db,
            script_text=payload.text,
            voice=payload.voice,
            voice_instructions=payload.voice_instructions,
            provider=provider,
            clip_type="previews",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"TTS provider failed: {exc}") from exc
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize preview clip")
    return TTSPreviewResponse(clip_url=f"/media/dj-clip/{clip.script_hash}")


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.stop.requested")
    state.is_playing = False
    db.commit()
    db.refresh(state)
    _log_event("player.stop.completed")
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="stop")


@app.get("/player/state", response_model=PlayerStateResponse)
def get_player_state(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.put("/player/state", response_model=PlayerStateResponse)
def update_player_state(payload: PlayerStateUpdateRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    if payload.is_playing is not None:
        state.is_playing = payload.is_playing
    if payload.volume is not None:
        state.volume = payload.volume
    db.commit()
    db.refresh(state)
    return _build_player_state_response(db, state)



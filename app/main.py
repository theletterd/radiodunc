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
from .dj_scripts import active_station, generate_ad_script, generate_dj_script, generate_news_script
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
    DJClipSynthesizeRequest,
    DJClipResponse,
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


def _log_event(event: str, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    logger.info("%s %s", event, details)


_configure_logging()

app = FastAPI(title="RadioDunc", version="0.3.0")
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")


@app.middleware("http")
async def log_requests(request, call_next):
    started = time.perf_counter()
    _log_event("request.start", method=request.method, path=request.url.path, client=request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception("request.error method=%s path=%s elapsed_ms=%s", request.method, request.url.path, elapsed_ms)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _log_event("request.end", method=request.method, path=request.url.path, status=response.status_code, elapsed_ms=elapsed_ms)
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


@app.put("/config", response_model=AppConfig)
def update_config(config: AppConfig):
    save_config(config)
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
        dj_style=station.dj_style,
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

    _log_event("queue.inject", track_id=track.id, position=insert_at, queue_depth=len(queue))
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
    _log_event("queue.reorder", from_position=payload.from_position, to_position=payload.to_position)


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

    _log_event("queue.extend", added=len(new_items), queue_depth=len(queue))
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
    return FileResponse(str(media_path), media_type="audio/wav", filename=media_path.name)


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
                    voice_instructions=instructions,
                )
            except RuntimeError:
                clip, _path, dj_cached = get_or_create_dj_clip(
                    db, script_text=script_response.script_text, voice=None, provider=provider,
                )
            elapsed = time.perf_counter() - t0
            logger.info("DJ clip ready", extra={"elapsed_s": round(elapsed, 2), "cached": dj_cached})
            if clip is None:
                return

            with _prefetch_lock:
                _prefetch_cache[target_idx] = {
                    "script_text": script_response.script_text,
                    "clip_hash": clip.script_hash,
                }
            _log_event("player.next.prefetched", target_idx=target_idx)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("DJ clip prefetch failed for target_idx=%s", target_idx)


def _take_prefetched(target_idx: int) -> dict | None:
    with _prefetch_lock:
        return _prefetch_cache.pop(target_idx, None)


@app.post("/player/next", response_model=PlayerNextResponse)
def player_next(payload: PlayerNextRequest | None = None, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    reason = payload.reason if payload else None
    _log_event("player.next.requested", current_index=state.queue_index, reason=reason)

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
            _log_event("player.next.prefetch_hit", target_idx=next_idx)

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
                voice_instructions=instructions,
            )
        except RuntimeError:
            logger.warning("DJ clip synthesis failed with voice=%r; retrying with default voice", voice)
            clip, _audio_path, dj_cached = get_or_create_dj_clip(
                db, script_text=script_text, voice=None, provider=provider,
            )
        elapsed = time.perf_counter() - t0
        logger.info("DJ clip ready", extra={"elapsed_s": round(elapsed, 2), "cached": dj_cached})
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize DJ clip")

    # Optional news clip — always fresh (no caching, news goes stale fast).
    news_clip_url: str | None = None
    news_script_text: str | None = None
    if config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks):
        news_cfg = config.alerts.news
        news_script_text = generate_news_script(config)
        if news_script_text:
            news_voice_cfg = random.choice(news_cfg.voices) if news_cfg.voices else None
            news_voice = news_voice_cfg.voice if news_voice_cfg else None
            news_instructions = news_voice_cfg.voice_instructions if news_voice_cfg else None
            try:
                news_clip, _, _ = get_or_create_dj_clip(
                    db,
                    script_text=news_script_text,
                    voice=news_voice,
                    voice_instructions=news_instructions,
                    provider=provider,
                )
                news_clip_url = f"/media/dj-clip/{news_clip.script_hash}"
                _log_event("player.next.news_attached", source=config.alerts.news.rss_url)
            except RuntimeError:
                logger.warning("News clip synthesis failed with voice=%r; skipping", news_voice)

    # Optional ad clip: serve from pool or generate a new one.
    ad_clip_url: str | None = None
    ad_script_text: str | None = None
    if config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks):
        ads_cfg = config.alerts.ads
        ad_clip: DJClip | None = None
        ad_cached = False

        pool_count = db.query(DJClip).filter(DJClip.is_ad == True).count()  # noqa: E712
        if pool_count >= ads_cfg.pool_size:
            # Pool is full — pick a random existing ad clip.
            all_ad_clips = db.query(DJClip).filter(DJClip.is_ad == True).all()  # noqa: E712
            ad_clip = random.choice(all_ad_clips)
            ad_script_text = ad_clip.script_text
            ad_cached = True
            _log_event("player.next.ad_pool_hit", pool_count=pool_count)
        else:
            # Generate a new ad clip and add it to the pool.
            ad_script_text = generate_ad_script(station, config)
            if ad_script_text:
                ad_voice_cfg = random.choice(ads_cfg.voices) if ads_cfg.voices else None
                ad_voice = ad_voice_cfg.voice if ad_voice_cfg else None
                ad_instructions = ad_voice_cfg.voice_instructions if ad_voice_cfg else None
                try:
                    ad_clip, _ad_path, ad_cached = get_or_create_dj_clip(
                        db,
                        script_text=ad_script_text,
                        voice=ad_voice,
                        voice_instructions=ad_instructions,
                        provider=provider,
                        is_ad=True,
                    )
                except RuntimeError:
                    logger.warning("Ad clip synthesis failed with voice=%r; retrying with default", ad_voice)
                    ad_clip, _ad_path, ad_cached = get_or_create_dj_clip(
                        db, script_text=ad_script_text, voice=None, provider=provider, is_ad=True,
                    )

        if ad_clip is not None:
            ad_clip_url = f"/media/dj-clip/{ad_clip.script_hash}"
            _log_event("player.next.ad_attached", ad_cached=ad_cached, pool_count=pool_count)

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
        new_index=next_idx,
        track_id=next_track.id,
        dj_cached=dj_cached,
        ad_attached=bool(ad_clip_url),
    )

    # Fire-and-forget: pre-generate the DJ clip for the next transition.
    prefetch_target = next_idx + 1
    if prefetch_target < len(queue):
        threading.Thread(
            target=_prefetch_dj_clip,
            args=(prefetch_target, list(queue), next_idx),
            daemon=True,
        ).start()

    return PlayerNextResponse(
        current_track_url=f"/media/track/{next_track.id}",
        current_track_metadata=TrackOut.model_validate(next_track),
        current_track_label=_track_label(next_track),
        dj_clip_url=f"/media/dj-clip/{clip.script_hash}",
        ad_clip_url=ad_clip_url,
        ad_script=ad_script_text,
        news_clip_url=news_clip_url,
        news_script=news_script_text,
        next_track_url=f"/media/track/{look_ahead_track.id}" if look_ahead_track else None,
        next_track_metadata=TrackOut.model_validate(look_ahead_track) if look_ahead_track else None,
        dj_script=script_text or "",
    )


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


@app.post("/dj-script", response_model=DJScriptResponse)
def generate_dj_script_endpoint(payload: DJScriptGenerateRequest, db: Session = Depends(get_db)):
    config = load_config()

    previous_track = None
    if payload.previous_track_id is not None:
        previous_track = db.query(Track).filter(Track.id == payload.previous_track_id).first()
        if not previous_track:
            raise HTTPException(status_code=404, detail=f"Track {payload.previous_track_id} not found")

    next_track = None
    if payload.next_track_id is not None:
        next_track = db.query(Track).filter(Track.id == payload.next_track_id).first()
        if not next_track:
            raise HTTPException(status_code=404, detail=f"Track {payload.next_track_id} not found")

    return generate_dj_script(active_station(config.station, config), payload, previous_track, next_track, config=config)


@app.post("/dj-clip", response_model=DJClipResponse)
def synthesize_dj_clip(payload: DJClipSynthesizeRequest, db: Session = Depends(get_db)):
    config = load_config()
    try:
        provider = build_tts_provider(config)
    except ValueError as exc:
        logger.warning("Invalid OpenAI TTS config during clip synthesis; falling back to tone", extra={"error": str(exc)})
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
    clip, audio_path, cached = get_or_create_dj_clip(db, payload.script_text, payload.voice, provider=provider)
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to persist DJ clip")
    return DJClipResponse(clip_id=clip.id, audio_path=audio_path, voice=clip.voice, cached=cached)

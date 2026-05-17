import json
import logging
import os
import time
import hashlib
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from .config import AppConfig, load_config, save_config
from .database import Base, engine, get_db
from .models import DJClip, FavoriteStation, PlayerState, RecentStation, Station, Track
from .dj_scripts import generate_dj_script
from .scanner import scan_library
from .schemas import (
    LibraryScanRequest,
    QueueGenerateRequest,
    QueueResponse,
    PlayerStateResponse,
    PlayerStateUpdateRequest,
    PlayerPlayRequest,
    PlayerActionResponse,
    FavoriteStationRequest,
    DJScriptGenerateRequest,
    DJScriptResponse,
    DJClipSynthesizeRequest,
    DJClipResponse,
    StationGenerateRequest,
    StationOut,
    TrackOut,
    PlayerNextResponse,
    QueueItemOut,
    QueuePreviewResponse,
)
from .scheduler import build_station_queue
from .stations import generate_stations
from .tts import build_tts_provider, get_or_create_dj_clip

Base.metadata.create_all(bind=engine)


def _ensure_player_state_schema() -> None:
    with engine.begin() as conn:
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(player_state)")).fetchall()
        }
        missing_columns = {
            "timeline_started_at_epoch": "ALTER TABLE player_state ADD COLUMN timeline_started_at_epoch FLOAT NOT NULL DEFAULT 0.0",
            "current_item_started_at_epoch": "ALTER TABLE player_state ADD COLUMN current_item_started_at_epoch FLOAT NOT NULL DEFAULT 0.0",
            "current_item_expected_end_at_epoch": "ALTER TABLE player_state ADD COLUMN current_item_expected_end_at_epoch FLOAT NOT NULL DEFAULT 0.0",
            "current_sequence_id": "ALTER TABLE player_state ADD COLUMN current_sequence_id INTEGER NOT NULL DEFAULT 0",
            "playout_mode": "ALTER TABLE player_state ADD COLUMN playout_mode VARCHAR NOT NULL DEFAULT 'stopped'",
        }
        for column_name, ddl in missing_columns.items():
            if column_name not in columns:
                conn.execute(text(ddl))
        conn.execute(
            text(
                """
                UPDATE player_state
                SET timeline_started_at_epoch = COALESCE(timeline_started_at_epoch, 0.0),
                    current_item_started_at_epoch = COALESCE(current_item_started_at_epoch, 0.0),
                    current_item_expected_end_at_epoch = COALESCE(current_item_expected_end_at_epoch, 0.0),
                    current_sequence_id = COALESCE(current_sequence_id, 0),
                    playout_mode = COALESCE(NULLIF(playout_mode, ''), 'stopped')
                """
            )
        )


_ensure_player_state_schema()

logger = logging.getLogger(__name__)

_admin_auth_disabled_logged = False

_RESERVED_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__.keys())


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

app = FastAPI(title="Local AI Radio Station Generator", version="0.2.0")
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


@app.get("/tracks", response_model=list[TrackOut])
def list_tracks(db: Session = Depends(get_db)):
    return db.query(Track).order_by(Track.artist.asc(), Track.album.asc(), Track.title.asc()).all()


@app.post("/stations/generate")
def generate_stations_endpoint(payload: StationGenerateRequest, db: Session = Depends(get_db)):
    try:
        result = generate_stations(db, load_config(), payload.count)
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/stations", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    return db.query(Station).order_by(Station.created_at.desc()).all()


@app.post("/stations/{station_id}/queue", response_model=QueueResponse)
def generate_station_queue(station_id: int, payload: QueueGenerateRequest, db: Session = Depends(get_db)):
    try:
        return build_station_queue(
            db=db,
            station_id=station_id,
            config=load_config(),
            size=payload.size,
            seed=payload.seed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    station = None
    if state.current_station_id is not None:
        station = db.query(Station).filter(Station.id == state.current_station_id).first()
    favorites = [row.station_id for row in db.query(FavoriteStation).order_by(FavoriteStation.created_at.desc()).all()]
    recent_station_ids = [
        row.station_id for row in db.query(RecentStation).order_by(RecentStation.played_at.desc(), RecentStation.id.desc()).limit(10).all()
    ]
    queue = json.loads(state.queue_json) if state.queue_json else []
    now = queue[state.queue_index] if queue and 0 <= state.queue_index < len(queue) else None
    current_track = None
    if state.current_track_id is not None:
        current_track = db.query(Track).filter(Track.id == state.current_track_id).first()

    return PlayerStateResponse(
        station_id=state.current_station_id,
        is_playing=state.is_playing,
        volume=state.volume,
        station=station,
        favorites=favorites,
        recent_station_ids=recent_station_ids,
        current_track=current_track,
        queue_depth=len(queue),
        queue_position=state.queue_index,
        now_playing_type=now.get("type") if now else None,
        now_playing_label=now.get("label") if now else None,
        last_error=state.last_error,
        timeline_started_at_epoch=state.timeline_started_at_epoch,
        current_item_started_at_epoch=state.current_item_started_at_epoch,
        current_item_expected_end_at_epoch=state.current_item_expected_end_at_epoch,
        current_sequence_id=state.current_sequence_id,
        playout_mode=state.playout_mode,
    )


def _require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    global _admin_auth_disabled_logged
    expected = os.getenv("ADMIN_API_TOKEN")
    if not expected:
        if not _admin_auth_disabled_logged:
            logger.warning("admin.auth.disabled reason=missing_env ADMIN_API_TOKEN")
            _admin_auth_disabled_logged = True
        return
    if not x_admin_token:
        raise HTTPException(status_code=403, detail="Missing X-Admin-Token header")
    if not hashlib.sha256(x_admin_token.encode()).digest() == hashlib.sha256(expected.encode()).digest():
        raise HTTPException(status_code=403, detail="Admin authentication failed")


@app.get("/player/status", response_model=PlayerStateResponse)
def player_status(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.get("/player/queue", response_model=QueuePreviewResponse)
def player_queue(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    current_pos = state.queue_index
    upcoming_items: list[QueueItemOut] = []
    for i in range(current_pos + 1, min(current_pos + 6, len(queue))):
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
    _admin: None = Depends(_require_admin),
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


def _safe_media_path(raw_path: str, config: AppConfig) -> Path:
    media_path = Path(raw_path).expanduser().resolve()
    allowed_roots = [Path(config.music_folder).expanduser().resolve(), Path("generated_audio").resolve()]
    if not any(str(media_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Media path is outside allowed roots")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return media_path


@app.get("/player/current-media")
def player_current_media(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    if not queue or not (0 <= state.queue_index < len(queue)):
        raise HTTPException(status_code=404, detail="No active queue item")

    item = queue[state.queue_index]
    config = load_config()
    if item.get("type") == "track":
        track_id = item.get("track_id")
        track = db.query(Track).filter(Track.id == track_id).first() if track_id is not None else None
        if track is None:
            raise HTTPException(status_code=404, detail="Current track not found")
        media_path = _safe_media_path(track.file_path, config)
        return FileResponse(str(media_path), filename=(track.title or "track") + media_path.suffix)

    if item.get("type") == "dj":
        script_text = item.get("script_text") or item.get("label") or "Station ID"
        voice = item.get("voice")
        try:
            provider = build_tts_provider(config)
        except ValueError as exc:
            logger.warning("Invalid OpenAI TTS config during playback; falling back to tone", extra={"error": str(exc)})
            provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
        clip, audio_path, _cached = get_or_create_dj_clip(
            db,
            script_text=script_text,
            voice=voice,
            provider=provider,
        )
        media_path = _safe_media_path(audio_path, config)

        return FileResponse(str(media_path), filename=media_path.name)

    raise HTTPException(status_code=409, detail="Current queue item has unsupported type")


@app.get("/media/track/{track_id}")
def media_track(track_id: int, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found")
    config = load_config()
    media_path = _safe_media_path(track.file_path, config)
    return FileResponse(str(media_path), filename=(track.title or "track") + media_path.suffix)


@app.get("/media/dj-clip/{clip_hash}")
def media_dj_clip(clip_hash: str, db: Session = Depends(get_db)):
    clip = db.query(DJClip).filter(DJClip.script_hash == clip_hash).first()
    if clip is None:
        raise HTTPException(status_code=404, detail="DJ clip not found")
    config = load_config()
    media_path = _safe_media_path(clip.audio_path, config)
    return FileResponse(str(media_path), media_type="audio/wav", filename=media_path.name)


@app.post("/player/play", response_model=PlayerActionResponse)
def player_play(payload: PlayerPlayRequest, db: Session = Depends(get_db), _admin: None = Depends(_require_admin)):
    state = _get_or_create_player_state(db)
    _log_event("player.play.requested", station_id=payload.station_id, queue_size=payload.queue_size, seed=payload.seed)
    config = load_config()
    station = db.query(Station).filter(Station.id == payload.station_id).first()
    if not station:
        raise HTTPException(status_code=404, detail=f"Station {payload.station_id} not found")

    queue = build_station_queue(db=db, station_id=payload.station_id, config=config, size=payload.queue_size, seed=payload.seed)
    sequence = [
        {"type": "track", "track_id": track.id, "label": f"{track.artist or 'Unknown'} - {track.title or 'Untitled'}"}
        for track in queue["tracks"]
    ]

    state.current_station_id = payload.station_id
    state.is_playing = True
    state.queue_json = json.dumps(sequence)
    state.queue_index = 0
    state.current_track_id = sequence[0].get("track_id") if sequence else None
    now_epoch = time.time()
    state.timeline_started_at_epoch = now_epoch
    state.current_item_started_at_epoch = now_epoch
    state.current_item_expected_end_at_epoch = now_epoch
    state.current_sequence_id = (state.current_sequence_id or 0) + 1
    state.playout_mode = "live" if sequence else "recovering"
    state.last_error = None
    db.commit()
    db.refresh(state)
    _log_event("player.play.started", station_id=payload.station_id, queue_items=len(sequence))
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="play")


@app.post("/player/next", response_model=PlayerNextResponse)
def player_next(db: Session = Depends(get_db), _admin: None = Depends(_require_admin)):
    state = _get_or_create_player_state(db)
    _log_event("player.next.requested", current_index=state.queue_index)

    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No queue available")

    queue = json.loads(state.queue_json)
    if not queue:
        raise HTTPException(status_code=400, detail="Queue is empty")

    config = load_config()
    current_idx = state.queue_index

    # Track being left behind — back-announced in the DJ script
    previous_track: Track | None = None
    if 0 <= current_idx < len(queue):
        item = queue[current_idx]
        if item.get("type") == "track" and item.get("track_id") is not None:
            previous_track = db.query(Track).filter(Track.id == item["track_id"]).first()

    # Advance to the next queue slot
    next_idx = current_idx + 1
    if next_idx >= len(queue):
        raise HTTPException(status_code=400, detail="Already at end of queue")

    next_item = queue[next_idx]
    if next_item.get("type") != "track" or next_item.get("track_id") is None:
        raise HTTPException(status_code=400, detail="Next queue item is not a track")

    next_track = db.query(Track).filter(Track.id == next_item["track_id"]).first()
    if next_track is None:
        raise HTTPException(status_code=404, detail="Next track not found in library")

    # Require an active station for DJ script generation
    if state.current_station_id is None:
        raise HTTPException(status_code=400, detail="No active station")
    station = db.query(Station).filter(Station.id == state.current_station_id).first()
    if station is None:
        raise HTTPException(status_code=400, detail="Active station not found")

    # Generate transition script: back-announce previous, intro next
    script_response = generate_dj_script(
        station,
        DJScriptGenerateRequest(max_sentences=3),
        previous_track,
        next_track,
        config=config,
    )

    # Synthesize (or return cached) DJ clip
    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))

    station_config = json.loads(station.config_json) if station.config_json else {}
    voice = station_config.get("voice_hint") or None
    try:
        clip, _audio_path, dj_cached = get_or_create_dj_clip(
            db,
            script_text=script_response.script_text,
            voice=voice,
            provider=provider,
        )
    except RuntimeError:
        logger.warning(
            "DJ clip synthesis failed with voice=%r; retrying with default voice", voice
        )
        clip, _audio_path, dj_cached = get_or_create_dj_clip(
            db,
            script_text=script_response.script_text,
            voice=None,
            provider=provider,
        )
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize DJ clip")

    # Advance queue state
    state.queue_index = next_idx
    state.current_track_id = next_track.id
    state.playout_mode = "live"
    db.commit()

    # Look ahead one more slot for prefetch
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
    )
    return PlayerNextResponse(
        current_track_url=f"/media/track/{next_track.id}",
        dj_clip_url=f"/media/dj-clip/{clip.script_hash}",
        next_track_url=f"/media/track/{look_ahead_track.id}" if look_ahead_track else None,
        next_track_metadata=TrackOut.model_validate(look_ahead_track) if look_ahead_track else None,
        dj_script=script_response.script_text,
    )


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.stop.requested", station_id=state.current_station_id)
    state.is_playing = False
    state.playout_mode = "stopped"
    db.commit()
    db.refresh(state)
    _log_event("player.stop.completed", station_id=state.current_station_id)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="stop")


@app.get("/player/state", response_model=PlayerStateResponse)
def get_player_state(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.put("/player/state", response_model=PlayerStateResponse)
def update_player_state(payload: PlayerStateUpdateRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    if payload.station_id is not None:
        station = db.query(Station).filter(Station.id == payload.station_id).first()
        if not station:
            raise HTTPException(status_code=404, detail=f"Station {payload.station_id} not found")
        state.current_station_id = payload.station_id
        db.query(RecentStation).filter(RecentStation.station_id == payload.station_id).delete()
        db.add(RecentStation(station_id=payload.station_id))
    if payload.is_playing is not None:
        state.is_playing = payload.is_playing
        if payload.is_playing:
            state.playout_mode = "live"
        else:
            state.playout_mode = "stopped"
    if payload.volume is not None:
        state.volume = payload.volume
    db.commit()
    db.refresh(state)
    return _build_player_state_response(db, state)


@app.put("/stations/{station_id}/favorite")
def set_favorite_station(station_id: int, payload: FavoriteStationRequest, db: Session = Depends(get_db)):
    station = db.query(Station).filter(Station.id == station_id).first()
    if not station:
        raise HTTPException(status_code=404, detail=f"Station {station_id} not found")

    existing = db.query(FavoriteStation).filter(FavoriteStation.station_id == station_id).first()
    if payload.favorite and not existing:
        db.add(FavoriteStation(station_id=station_id))
    elif not payload.favorite and existing:
        db.delete(existing)
    db.commit()
    return {"station_id": station_id, "favorite": payload.favorite}


@app.post("/stations/{station_id}/dj-script", response_model=DJScriptResponse)
def generate_station_dj_script(station_id: int, payload: DJScriptGenerateRequest, db: Session = Depends(get_db)):
    station = db.query(Station).filter(Station.id == station_id).first()
    if not station:
        raise HTTPException(status_code=404, detail=f"Station {station_id} not found")

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

    return generate_dj_script(station, payload, previous_track, next_track, config=load_config())


@app.post("/stations/{station_id}/dj-clip", response_model=DJClipResponse)
def synthesize_station_dj_clip(station_id: int, payload: DJClipSynthesizeRequest, db: Session = Depends(get_db)):
    station = db.query(Station).filter(Station.id == station_id).first()
    if not station:
        raise HTTPException(status_code=404, detail=f"Station {station_id} not found")

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

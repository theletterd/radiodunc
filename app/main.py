import json
import logging
import os
import time
import hashlib
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text

from .config import AppConfig, load_config, save_config
from .database import Base, engine, get_db
from .models import DJClip, FavoriteStation, ListenerSession, PlayerState, RecentStation, Station, Track
from .dj_scripts import generate_dj_script
from .scanner import scan_library
from .broadcast import BroadcastEngine
from .schemas import (
    LibraryScanRequest,
    QueueGenerateRequest,
    QueueResponse,
    PlayerStateResponse,
    PlayerStateUpdateRequest,
    PlayerPlayRequest,
    PlayerActionResponse,
    PlayerAdminCommandRequest,
    FavoriteStationRequest,
    DJScriptGenerateRequest,
    DJScriptResponse,
    DJClipSynthesizeRequest,
    DJClipResponse,
    StationGenerateRequest,
    StationOut,
    TrackOut,
    BroadcastStatusResponse,
    ListenerHeartbeatRequest,
    ListenerHeartbeatResponse,
)
from .scheduler import build_station_queue
from .stations import generate_stations
from .tts import build_tts_provider, get_or_create_dj_clip
from .playout_worker import PlayoutWorker

Base.metadata.create_all(bind=engine)

_last_manifest_stale_log_at_epoch: float = 0.0


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
            "admin_commands_json": "ALTER TABLE player_state ADD COLUMN admin_commands_json TEXT NOT NULL DEFAULT '[]'",
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
                    playout_mode = COALESCE(NULLIF(playout_mode, ''), 'stopped'),
                    admin_commands_json = COALESCE(NULLIF(admin_commands_json, ''), '[]')
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

broadcast_engine = BroadcastEngine(Path("generated_audio") / "hls")
playout_worker = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    playout_worker.start()
    try:
        yield
    finally:
        playout_worker.stop()


app = FastAPI(title="Local AI Radio Station Generator", version="0.2.0", lifespan=lifespan)
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



def _daypart_greeting(config: AppConfig) -> str:
    try:
        zone = ZoneInfo(config.alerts.local_time_zone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    hour = datetime.now(zone).hour
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    if 17 <= hour < 22:
        return "Good evening"
    return "Late night vibes"


def _local_time_announcement(config: AppConfig) -> str:
    try:
        zone = ZoneInfo(config.alerts.local_time_zone)
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    stamp = datetime.now(zone).strftime("%-I:%M%p").lower()
    return f"It's {stamp}"


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


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


def _active_listener_count(db: Session, *, now_epoch: float | None = None, active_window_seconds: float = 30.0) -> int:
    now_epoch = now_epoch or time.time()
    min_last_seen = now_epoch - active_window_seconds
    return db.query(ListenerSession).filter(ListenerSession.last_seen_at_epoch >= min_last_seen).count()



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


def _enqueue_admin_command(state: PlayerState, payload: PlayerAdminCommandRequest) -> dict:
    commands = json.loads(state.admin_commands_json) if state.admin_commands_json else []
    command = {
        "command": payload.command,
        "station_id": payload.station_id,
        "queue_size": payload.queue_size,
        "seed": payload.seed,
        "metadata": payload.metadata or {},
        "requested_at_epoch": time.time(),
    }
    commands.append(command)
    state.admin_commands_json = json.dumps(commands)
    return command

@app.get("/player/status", response_model=PlayerStateResponse)
def player_status(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.post("/listeners/heartbeat", response_model=ListenerHeartbeatResponse)
def listeners_heartbeat(payload: ListenerHeartbeatRequest, db: Session = Depends(get_db)):
    now_epoch = time.time()
    listener = db.query(ListenerSession).filter(ListenerSession.session_id == payload.session_id).first()
    if listener is None:
        listener = ListenerSession(session_id=payload.session_id, last_seen_at_epoch=now_epoch)
        db.add(listener)
    else:
        listener.last_seen_at_epoch = now_epoch
    db.commit()
    db.refresh(listener)

    return ListenerHeartbeatResponse(
        session_id=listener.session_id,
        last_seen_at_epoch=listener.last_seen_at_epoch,
        active_listener_count=_active_listener_count(db, now_epoch=now_epoch),
    )


def _safe_media_path(raw_path: str, config: AppConfig) -> Path:
    media_path = Path(raw_path).expanduser().resolve()
    allowed_roots = [Path(config.music_folder).expanduser().resolve(), Path("generated_audio").resolve()]
    if not any(str(media_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Media path is outside allowed roots")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return media_path


if playout_worker is None:
    playout_worker = PlayoutWorker(tick_seconds=0.3, broadcast_engine=broadcast_engine, safe_media_path=_safe_media_path)


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
        is_ad_break = bool(item.get("is_ad_break"))
        clip, audio_path, _cached = get_or_create_dj_clip(
            db,
            script_text=script_text,
            voice=voice,
            provider=provider,
            persist=is_ad_break,
        )
        media_path = _safe_media_path(audio_path, config)

        return FileResponse(str(media_path), filename=media_path.name)

    raise HTTPException(status_code=409, detail="Current queue item has unsupported type")




@app.get("/player/stream")
def player_stream(db: Session = Depends(get_db)):
    """Transitional single-audio endpoint for frontend playback."""
    return player_current_media(db)


@app.get("/broadcast/status", response_model=BroadcastStatusResponse)
def broadcast_status():
    return broadcast_engine.status()


@app.get("/broadcast/live.m3u8")
def broadcast_live_manifest(request: Request):
    manifest = broadcast_engine.manifest_path()
    range_header = request.headers.get("range")
    status = broadcast_engine.status()
    if not status.running:
        _log_event("broadcast.manifest.unavailable", path=str(manifest), reason="engine_not_running", range=range_header)
        raise HTTPException(status_code=503, detail="Live stream encoder is not running")
    if not manifest.exists():
        _log_event("broadcast.manifest.miss", path=str(manifest), range=range_header)
        raise HTTPException(status_code=404, detail="Live stream is not running")

    manifest_age_seconds = time.time() - manifest.stat().st_mtime
    stale_manifest = manifest_age_seconds > 8.0
    global _last_manifest_stale_log_at_epoch
    if stale_manifest and (time.time() - _last_manifest_stale_log_at_epoch) >= 5.0:
        _last_manifest_stale_log_at_epoch = time.time()
        _log_event(
            "broadcast.manifest.stale",
            path=str(manifest),
            age_seconds=round(manifest_age_seconds, 3),
            range=range_header,
        )
        raise HTTPException(status_code=503, detail="Live stream manifest is stale")

    content = manifest.read_text(encoding="utf-8")
    size = len(content.encode("utf-8"))
    _log_event("broadcast.manifest.serve", path=str(manifest), size_bytes=size, range=range_header)
    return Response(
        content=content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0, s-maxage=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Live-Manifest-Stale": "1" if stale_manifest else "0",
        },
    )


@app.get("/broadcast/{segment_name}")
def broadcast_live_segment(segment_name: str, request: Request):
    segment = broadcast_engine.segment_path(segment_name)
    range_header = request.headers.get("range")
    if not segment.exists() or segment.suffix != ".ts":
        _log_event("broadcast.segment.miss", segment=segment_name, path=str(segment), range=range_header)
        raise HTTPException(status_code=404, detail="Segment not found")
    size = segment.stat().st_size
    _log_event("broadcast.segment.serve", segment=segment_name, path=str(segment), size_bytes=size, range=range_header)
    return FileResponse(
        str(segment),
        media_type="video/mp2t",
        filename=segment.name,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0, s-maxage=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Live-Manifest-Stale": "1" if stale_manifest else "0",
        },
    )


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
    first_track = queue["tracks"][0] if queue.get("tracks") else None
    if first_track is not None:
        try:
            broadcast_engine.start(station_id=payload.station_id, source_track_path=Path(first_track.file_path).expanduser().resolve())
        except Exception as exc:  # noqa: BLE001
            logger.exception("broadcast.start.failed station_id=%s", payload.station_id)
            state.last_error = f"broadcast start failed: {exc}"
            db.commit()
            db.refresh(state)
    _log_event("player.play.started", station_id=payload.station_id, queue_items=len(sequence))
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="play")


@app.post("/player/next", response_model=PlayerActionResponse)
def player_next(db: Session = Depends(get_db), _admin: None = Depends(_require_admin)):
    state = _get_or_create_player_state(db)
    if state.playout_mode == "live":
        raise HTTPException(status_code=403, detail="/player/next is disabled during live playout; use /player/admin/command")
    _log_event("player.next.requested", current_index=state.queue_index)
    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No queue available")

    queue = json.loads(state.queue_json) if state.queue_json else []
    if not queue or not (0 <= state.queue_index < len(queue)):
        state.is_playing = False
        state.current_track_id = None
        state.playout_mode = "recovering"
    else:
        queue[state.queue_index]["planned_end_epoch"] = time.time()
        state.queue_json = json.dumps(queue)
        state.playout_mode = "live"

    db.commit()
    db.refresh(state)
    _log_event("player.next.completed", new_index=state.queue_index, is_playing=state.is_playing)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="next")


@app.post("/player/admin/command", response_model=PlayerActionResponse)
def player_admin_command(payload: PlayerAdminCommandRequest, db: Session = Depends(get_db), _admin: None = Depends(_require_admin)):
    state = _get_or_create_player_state(db)
    if payload.command == "force_station_change" and payload.station_id is None:
        raise HTTPException(status_code=400, detail="station_id is required for force_station_change")
    if payload.station_id is not None:
        station = db.query(Station).filter(Station.id == payload.station_id).first()
        if not station:
            raise HTTPException(status_code=404, detail=f"Station {payload.station_id} not found")
    command = _enqueue_admin_command(state, payload)
    _log_event("player.admin.command.queued", command=command.get("command"), station_id=command.get("station_id"))
    db.commit()
    db.refresh(state)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action=f"admin:{payload.command}")


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.stop.requested", station_id=state.current_station_id)
    state.is_playing = False
    state.playout_mode = "stopped"
    broadcast_engine.stop()
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
    clip, audio_path, cached = get_or_create_dj_clip(db, payload.script_text, payload.voice, provider=provider, persist=True)
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to persist DJ clip")
    return DJClipResponse(clip_id=clip.id, audio_path=audio_path, voice=clip.voice, cached=cached)

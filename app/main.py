import json
import logging
import os
import time
import hashlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .config import AppConfig, load_config, save_config
from .database import Base, engine, get_db
from .models import DJClip, FavoriteStation, PlayerState, RecentStation, Station, Track
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
    FavoriteStationRequest,
    DJScriptGenerateRequest,
    DJScriptResponse,
    DJClipSynthesizeRequest,
    DJClipResponse,
    StationGenerateRequest,
    StationOut,
    TrackOut,
    BroadcastStatusResponse,
)
from .scheduler import build_station_queue
from .stations import generate_stations
from .tts import build_tts_provider, get_or_create_dj_clip

Base.metadata.create_all(bind=engine)

logger = logging.getLogger(__name__)

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

broadcast_engine = BroadcastEngine(Path("generated_audio") / "hls")


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
    )


def _advance_player(state: PlayerState) -> None:
    queue = json.loads(state.queue_json) if state.queue_json else []
    if not queue:
        state.is_playing = False
        state.current_track_id = None
        return
    next_idx = state.queue_index + 1
    if next_idx >= len(queue):
        state.is_playing = False
        state.current_track_id = None
        return
    state.queue_index = next_idx
    item = queue[next_idx]
    state.current_track_id = item.get("track_id") if item.get("type") == "track" else None



@app.get("/player/status", response_model=PlayerStateResponse)
def player_status(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


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
def broadcast_live_manifest():
    manifest = Path("generated_audio") / "hls" / "live.m3u8"
    if not manifest.exists():
        raise HTTPException(status_code=404, detail="Live stream is not running")
    return FileResponse(str(manifest), media_type="application/vnd.apple.mpegurl", filename="live.m3u8")


@app.get("/broadcast/{segment_name}")
def broadcast_live_segment(segment_name: str):
    segment = Path("generated_audio") / "hls" / segment_name
    if not segment.exists() or segment.suffix != ".ts":
        raise HTTPException(status_code=404, detail="Segment not found")
    return FileResponse(str(segment), media_type="video/mp2t", filename=segment.name)


@app.post("/player/play", response_model=PlayerActionResponse)
def player_play(payload: PlayerPlayRequest, db: Session = Depends(get_db)):
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
def player_next(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.next.requested", current_index=state.queue_index)
    config = load_config()

    if not state.queue_json:
        if state.current_station_id is None:
            raise HTTPException(status_code=400, detail="No station selected")
        station = db.query(Station).filter(Station.id == state.current_station_id).first()
        if not station:
            raise HTTPException(status_code=404, detail=f"Station {state.current_station_id} not found")
        queue = build_station_queue(db=db, station_id=station.id, config=config, size=10, seed=None)
        sequence = [
            {"type": "track", "track_id": track.id, "label": f"{track.artist or 'Unknown'} - {track.title or 'Untitled'}"}
            for track in queue["tracks"]
        ]
        state.queue_json = json.dumps(sequence)
        state.queue_index = 0
        state.current_track_id = sequence[0].get("track_id") if sequence else None
        state.is_playing = bool(sequence)
        db.commit()
        db.refresh(state)
        return PlayerActionResponse(state=_build_player_state_response(db, state), action="next")

    queue = json.loads(state.queue_json) if state.queue_json else []
    if not queue or not (0 <= state.queue_index < len(queue)):
        state.is_playing = False
        state.current_track_id = None
    else:
        current_item = queue[state.queue_index]
        if current_item.get("type") == "track" and state.current_station_id is not None and state.queue_index + 1 < len(queue):
            station = db.query(Station).filter(Station.id == state.current_station_id).first()
            current_track = db.query(Track).filter(Track.id == current_item.get("track_id")).first()
            next_item = queue[state.queue_index + 1]
            next_track = db.query(Track).filter(Track.id == next_item.get("track_id")).first() if next_item.get("type") == "track" else None
            payload_script = DJScriptGenerateRequest(
                previous_track_id=current_track.id if current_track else None,
                next_track_id=next_track.id if next_track else None,
                include_weather=False,
                include_news=False,
                include_fake_ad=False,
                max_sentences=3,
            )
            script = generate_dj_script(
                station=station,
                payload=payload_script,
                previous_track=current_track,
                next_track=next_track,
                config=config if config.radio_polish_enabled else None,
            )
            opener = f"{_daypart_greeting(config)} from {station.name}. " if config.daypart_programming_enabled else ""
            time_check = f"{_local_time_announcement(config)} and you're listening to {station.name}. " if config.time_announcement_enabled else ""
            queue.insert(
                state.queue_index + 1,
                {
                    "type": "dj",
                    "label": f"{station.dj_name or 'DJ'} break",
                    "script_text": f"{time_check}{opener}{script.script_text}",
                    "is_ad_break": False,
                },
            )
            state.queue_json = json.dumps(queue)
            if station and current_track and next_track:
                script_text = queue[state.queue_index + 1].get("script_text") or queue[state.queue_index + 1].get("label") or "Station ID"
                voice = queue[state.queue_index + 1].get("voice")
                try:
                    provider = build_tts_provider(config)
                except ValueError:
                    provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
                _clip, audio_path, _cached = get_or_create_dj_clip(
                    db,
                    script_text=script_text,
                    voice=voice,
                    provider=provider,
                    persist=False,
                )
                try:
                    broadcast_engine.start_transition(
                        station_id=station.id,
                        current_track_path=_safe_media_path(current_track.file_path, config),
                        dj_clip_path=_safe_media_path(audio_path, config),
                        next_track_path=_safe_media_path(next_track.file_path, config),
                    )
                except Exception:
                    logger.exception("broadcast.transition.start.failed station_id=%s", station.id)
        _advance_player(state)

    db.commit()
    db.refresh(state)
    _log_event("player.next.completed", new_index=state.queue_index, is_playing=state.is_playing)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="next")


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.stop.requested", station_id=state.current_station_id)
    state.is_playing = False
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

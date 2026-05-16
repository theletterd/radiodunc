import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

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
)
from .scheduler import build_station_queue
from .stations import generate_stations
from .tts import build_tts_provider, get_or_create_dj_clip

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Local AI Radio Station Generator", version="0.2.0")
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")


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
    target_folder = payload.folder_path or config.music_folder
    try:
        result = scan_library(target_folder, db)
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
        clip, _cached = get_or_create_dj_clip(db, script_text=script_text, voice=voice, provider=build_tts_provider(config))
        media_path = _safe_media_path(clip.audio_path, config)
        return FileResponse(str(media_path), filename=media_path.name)

    raise HTTPException(status_code=409, detail="Current queue item has unsupported type")


@app.post("/player/play", response_model=PlayerActionResponse)
def player_play(payload: PlayerPlayRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    station = db.query(Station).filter(Station.id == payload.station_id).first()
    if not station:
        raise HTTPException(status_code=404, detail=f"Station {payload.station_id} not found")

    queue = build_station_queue(db=db, station_id=payload.station_id, config=load_config(), size=payload.queue_size, seed=payload.seed)
    sequence = []
    for track in queue["tracks"]:
        sequence.append({"type": "track", "track_id": track.id, "label": f"{track.artist or 'Unknown'} - {track.title or 'Untitled'}"})
        sequence.append({"type": "dj", "label": f"{station.dj_name or 'DJ'} break", "script_text": f"{station.dj_name or 'DJ'} here on {station.name}."})

    state.current_station_id = payload.station_id
    state.is_playing = True
    state.queue_json = json.dumps(sequence)
    state.queue_index = 0
    state.current_track_id = sequence[0].get("track_id") if sequence else None
    state.last_error = None
    db.commit()
    db.refresh(state)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="play")


@app.post("/player/next", response_model=PlayerActionResponse)
def player_next(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _advance_player(state)
    db.commit()
    db.refresh(state)
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="next")


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    state.is_playing = False
    db.commit()
    db.refresh(state)
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

    clip, cached = get_or_create_dj_clip(db, payload.script_text, payload.voice, provider=build_tts_provider(load_config()))
    return DJClipResponse(clip_id=clip.id, audio_path=clip.audio_path, voice=clip.voice, cached=cached)

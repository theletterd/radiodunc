from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .config import AppConfig, load_config, save_config
from .database import Base, engine, get_db
from .models import FavoriteStation, PlayerState, RecentStation, Station, Track
from .scanner import scan_library
from .schemas import (
    LibraryScanRequest,
    QueueGenerateRequest,
    QueueResponse,
    PlayerStateResponse,
    PlayerStateUpdateRequest,
    FavoriteStationRequest,
    StationGenerateRequest,
    StationOut,
    TrackOut,
)
from .scheduler import build_station_queue
from .stations import generate_stations

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
    return PlayerStateResponse(
        station_id=state.current_station_id,
        is_playing=state.is_playing,
        volume=state.volume,
        station=station,
        favorites=favorites,
        recent_station_ids=recent_station_ids,
    )


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

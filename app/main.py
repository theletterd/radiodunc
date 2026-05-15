from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import DJClip, Station, Track
from .scanner import scan_library
from .schemas import LibraryScanRequest, TrackOut

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Local AI Radio Station Generator", version="0.1.0")


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


@app.post("/library/scan")
def scan_library_endpoint(payload: LibraryScanRequest, db: Session = Depends(get_db)):
    try:
        result = scan_library(payload.folder_path, db)
        return result
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to scan library: {exc}") from exc


@app.get("/tracks", response_model=list[TrackOut])
def list_tracks(db: Session = Depends(get_db)):
    return db.query(Track).order_by(Track.artist.asc(), Track.album.asc(), Track.title.asc()).all()

from __future__ import annotations

import json
import random
from collections import deque

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import Station, Track


FALLBACK_ARTIST = "Unknown"


def _station_core_artists(station: Station) -> set[str]:
    if not station.config_json:
        return set()
    try:
        parsed = json.loads(station.config_json)
    except json.JSONDecodeError:
        return set()
    artists = parsed.get("core_artists") or []
    return {str(artist).strip() for artist in artists if str(artist).strip()}


def build_station_queue(
    db: Session,
    station_id: int,
    config: AppConfig,
    size: int,
    seed: int | None = None,
) -> dict:
    station = db.query(Station).filter(Station.id == station_id).first()
    if station is None:
        raise ValueError(f"Station {station_id} not found.")

    tracks = db.query(Track).order_by(Track.id.asc()).all()
    if not tracks:
        raise ValueError("No tracks found. Scan your library before generating a queue.")

    target_size = min(size, len(tracks))
    anti_repeat_window = max(0, config.playlist_artist_repeat_window)
    rng = random.Random(seed)

    core_artists = _station_core_artists(station)
    aligned_tracks = [t for t in tracks if (t.artist or FALLBACK_ARTIST) in core_artists]
    pool = aligned_tracks if aligned_tracks else tracks

    artist_recent: deque[str] = deque(maxlen=anti_repeat_window)
    available = pool.copy()
    queue: list[Track] = []

    while len(queue) < target_size:
        if not available:
            available = pool.copy()

        allowed = [t for t in available if (t.artist or FALLBACK_ARTIST) not in artist_recent]
        if not allowed:
            artist_recent.clear()
            allowed = available.copy()

        pick = rng.choice(allowed)
        queue.append(pick)
        available.remove(pick)
        artist_recent.append(pick.artist or FALLBACK_ARTIST)

    return {
        "station_id": station.id,
        "station_name": station.name,
        "queue_size": len(queue),
        "seed": seed,
        "artist_repeat_window": anti_repeat_window,
        "tracks": queue,
        "used_station_alignment": bool(aligned_tracks),
    }

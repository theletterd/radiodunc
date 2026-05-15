from __future__ import annotations

import json
import random
from collections import Counter

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import Station, Track


def _library_summary(db: Session) -> dict:
    tracks = db.query(Track).all()
    genres = Counter(t.genre or "Unknown" for t in tracks)
    artists = Counter(t.artist or "Unknown" for t in tracks)
    return {
        "track_count": len(tracks),
        "top_genres": [g for g, _ in genres.most_common(3)],
        "top_artists": [a for a, _ in artists.most_common(5)],
    }


def generate_stations(db: Session, config: AppConfig, count: int | None = None) -> dict:
    summary = _library_summary(db)
    if summary["track_count"] == 0:
        raise ValueError("No tracks found. Scan your library before generating stations.")

    target_count = count or config.station_generation_count
    rng = random.Random(config.station_generation_seed)
    presets = [preset.model_dump() for preset in config.station_presets]
    rng.shuffle(presets)

    created: list[Station] = []
    for idx in range(min(target_count, len(presets))):
        preset = presets[idx]
        format_name = preset["format"]
        station_name = f"{format_name} FM"
        description = (
            f"Built from {summary['track_count']} tracks with emphasis on "
            f"{', '.join(summary['top_genres'][:2])}."
        )
        station_config = {
            "weather_location": config.alerts.weather_location,
            "local_time_zone": config.alerts.local_time_zone,
            "news_preferences": config.alerts.news.model_dump(),
            "core_artists": summary["top_artists"][:3],
            "voice_hint": preset.get("voice_hint"),
        }

        station = Station(
            name=station_name,
            tagline=preset["tagline"],
            format=format_name,
            description=description,
            dj_name=f"{preset['dj_name_prefix']} {format_name.split()[0]}",
            dj_style=preset["dj_style"],
            config_json=json.dumps(station_config),
        )
        db.add(station)
        created.append(station)

    db.commit()
    return {"generated": len(created), "library_summary": summary, "seed": config.station_generation_seed}

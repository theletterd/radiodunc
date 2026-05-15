from __future__ import annotations

import json
import random
from collections import Counter

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import Station, Track

FORMAT_PRESETS: list[dict] = [
    {"format": "Indie Discovery", "tagline": "Fresh cuts and deep tracks.", "dj_style": "warm storyteller"},
    {"format": "Classic Rock Drive", "tagline": "Legends on repeat, with attitude.", "dj_style": "high-energy throwback"},
    {"format": "Chill Evenings", "tagline": "Low-key vibes for late nights.", "dj_style": "calm minimalist"},
    {"format": "Pop Pulse", "tagline": "Hooks, hits, and new obsessions.", "dj_style": "playful and fast-paced"},
    {"format": "Eclectic Mixtape", "tagline": "No rules, only great songs.", "dj_style": "quirky curator"},
    {"format": "Retro Rewind", "tagline": "Back when radio ruled the road.", "dj_style": "nostalgic host"},
    {"format": "Alternative Edge", "tagline": "Sharp guitars and bold voices.", "dj_style": "witty and rebellious"},
    {"format": "Late Night Vinyl", "tagline": "Analog soul for digital nights.", "dj_style": "intimate and poetic"},
]


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
    presets = FORMAT_PRESETS.copy()
    random.shuffle(presets)

    created: list[Station] = []
    for idx in range(min(target_count, len(presets))):
        preset = presets[idx]
        station_name = f"{preset['format']} FM"
        description = (
            f"Built from {summary['track_count']} tracks with emphasis on "
            f"{', '.join(summary['top_genres'][:2])}."
        )
        station_config = {
            "weather_location": config.alerts.weather_location,
            "local_time_zone": config.alerts.local_time_zone,
            "news_preferences": config.alerts.news.model_dump(),
            "core_artists": summary["top_artists"][:3],
        }

        station = Station(
            name=station_name,
            tagline=preset["tagline"],
            format=preset["format"],
            description=description,
            dj_name=f"DJ {preset['format'].split()[0]}",
            dj_style=preset["dj_style"],
            config_json=json.dumps(station_config),
        )
        db.add(station)
        created.append(station)

    db.commit()
    return {"generated": len(created), "library_summary": summary}

from __future__ import annotations

import json

from .models import Station, Track
from .schemas import DJScriptGenerateRequest, DJScriptResponse


def _track_ref(track: Track | None) -> str:
    if not track:
        return "an unknown track"
    title = track.title or "Unknown Title"
    artist = track.artist or "Unknown Artist"
    return f"{title} by {artist}"


def generate_dj_script(station: Station, payload: DJScriptGenerateRequest, previous_track: Track | None, next_track: Track | None) -> DJScriptResponse:
    dj_name = station.dj_name or "DJ"
    style = station.dj_style or "friendly"

    sentence_pool: list[str] = [
        f"You're listening to {station.name} with {dj_name}, keeping it {style} tonight.",
        f"We just heard {_track_ref(previous_track)}, and up next is {_track_ref(next_track)}.",
        f"{station.tagline or 'Stay tuned for more great music.'}",
    ]

    cfg: dict = {}
    if station.config_json:
        try:
            cfg = json.loads(station.config_json)
        except json.JSONDecodeError:
            cfg = {}

    if payload.include_weather:
        location = cfg.get("weather_location") or "your area"
        sentence_pool.append(f"Quick weather check for {location}: keep it locked here while we keep the soundtrack rolling.")

    if payload.include_news:
        sentence_pool.append("News flash is coming at the top of the hour, right after more hand-picked tracks.")

    if payload.include_fake_ad:
        sentence_pool.append("This set is sponsored by Midnight Coffee Co.—brew loud, drive smooth.")

    sentences = sentence_pool[: payload.max_sentences]
    return DJScriptResponse(
        station_id=station.id,
        station_name=station.name,
        dj_name=dj_name,
        sentences=sentences,
        script_text=" ".join(sentences),
    )

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import AppConfig
from .models import Station, Track
from .prompt_library import (
    render_ad_break_prompt,
    render_news_prompt,
    render_song_transition_prompt,
    render_weather_prompt,
)
from .schemas import DJScriptGenerateRequest, DJScriptResponse


def _track_ref(track: Track | None) -> str:
    if not track:
        return "an unknown track"
    title = track.title or "Unknown Title"
    artist = track.artist or "Unknown Artist"
    return f"{title} by {artist}"


def _generate_openai_script(
    station: Station,
    payload: DJScriptGenerateRequest,
    previous_track: Track | None,
    next_track: Track | None,
    config: AppConfig,
) -> str | None:
    if config.script_provider != "openai" or not config.openai_api_key:
        return None

    prompt_sections = [
        render_song_transition_prompt(station, previous_track, next_track, payload.max_sentences),
        f"DJ name: {station.dj_name or 'DJ'}. Style: {station.dj_style or 'friendly'}.",
        f"Include weather={payload.include_weather}, news={payload.include_news}, ad={payload.include_fake_ad}.",
    ]
    if payload.include_weather:
        prompt_sections.append(render_weather_prompt(station, "your area"))
    if payload.include_news:
        prompt_sections.append(render_news_prompt(station))
    if payload.include_fake_ad:
        prompt_sections.append(render_ad_break_prompt(station))
    prompt_sections.append("Return plain text only.")
    prompt = " ".join(prompt_sections)
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({"model": config.openai_text_model, "input": prompt}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.openai_api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    output_text = data.get("output_text")
    return output_text if isinstance(output_text, str) and output_text.strip() else None


def generate_dj_script(
    station: Station,
    payload: DJScriptGenerateRequest,
    previous_track: Track | None,
    next_track: Track | None,
    config: AppConfig | None = None,
) -> DJScriptResponse:
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
        sentence_pool.append(render_weather_prompt(station, location))
        sentence_pool.append(f"Quick weather check for {location}: keep it locked here while we keep the soundtrack rolling.")

    if payload.include_news:
        sentence_pool.append(render_news_prompt(station))
        sentence_pool.append("News flash is coming at the top of the hour, right after more hand-picked tracks.")

    if payload.include_fake_ad:
        sentence_pool.append(render_ad_break_prompt(station))
        sentence_pool.append("This set is sponsored by Midnight Coffee Co.—brew loud, drive smooth.")

    if config is not None:
        scripted = _generate_openai_script(station, payload, previous_track, next_track, config)
        if scripted:
            parts = [part.strip() for part in scripted.replace("\n", " ").split(".") if part.strip()]
            sentences = [f"{part}." for part in parts[: payload.max_sentences]]
            if sentences:
                return DJScriptResponse(
                    station_id=station.id,
                    station_name=station.name,
                    dj_name=dj_name,
                    sentences=sentences,
                    script_text=" ".join(sentences),
                )

    sentences = sentence_pool[: payload.max_sentences]
    return DJScriptResponse(
        station_id=station.id,
        station_name=station.name,
        dj_name=dj_name,
        sentences=sentences,
        script_text=" ".join(sentences),
    )

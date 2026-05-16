from __future__ import annotations

from .models import Station, Track


def _track_ref(track: Track | None) -> str:
    if not track:
        return "an unknown track"
    title = track.title or "Unknown Title"
    artist = track.artist or "Unknown Artist"
    return f"{title} by {artist}"


def render_song_transition_prompt(
    station: Station,
    previous_track: Track | None,
    next_track: Track | None,
    max_sentences: int,
) -> str:
    return (
        "[TRANSITION_PROMPT_PLACEHOLDER] "
        f"Write a {max_sentences}-sentence transition for {station.name} from "
        f"{_track_ref(previous_track)} into {_track_ref(next_track)}."
    )


def render_ad_break_prompt(station: Station) -> str:
    return (
        "[AD_BREAK_PROMPT_PLACEHOLDER] "
        f"Write a brief sponsor-style ad break line in the voice of {station.dj_name or 'DJ'} on {station.name}."
    )


def render_weather_prompt(station: Station, weather_location: str) -> str:
    return (
        "[WEATHER_PROMPT_PLACEHOLDER] "
        f"Write a concise weather update for {weather_location} on {station.name}."
    )


def render_news_prompt(station: Station) -> str:
    return "[NEWS_PROMPT_PLACEHOLDER] Write a short top-of-hour news tease for station {station_name}.".format(
        station_name=station.name
    )

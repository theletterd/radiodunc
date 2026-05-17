from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import WEEKDAYS, AppConfig, DJPersona, StationConfig
from .models import Track
from .news import fetch_random_headline
from .schemas import DJScriptGenerateRequest, DJScriptResponse
from .weather import fetch_weather_summary

logger = logging.getLogger(__name__)


def _persona_matches(persona: DJPersona, now: datetime) -> bool:
    if persona.days:
        weekday_name = WEEKDAYS[now.weekday()]
        if weekday_name not in persona.days:
            return False
    if persona.start_hour is not None or persona.end_hour is not None:
        start = persona.start_hour if persona.start_hour is not None else 0
        end = persona.end_hour if persona.end_hour is not None else 23
        hour = now.hour
        if start <= end:
            if not (start <= hour <= end):
                return False
        else:
            # Wrapping window, e.g. 22..3 covers 22, 23, 0, 1, 2, 3
            if not (hour >= start or hour <= end):
                return False
    return True


def pick_active_persona(station: StationConfig, now: datetime) -> DJPersona | None:
    """Return the first roster persona whose schedule matches now, or None."""
    for persona in station.dj_roster:
        if _persona_matches(persona, now):
            return persona
    return None


def active_station(station: StationConfig, config: AppConfig, now: datetime | None = None) -> StationConfig:
    """Return station with DJ fields overridden by any matching roster persona."""
    if not station.dj_roster:
        return station
    if now is None:
        try:
            now = datetime.now(ZoneInfo(config.alerts.local_time_zone))
        except Exception:  # noqa: BLE001
            now = datetime.now()
    persona = pick_active_persona(station, now)
    if persona is None:
        return station
    return station.model_copy(update={
        "dj_name": persona.name,
        "dj_style": persona.style,
        "voice": persona.voice or station.voice,
        "voice_instructions": persona.voice_instructions or station.voice_instructions,
        "dj_prompt_template": persona.prompt_template or station.dj_prompt_template,
    })


DEFAULT_DJ_PROMPT_TEMPLATE = """\
Write a {max_sentences}-sentence radio DJ transition for '{station_name}'.
DJ: {dj_name} ({dj_style}).
Station format: {station_format}.{station_era}{station_genre_focus}{station_description}
Local time right now: {current_time} on {current_weekday}. Mention the time only if it fits naturally (top of the hour, late night, morning, etc.) — don't force it.
We just heard: {previous_track}.
Up next: {next_track}.
{reason_block}{weather_block}{news_block}{ad_block}\
Return plain text only — no headings, no markdown.
When mentioning the station name, you MUST say it in full, e.g 'KVW 98 point 6 FM'"""


DEFAULT_AD_PROMPT_TEMPLATE = """\
Write a 2-sentence late-night radio sponsor spot. Invent a fake (sometimes wacky, sometimes plausible)
brand and product (For example: a local service (e.g., window cleaners, mechanic, restaurant), food, gadget or dating app) — be creative and varied.
Vintage radio-ad voice: punchy, slightly cheesy, with a memorable tagline.
Make it clearly sound like an ad break, not DJ banter.
Return plain text only — no headings, no markdown, no quotation marks."""


def _track_ref(track: Track | None) -> str:
    if not track:
        return "an unknown track"
    title = track.title or "Unknown Title"
    artist = track.artist or "Unknown Artist"
    filename = Path(track.file_path).name if track.file_path else None
    base = f"{title} by {artist}"
    return f"{base} (filename: {filename})" if filename else base


def _build_prompt(
    station: StationConfig,
    payload: DJScriptGenerateRequest,
    previous_track: Track | None,
    next_track: Track | None,
    config: AppConfig,
) -> str:
    weather_block = ""
    if payload.include_weather:
        location = config.alerts.weather_location
        live_weather = fetch_weather_summary(location)
        if live_weather:
            weather_block = f"Weather context — use as facts: {live_weather}\n"
        else:
            weather_block = f"Weather context: include a brief check for {location}.\n"
    news_block = ""
    if payload.include_news:
        headline = fetch_random_headline(config.alerts.news.rss_url)
        if headline:
            news_block = f"News context — work in this real headline naturally: \"{headline}\".\n"
        else:
            news_block = "News context: include a one-sentence top-of-hour news tease.\n"
    ad_block = "Ad context: include a brief sponsor-style ad break line. Include the fact that the next song plays after the break.\n" if payload.include_fake_ad else ""
    if payload.ad_break_follows:
        ad_block = (
            "Ad break context: a short ad break follows immediately after your transition. "
            "Work in a natural tease — something like 'coming up after the break, [next track]' — "
            "keep it brief and smooth. Don't dwell on it.\n"
        )
    reason_block = ""
    if payload.reason == "skip":
        reason_block = (
            "Reason: the audience didn't like the previous track. "
            "Acknowledge that lightly and tee up the next one with extra enthusiasm.\n"
        )
    elif payload.reason == "request":
        reason_block = (
            "Reason: this next track is an audience request — someone called in for it. "
            "Give it a warm intro, like you're honouring their pick.\n"
        )

    try:
        now = datetime.now(ZoneInfo(config.alerts.local_time_zone))
    except Exception:  # noqa: BLE001
        now = datetime.now()
    current_time = now.strftime("%-I:%M %p")
    current_weekday = WEEKDAYS[now.weekday()].capitalize()

    fields = {
        "max_sentences": payload.max_sentences,
        "station_name": station.name,
        "station_format": station.format,
        "station_description": f" {station.description}" if station.description else "",
        "station_era": f" Era: {station.era}." if station.era else "",
        "station_genre_focus": f" Genre focus: {', '.join(station.genre_focus)}." if station.genre_focus else "",
        "dj_name": station.dj_name,
        "dj_style": station.dj_style,
        "previous_track": _track_ref(previous_track),
        "next_track": _track_ref(next_track),
        "current_time": current_time,
        "current_weekday": current_weekday,
        "weather_block": weather_block,
        "news_block": news_block,
        "ad_block": ad_block,
        "reason_block": reason_block,
    }
    template = station.dj_prompt_template or DEFAULT_DJ_PROMPT_TEMPLATE
    try:
        return template.format_map(fields)
    except KeyError as exc:
        logger.warning("dj_prompt_template has unknown placeholder %s; using default", exc)
        return DEFAULT_DJ_PROMPT_TEMPLATE.format_map(fields)


def _call_openai_text(prompt: str, config: AppConfig) -> str | None:
    """POST a prompt to OpenAI's Responses API and return the text, or None on failure."""
    if config.script_provider != "openai":
        logger.info("Skipping OpenAI text generation: script_provider=%s", config.script_provider)
        return None
    if not config.openai_api_key:
        logger.warning("Skipping OpenAI text generation: OPENAI_API_KEY is missing")
        return None

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({"model": config.openai_text_model, "input": prompt}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.openai_api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("OpenAI text request failed status=%s", exc.code)
        return None
    except urllib.error.URLError as exc:
        logger.warning("OpenAI text request failed reason=%s", exc.reason)
        return None
    except TimeoutError:
        logger.warning("OpenAI text request timed out")
        return None
    except json.JSONDecodeError:
        logger.warning("OpenAI text response was not valid JSON")
        return None

    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output_items = data.get("output")
    if isinstance(output_items, list):
        text_chunks: list[str] = []
        for item in output_items:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content_items = item.get("content")
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                if not isinstance(content, dict) or content.get("type") != "output_text":
                    continue
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    text_chunks.append(text.strip())
        if text_chunks:
            return " ".join(text_chunks)
    return None


def _generate_openai_script(
    station: StationConfig,
    payload: DJScriptGenerateRequest,
    previous_track: Track | None,
    next_track: Track | None,
    config: AppConfig,
) -> str | None:
    logger.info("Generating DJ transition script via OpenAI model=%s", config.openai_text_model)
    prompt = _build_prompt(station, payload, previous_track, next_track, config)
    logger.info(prompt)
    text = _call_openai_text(prompt, config)
    if text is None:
        logger.warning("OpenAI DJ script failed; will fall back to sentence pool")
    return text


def generate_ad_script(station: StationConfig, config: AppConfig) -> str | None:
    """Generate a fake-ad script via OpenAI. Returns None on failure (caller should skip the ad)."""
    template = config.alerts.ads.prompt_template or DEFAULT_AD_PROMPT_TEMPLATE
    fields = {
        "station_name": station.name,
        "station_format": station.format,
        "dj_name": station.dj_name,
    }
    try:
        prompt = template.format_map(fields)
    except KeyError as exc:
        logger.warning("ads.prompt_template has unknown placeholder %s; using default", exc)
        prompt = DEFAULT_AD_PROMPT_TEMPLATE.format_map(fields)
    logger.info("Generating ad-break script via OpenAI")
    return _call_openai_text(prompt, config)

    logger.warning("OpenAI DJ script response missing usable output_text; falling back to sentence pool")
    return None


def generate_dj_script(
    station: StationConfig,
    payload: DJScriptGenerateRequest,
    previous_track: Track | None,
    next_track: Track | None,
    config: AppConfig | None = None,
) -> DJScriptResponse:
    sentence_pool: list[str] = []
    if payload.reason == "skip":
        sentence_pool.append(
            f"Sounds like {_track_ref(previous_track)} wasn't doing it for you — let's try this instead."
        )
    elif payload.reason == "request":
        sentence_pool.append(
            f"We've got a request coming in — this one goes out to whoever called in for {_track_ref(next_track)}."
        )
    sentence_pool.extend([
        f"You're listening to {station.name} with {station.dj_name}, keeping it {station.dj_style} tonight.",
        f"We just heard {_track_ref(previous_track)}, and up next is {_track_ref(next_track)}.",
        f"{station.tagline}",
    ])

    if payload.include_weather:
        location = config.alerts.weather_location if config else "your area"
        live_weather = fetch_weather_summary(location)
        if live_weather:
            sentence_pool.append(live_weather)
        else:
            sentence_pool.append(
                f"Quick weather check for {location}: keep it locked here while we keep the soundtrack rolling."
            )

    if payload.include_news:
        sentence_pool.append("News flash is coming at the top of the hour, right after more hand-picked tracks.")

    if payload.include_fake_ad:
        sentence_pool.append("This set is sponsored by Midnight Coffee Co.—brew loud, drive smooth.")

    if config is not None:
        scripted = _generate_openai_script(station, payload, previous_track, next_track, config)
        if scripted:
            parts = [part.strip() for part in scripted.replace("\n", " ").split(".") if part.strip()]
            sentences = [f"{part}." for part in parts[: payload.max_sentences]]
            if sentences:
                return DJScriptResponse(
                    station_name=station.name,
                    dj_name=station.dj_name,
                    sentences=sentences,
                    script_text=" ".join(sentences),
                )

    sentences = sentence_pool[: payload.max_sentences]
    return DJScriptResponse(
        station_name=station.name,
        dj_name=station.dj_name,
        sentences=sentences,
        script_text=" ".join(sentences),
    )

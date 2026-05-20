from __future__ import annotations

import concurrent.futures
import json
import logging
import math
import time
import urllib.error
import urllib.request

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import WEEKDAYS, AppConfig, DJ, DJPersona, DJShift, Show, StationConfig
from .models import Track
from .news import fetch_random_headline, fetch_top_headlines
from .schemas import DJScriptGenerateRequest, DJScriptResponse
from .weather import fetch_weather_summary

logger = logging.getLogger(__name__)


def _shift_covers(shift: DJShift, weekday_name: str, hour: int) -> bool:
    """True if a single shift covers (weekday_name, hour). Handles wrapping
    windows (e.g. 22..3 covers 22, 23, 0, 1, 2, 3)."""
    if shift.day != weekday_name:
        return False
    if shift.start_hour <= shift.end_hour:
        return shift.start_hour <= hour <= shift.end_hour
    return hour >= shift.start_hour or hour <= shift.end_hour


def _shifts_match(shifts: list[DJShift], now: datetime) -> bool:
    """True if any shift in the list covers `now`.

    Per the DJ-vs-Show design, an empty shift list means the Show never airs
    (it's a transient "I just deleted everything to re-enter it" state, with
    a soft warning in the UI). This is intentionally different from the
    legacy DJPersona behaviour where empty shifts meant "always on".
    """
    if not shifts:
        return False
    weekday_name = WEEKDAYS[now.weekday()]
    hour = now.hour
    return any(_shift_covers(s, weekday_name, hour) for s in shifts)


def _persona_matches(persona: DJPersona, now: datetime) -> bool:
    """Legacy resolver: True if any of the persona's shifts covers `now`.
    Empty shifts → always on. Used only for the dj_roster fallback path below."""
    if not persona.shifts:
        return True
    weekday_name = WEEKDAYS[now.weekday()]
    hour = now.hour
    return any(_shift_covers(s, weekday_name, hour) for s in persona.shifts)


def _pick_active_show(station: StationConfig, now: datetime) -> Show | None:
    """Return the first Show whose shifts cover `now`, or None.

    First-match-wins; empty-shifts Shows are skipped (per the design: empty
    shifts mean the Show doesn't air — a soft-warning UI state, not a runtime
    match). Shared between pick_active_persona (for DJ lookup) and the prompt
    builder (for the {show_name} placeholder)."""
    for show in station.shows:
        if _shifts_match(show.shifts, now):
            return show
    return None


def pick_active_persona(station: StationConfig, now: datetime) -> DJ | DJPersona | None:
    """Return the DJ for the first matching Show, or None.

    None means "no override — caller falls through to station defaults". This
    happens when no Show matches the current time, or when the matching Show
    has dj_id=None (an explicit Default DJ slot in the schedule).

    Walks `station.shows` first. Falls back to the legacy `station.dj_roster`
    walk only when `shows` is empty — this preserves behaviour for configs
    that haven't been touched by the new model yet (back-compat for one
    release while installations migrate).
    """
    if station.shows:
        show = _pick_active_show(station, now)
        if show is None or show.dj_id is None:
            # No match, or an explicit Default-DJ slot.
            return None
        djs_by_id = {dj.id: dj for dj in station.djs}
        # If show.dj_id references a DJ that no longer exists, the lookup
        # returns None and we fall through to the Default DJ. Slice 6's
        # delete-DJ flow rewrites those references; this is the defensive
        # belt-and-braces case.
        return djs_by_id.get(show.dj_id)

    # Legacy fallback — only triggers when shows is empty.
    for persona in station.dj_roster:
        if _persona_matches(persona, now):
            return persona
    return None


def active_station(station: StationConfig, config: AppConfig, now: datetime | None = None) -> StationConfig:
    """Return station with DJ fields overridden by any matching Show's DJ."""
    if not station.shows and not station.dj_roster:
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
        "personality": persona.personality,
        "voice": persona.voice or station.voice,
        "voice_instructions": persona.voice_instructions or station.voice_instructions,
        "dj_prompt_template": persona.prompt_template or station.dj_prompt_template,
    })


STATION_PLACEHOLDER = "[[STATION]]"


def substitute_station_placeholder(script: str, config: AppConfig) -> str:
    """Replace [[STATION]] in an LLM-generated script with the station's
    spoken pronunciation. Uses spoken_name if set, falls back to name.

    LLMs (and TTS engines) garble formatted station names like 'RadioDunc
    107.2 FM' — they read '107.2' as 'one hundred and seven point two'
    instead of 'one oh seven point two', or skip the FM, or change the
    word order. Telling the LLM to emit a placeholder and substituting
    here gets us deterministic, configurable pronunciation."""
    if not script or STATION_PLACEHOLDER not in script:
        return script
    spoken = config.station.spoken_name or config.station.name
    return script.replace(STATION_PLACEHOLDER, spoken)

DEFAULT_DJ_PROMPT_TEMPLATE = """\
Write a {max_sentences}-sentence radio DJ transition for the station named '{station_name}'.
DJ: {dj_name} ({personality}).
Station format: {station_format}.{station_era}{station_genre_focus}{station_description}
{show_block}Local time right now: {current_time} on {current_weekday}. Mention the time only if it fits naturally (top of the hour, late night, morning, etc.) — don't force it.
We just heard: {previous_track}.
Up next: {next_track}.
{reason_block}{weather_block}{news_block}{ad_block}\
Return plain text only — no headings, no markdown.
When you would mention the station by name, write the literal placeholder [[STATION]] (with the double square brackets) — the radio software substitutes the correct spoken pronunciation before audio is generated. Never write the station name out as digits or abbreviations yourself."""


# Curated category list. One is picked at random per call and injected into the
# prompt as {ad_category}. Doing the selection in Python (rather than handing
# the LLM a list and asking it to "vary") removes anchoring bias — LLMs lean
# heavily on the last item in any embedded list, which is why the previous
# prompt produced wall-to-wall dating-app spots.
AD_CATEGORIES: list[str] = [
    "a local service: plumber, electrician, mechanic, dog groomer, locksmith, mover, roofer, handyman, or gardener — pick one and run with it",
    "a food or drink spot: pizza joint, coffee shop, brewery, food truck, bakery, taco stand, donut shop, deli, or ice-cream parlour — pick one",
    "a quirky retail store: bookshop, record store, thrift store, bike shop, board-game café, comic shop, or vinyl emporium — pick one",
    "an oddly specific kitchen, cleaning, or household gadget that solves a problem nobody knew they had",
    "a community class or recurring event: yoga, pottery, salsa dancing, axe throwing, escape rooms, life drawing, or cooking class — pick one",
    "a car-related service: tire shop, car wash, detailer, used dealership, or 24-hour towing — pick one",
    "a health or wellness service: dentist, chiropractor, massage clinic, optometrist, urgent care, or physical therapist — pick one",
    "an entertainment venue: mini-golf, bowling alley, drive-in theatre, vintage arcade, trampoline park, or roller-disco — pick one",
    "a slightly weird small business: psychic, taxidermist, pet boutique, vintage typewriter repair, fortune teller, or antique mall — pick one",
    "a home improvement product or service: paint store, hardware store, custom blinds, garden centre, pool company, or fencing contractor — pick one",
    "a travel or leisure product: B&B, RV rental, hiking gear, road-trip accessories, or scenic train tour — pick one",
    "a hyper-specific niche product or absurd subscription box that should not exist but somehow does",
    "a financial or professional service: insurance broker, tax prep, lawyer, real estate agent, or mortgage advisor — pick one",
    "an app, website, or piece of software: a dating app, a niche social network, a productivity gimmick, a delivery service, a hyper-local marketplace, or something for tracking something extremely mundane — pick one",
    "a personal-care or beauty business: hair salon, barbershop, nail studio, day spa, or tanning salon — pick one",
    "a pet-related service or product: dog daycare, cat boutique, exotic-pet supplies, mobile groomer, or pet psychic — pick one",
]


RISQUE_TONE_HINT = (
    "Tone for this one: lean a little risqué or suggestive — it's a late-night slot, "
    "the adults are listening. Don't be crass; the wink is the point."
)


DEFAULT_AD_PROMPT_TEMPLATE = """\
Write a 2-sentence late-night radio sponsor spot for {ad_category}.

Invent a fake brand and product. Punchy, slightly cheesy, with a memorable tagline.
Sometimes plausible, sometimes wacky — surprise me.
{ad_tone}
Vintage radio-ad voice. Clearly an ad break, not DJ banter.

Return plain text only — no headings, no markdown, no quotation marks."""


DEFAULT_NEWS_PROMPT_TEMPLATE = """\
Write a short radio news bulletin, delivered by {newsreader_name} from the {rss_source} news desk.
Total length: about 30–40 seconds when spoken aloud.

The bulletin MUST have three parts, in this order: INTRO, BODY, OUTRO.
The OUTRO is not optional. Do not finish the bulletin without it.

1. INTRO — one short sentence that opens the bulletin and establishes the newsreader. Examples:
   - "Good evening — I'm {newsreader_name} with the latest from {rss_source}."
   - "{rss_source} News, I'm {newsreader_name}."
   - "This is the {rss_source} news bulletin. I'm {newsreader_name}."
   Pick one of these or write something similar in feel. Vary it across bulletins.

2. BODY — cover these {headline_count} stories from today's feed, in the order given:
{headlines_block}
   For each story, write one clean sentence that adds slight context beyond the bare headline.

3. OUTRO — REQUIRED. A clean, deliberate sign-off sentence that ends the bulletin.
   It must:
   - Name the newsreader ({newsreader_name})
   - Feel distinct from the last news sentence — pause-worthy, like a closing line
   - Be exactly one short sentence
   Examples (pick one or write a similar one — vary across bulletins):
   - "I'm {newsreader_name} — more headlines later."
   - "That's the latest from {rss_source}. {newsreader_name} reporting."
   - "Back soon with more from the {rss_source} news desk. I'm {newsreader_name}."
   - "{newsreader_name} reporting for {rss_source}. Until next time."

Sober, neutral, professional newsreader tone throughout. No editorial, no jokes, no station mentions, no music banter.
Do NOT sign off with phrases like 'back to the music' or 'now back to your DJ' — a station ID plays after you finish; let it do that work.
Return plain text only — no headings, no markdown, no quotation marks, no section labels."""


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
        live_weather = fetch_weather_summary(
            location,
            latitude=config.alerts.weather_latitude,
            longitude=config.alerts.weather_longitude,
        )
        if live_weather:
            weather_block = f"Weather context — use as facts: {live_weather}\n"
        else:
            weather_block = f"Weather context: include a brief check for {location}.\n"
    news_block = ""
    if payload.news_break_follows:
        news_block = (
            "News break context: a short news bulletin follows immediately after your transition. "
            "Hand off naturally — something like 'and now to the news' or 'first, here's what's happening in the world'. "
            "Don't summarise the news yourself; the newsreader will handle that. Keep the hand-off brief.\n"
        )
    elif payload.include_news:
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
            "Reason: the listener skipped that last track. React briefly in YOUR voice and pivot energy onto the next song.\n"
            "AVOID these stock openings — they're canned LLM tells: "
            "'Well, well, well…', 'Alright, alright, alright…', 'Okay then', 'Moving on', "
            "'Not for everyone', 'Yeah, no', 'So…', 'Anyway'. Don't start with any of them.\n"
            "Better: say something specific and interesting about the NEXT track instead of dwelling on the skip.\n"
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

    # Show name surfaces when the active Show has been given a distinct name
    # (different from the DJ's own name). The point is to enable the contrast
    # angle — "Cheerful Morning Drive" hosted by "Ms. Jessica Danger" — so the
    # LLM has a hook to play with. Empty when there's no active Show, or when
    # the active Show has no name set.
    active_show = _pick_active_show(station, now)
    show_name = (active_show.name or "") if active_show else ""
    show_block = (
        f"Current show: '{show_name}' — if its vibe contrasts with your DJ persona, that's a hook to play with.\n"
        if show_name else ""
    )

    fields = {
        "max_sentences": payload.max_sentences,
        "station_name": station.name,
        "station_format": station.format,
        "station_description": f" {station.description}" if station.description else "",
        "station_era": f" Era: {station.era}." if station.era else "",
        "station_genre_focus": f" Genre focus: {', '.join(station.genre_focus)}." if station.genre_focus else "",
        "dj_name": station.dj_name,
        "personality": station.personality,
        # Alias so custom prompt_templates written before the rename still resolve.
        "dj_style": station.personality,
        "show_name": show_name,
        "show_block": show_block,
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


def _call_openai_text(prompt: str, config: AppConfig, *, temperature: float | None = None) -> str | None:
    """POST a prompt to OpenAI's Responses API and return the text, or None on failure.

    `temperature` overrides config.openai_text_temperature for this call only —
    useful for tasks where we want lower variance than the default (e.g. news
    bulletins, which should sound professional and predictable).
    """
    if config.script_provider != "openai":
        logger.info("Skipping OpenAI text generation: script_provider=%s", config.script_provider)
        return None
    if not config.openai_api_key:
        logger.warning("Skipping OpenAI text generation: OPENAI_API_KEY is missing")
        return None

    effective_temp = temperature if temperature is not None else config.openai_text_temperature
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps({
            "model": config.openai_text_model,
            "input": prompt,
            "temperature": effective_temp,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {config.openai_api_key}"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=40) as response:  # noqa: S310
            data = json.loads(response.read().decode("utf-8"))
        elapsed = time.perf_counter() - t0
        logger.debug("OpenAI text generation completed", extra={"elapsed_s": round(elapsed, 2), "model": config.openai_text_model})
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - t0
        logger.warning("OpenAI text request failed status=%s elapsed_s=%s", exc.code, round(elapsed, 2))
        return None
    except urllib.error.URLError as exc:
        elapsed = time.perf_counter() - t0
        logger.warning("OpenAI text request failed reason=%s elapsed_s=%s", exc.reason, round(elapsed, 2))
        return None
    except TimeoutError:
        elapsed = time.perf_counter() - t0
        logger.warning("OpenAI text request timed out elapsed_s=%s", round(elapsed, 2))
        return None
    except json.JSONDecodeError:
        elapsed = time.perf_counter() - t0
        logger.warning("OpenAI text response was not valid JSON elapsed_s=%s", round(elapsed, 2))
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
    logger.debug("Generating DJ transition script via OpenAI model=%s", config.openai_text_model)
    prompt = _build_prompt(station, payload, previous_track, next_track, config)
    logger.debug("DJ prompt: %s", prompt)
    text = _call_openai_text(prompt, config)
    if text is None:
        logger.warning("OpenAI DJ script failed; will fall back to sentence pool")
    return text


def generate_news_script(config: AppConfig, newsreader_name: str | None = None) -> str | None:
    """Generate a news bulletin via OpenAI from the configured RSS feed.

    `newsreader_name` is the on-air name to use in the intro/outro. If None, falls
    back to "your newsreader" so the template still has something to substitute.
    Returns None on failure (caller should skip the news segment).
    """
    news_cfg = config.alerts.news
    feed = fetch_top_headlines(news_cfg.rss_url, news_cfg.headline_count)
    if not feed:
        logger.warning("No headlines available; skipping news segment")
        return None

    headlines_block = "\n".join(
        f"- {item['title']}" + (f" — {item['description']}" if item['description'] else "")
        for item in feed["items"]
    )
    fields = {
        "headline_count": len(feed["items"]),
        "rss_source": feed["source"],
        "headlines_block": headlines_block,
        "newsreader_name": newsreader_name or "your newsreader",
    }
    template = news_cfg.prompt_template or DEFAULT_NEWS_PROMPT_TEMPLATE
    try:
        prompt = template.format_map(fields)
    except KeyError as exc:
        logger.warning("news.prompt_template has unknown placeholder %s; using default", exc)
        prompt = DEFAULT_NEWS_PROMPT_TEMPLATE.format_map(fields)
    logger.debug(
        "Generating news bulletin via OpenAI",
        extra={"headline_count": len(feed["items"]), "source": feed["source"], "newsreader": newsreader_name or "anonymous"},
    )
    # News should sound professional and predictable — pull temperature down
    # from the higher default we use for DJ banter.
    result = _call_openai_text(prompt, config, temperature=0.7)
    return substitute_station_placeholder(result, config) if result else result


STATION_ID_CACHE_FILE = Path("generated_station_ids.json")

# Each vibe gets its own LLM call so the model stays focused on one tone per
# batch — much better variety and quality than asking for "vary the vibe" in a
# single 30-line prompt, where the LLM tends to drift and repeat itself.
STATION_ID_VIBES: list[tuple[str, str]] = [
    (
        "classic",
        "Classic, professional radio station IDs. Neutral, clearly enunciated, "
        "the kind a real FM affiliate would use. Confident but not flashy.",
    ),
    (
        "hyped",
        "Punchy and high-energy. Like a DJ shouting over the intro of a hit "
        "single. Big, bold, attention-grabbing. Exclamations are fair game.",
    ),
    (
        "warm",
        "Warm and welcoming. Like a host inviting an old friend in. "
        "Conversational, gentle, intimate — could fit a Sunday morning slot.",
    ),
    (
        "cheeky",
        "Cheeky and playful. A wink at the listener. Lightly irreverent, "
        "mildly self-aware, fun without trying too hard.",
    ),
    (
        "confident",
        "Cool and confident. Late-night-radio swagger. Understated, smooth, "
        "the kind of line a DJ delivers half-smiling.",
    ),
]

STATION_ID_PROMPT_TEMPLATE = """\
Write {count} short station-ID stingers for a radio station called "{station_name}".
Tagline: "{tagline}".

Vibe for this batch: {vibe}

Each stinger should be:
- ONE sentence, 4–10 words. Short and snappy.
- Easy to say out loud.
- Reference the station BY NAME. Write the literal placeholder [[STATION]]
  (with the double square brackets) wherever you'd write the station name —
  the radio software substitutes the spoken pronunciation before audio
  generation. Examples: "This is [[STATION]].", "You're locked in to [[STATION]]."
- Plain text only. No quotation marks, no numbering, no markdown, no emojis,
  no commentary.

Return them as a plain list, one per line. Every line must contain [[STATION]]
at least once."""


def _load_station_id_cache() -> dict[str, list[str]]:
    if not STATION_ID_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(STATION_ID_CACHE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not parse %s; treating as empty", STATION_ID_CACHE_FILE)
        return {}


def _save_station_id_cache(cache: dict[str, list[str]]) -> None:
    try:
        STATION_ID_CACHE_FILE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    except OSError as exc:
        logger.warning("Could not write %s: %s", STATION_ID_CACHE_FILE, exc)


def _parse_phrase_lines(response: str | None) -> list[str]:
    if not response:
        return []
    phrases: list[str] = []
    for line in response.splitlines():
        cleaned = line.strip().strip('"').strip("'").lstrip("-•*0123456789. )").strip()
        if 3 < len(cleaned) < 200:
            phrases.append(cleaned)
    return phrases


def _generate_station_id_batch(
    vibe_name: str,
    vibe_instruction: str,
    count: int,
    station_name: str,
    tagline: str,
    config: AppConfig,
) -> list[str]:
    prompt = STATION_ID_PROMPT_TEMPLATE.format(
        count=count,
        station_name=station_name,
        tagline=tagline,
        vibe=vibe_instruction,
    )
    response = _call_openai_text(prompt, config)
    phrases = _parse_phrase_lines(response)
    logger.debug(
        "Station ID batch generated",
        extra={"vibe": vibe_name, "requested": count, "got": len(phrases)},
    )
    return phrases


def get_station_id_phrases(config: AppConfig) -> list[str]:
    """Return the cached stinger phrases for the current station name.

    Generated once via the LLM on first call and persisted to disk. Generation
    happens in parallel across several distinct "vibe" batches (classic, hyped,
    warm, cheeky, confident) so the resulting set has real tonal variety rather
    than the repetitive output a single big-list LLM call tends to produce.

    If the station name changes, a fresh set is generated and the old set stays
    cached, in case you switch back. Falls back to a single hard-coded line if
    every batch returns nothing usable.
    """
    station_name = config.station.name
    cache = _load_station_id_cache()
    if cached := cache.get(station_name):
        return cached

    total = config.alerts.station_id.phrase_count
    per_vibe = max(1, math.ceil(total / len(STATION_ID_VIBES)))
    tagline = config.station.tagline or ""

    logger.info(
        "Generating station ID phrases via OpenAI",
        extra={
            "station_name": station_name,
            "vibes": len(STATION_ID_VIBES),
            "per_vibe": per_vibe,
            "target_total": total,
        },
    )

    phrases: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(STATION_ID_VIBES)) as pool:
        futures = [
            pool.submit(
                _generate_station_id_batch,
                name, instr, per_vibe, station_name, tagline, config,
            )
            for name, instr in STATION_ID_VIBES
        ]
        for fut in futures:
            try:
                phrases.extend(fut.result())
            except Exception:  # noqa: BLE001
                logger.exception("Station ID vibe batch failed")

    # Dedupe case-insensitively while preserving the original order/casing.
    seen: set[str] = set()
    deduped: list[str] = []
    for phrase in phrases:
        key = phrase.lower().strip(" .!?,")
        if key and key not in seen:
            seen.add(key)
            deduped.append(phrase)

    if not deduped:
        logger.warning("Station ID generation returned no usable phrases; using fallback")
        deduped = [f"This is {STATION_PLACEHOLDER}."]

    # Substitute [[STATION]] with the configured spoken name before caching,
    # so the on-disk JSON and the TTS clip hash both bake in the resolved
    # pronunciation. If the user later changes spoken_name, deleting
    # generated_station_ids.json regenerates with the new spelling.
    deduped = [substitute_station_placeholder(p, config) for p in deduped]
    cache[station_name] = deduped
    _save_station_id_cache(cache)
    return deduped


def generate_ad_script(station: StationConfig, config: AppConfig) -> str | None:
    """Generate a fake-ad script via OpenAI. Returns None on failure (caller should skip the ad)."""
    import random as _random  # local import keeps the module-level random call testable via monkeypatch

    template = config.alerts.ads.prompt_template or DEFAULT_AD_PROMPT_TEMPLATE
    risque_chance = config.alerts.ads.risque_chance
    is_risque = _random.random() < risque_chance
    fields = {
        "station_name": station.name,
        "station_format": station.format,
        "dj_name": station.dj_name,
        "ad_category": _random.choice(AD_CATEGORIES),
        "ad_tone": RISQUE_TONE_HINT if is_risque else "",
    }
    try:
        prompt = template.format_map(fields)
    except KeyError as exc:
        logger.warning("ads.prompt_template has unknown placeholder %s; using default", exc)
        prompt = DEFAULT_AD_PROMPT_TEMPLATE.format_map(fields)
    logger.debug(
        "Generating ad-break script via OpenAI",
        extra={"ad_category": fields["ad_category"][:60], "risque": is_risque},
    )
    result = _call_openai_text(prompt, config)
    return substitute_station_placeholder(result, config) if result else result


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
        f"You're listening to {station.name} with {station.dj_name}, keeping it {station.personality} tonight.",
        f"We just heard {_track_ref(previous_track)}, and up next is {_track_ref(next_track)}.",
        f"{station.tagline}",
    ])

    if payload.include_weather:
        location = config.alerts.weather_location if config else "your area"
        live_weather = fetch_weather_summary(
            location,
            latitude=config.alerts.weather_latitude if config else None,
            longitude=config.alerts.weather_longitude if config else None,
        )
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
            # Replace [[STATION]] placeholders with the configured spoken
            # pronunciation BEFORE splitting into sentences — so the substituted
            # text gets the same trimming/cleaning everything else does.
            scripted = substitute_station_placeholder(scripted, config)
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

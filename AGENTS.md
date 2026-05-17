# AGENTS.md — RadioDunc developer notes

Notes for AI agents working on this codebase. Covers non-obvious design decisions, gotchas, and patterns that aren't obvious from reading the code.

## What this is

A personal AI radio station. FastAPI backend + single-page Web Audio frontend. No broadcast/streaming server — the browser fetches individual audio files from the API and plays them in sequence via Web Audio API. The "radio" illusion comes from pre-generated DJ voice clips that play between tracks.

## Running it

```bash
source .venv/bin/activate
make run          # uvicorn app.main:app --reload
make test         # pytest -q
```

Tests use an in-memory SQLite DB (see `tests/conftest.py`) and mock OpenAI calls. Always run pytest after touching Python.

## Key architecture decisions

### Single-station model
There is exactly one station, defined in `radio_config.json`. The old multi-station model was removed. `config.station` is a `StationConfig`. Ignore anything in the code that looks like it supports multiple stations — it's dead.

### Player state lives in the DB
`PlayerState` (a single-row SQLite table) holds the current queue, now-playing info, and break counters. It's serialised as JSON columns. Load it with `db.query(PlayerState).first()`.

### DJ clip cache
`DJClip` rows store `(script_text, voice, voice_instructions)` → WAV file path. The cache key is `sha256(voice + "\n" + instructions + "\n" + script_text)`. Clips are never regenerated if the hash matches. Ad clips have `is_ad=True` and are pooled: once `pool_size` ad clips are cached, new ad breaks pick randomly from the existing pool instead of generating fresh ones.

### TTS flow
`get_or_create_dj_clip()` in `tts.py` is the single entry point. It checks the DB, generates if missing, writes a WAV to `generated_audio/`, commits a `DJClip` row, and returns `(clip, path, was_cached)`.

### Network drive reliability
Track files live on a network mount. `GET /media/{track_id}` reads the whole file into memory with `Path.read_bytes()` before returning a `Response` — this avoids mid-stream failures from the mount dropping. The frontend also retries failed audio loads up to 3× via `loadWithRetry()`.

### Pre-generation of DJ clips
After each transition, the backend pre-generates the *next* DJ clip in a background thread so TTS latency doesn't stall playback. This happens at the end of `player_next`.

### Ad break follows flag
When an ad break is about to play, `ad_break_follows=True` is passed to DJ script generation. The prompt tells the DJ to tease the break ("coming up after the break, [song]"). The ad clip itself uses a random voice from `config.alerts.ads.voices`.

### News segment
Mirrors the ads architecture. When `alerts.news.enabled` is true and the break is on cadence, `player_next` calls `generate_news_script()` (fetches top N headlines from the RSS feed, asks LLM to write a sober bulletin) and synthesises a clip with one of the configured `news.voices`. The clip URL is returned as `news_clip_url` in the response; the frontend plays it after the DJ clip and before any ad clip. **News clips are not cached** (they go stale within hours) — every break that fires generates fresh audio. The DJ teases the bulletin via `news_break_follows=True` in the DJ script request (same pattern as `ad_break_follows`). On the frontend, news shows a blue `📰 News` badge.

### Audience request flag
Queue items injected via `POST /player/queue/inject` carry `"requested": True`. `player_next` detects this and passes `reason="request"` to `generate_dj_script`, which adds a "someone called in for this" flavour to the prompt.

## Config shape — things that have changed

- `voice_hint` was renamed to `voice` everywhere (station, persona roster entries)
- `ads.voice` (single string) was replaced by `ads.voices` (list of `{voice, voice_instructions}`)
- `ads.pool_size` controls when to stop generating new ad clips (default 100)
- Each `dj_roster` entry and the base station can have `voice` and `voice_instructions` independently
- `alerts.weather_latitude`/`weather_longitude` bypass Open-Meteo geocoding when set — useful for small cities not in their database. `weather_location` is still used as the spoken display name.

The example config is `example-radio_config.json`. The local config is `radio_config.json` (gitignored).

## Station config fields — what each one does

| Field | Affects | Effect |
|---|---|---|
| `dj_style` | LLM prompt | Injected as `DJ: {name} ({style}).` — shapes tone and personality |
| `era` | LLM prompt | Adds `Era: X.` to station context — nudges DJ to reference that period |
| `genre_focus` | LLM prompt | Adds `Genre focus: X, Y.` — prompts genre-aware track connections |
| `description` | LLM prompt | Free-form flavour text appended to the format line |
| `voice_instructions` | TTS | Natural-language delivery hint sent to the TTS model |
| `core_artists` | Scheduler only | When non-empty, queue is built **exclusively** from these artists (exact metadata match). Falls back to full library if none match. |

`genre_focus` and `core_artists` are easy to confuse: `genre_focus` only affects what the DJ *says*, `core_artists` only affects which *tracks get played*.

## DJ persona scheduling

`dj_roster` is an ordered list of `DJPersona` entries with optional `days`, `start_hour`, `end_hour`. `pick_active_persona()` returns the first match. `active_station()` applies the match by merging fields onto the base `StationConfig` (name, style, voice, voice_instructions, prompt_template). The base station is the fallback when nothing matches.

## Frontend (app/ui/)

- `app.js` — all logic; no build step, plain ES modules not used, single file
- `api(url, opts)` — thin fetch wrapper; skips `.json()` on 204 / empty body
- `loadWithRetry(el, url)` — retries `<audio>` src load up to 3× with 2 s gaps
- `triggerTransition()` — called after each track ends; fetches `/player/next`, then `Promise.allSettled([fetchDjClip, loadTrack])` in parallel before playing
- `adBadgeTimer` — tracked globally so stale `setTimeout` from previous transitions can be cancelled
- Queue drag-and-drop uses HTML5 native events; reorder hits `POST /player/queue/reorder`
- Volume is persisted to `localStorage`

## DB migrations

Done inline in `_migrate_drop_legacy_schema()` at startup in `main.py` using raw SQL `PRAGMA table_info` checks. Add new migrations there — no Alembic.

## Logging

`ContextFormatter` in `main.py` appends `key=value` extras from `logger.info(..., extra={...})` calls. Always pass structured context via `extra=` rather than f-strings where it makes sense. Timing is captured with `time.perf_counter()` and logged as `elapsed_s`.

## What not to touch

- `radio_config.json` — Duncan's personal config, gitignored. Propose changes, don't overwrite silently.
- `generated_audio/` — cached TTS WAVs, not committed.
- `radio.db` — SQLite data file, not committed.

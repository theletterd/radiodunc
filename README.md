# RadioDunc

A personal AI radio station that plays your local music library with an LLM-scripted DJ and text-to-speech voiceovers between every track.

## What it does

- Scans your music library (MP3, FLAC, M4A, OGG) and builds a shuffled queue
- Generates DJ transition scripts using OpenAI (GPT-4o-mini by default)
- Synthesises voice clips via OpenAI TTS and caches them so repeats are instant
- Weaves in real weather, live news headlines, and fake ad breaks on a configurable cadence
- Streams audio in your browser via Web Audio API (no icecast, no external broadcast)
- Lets you request tracks, skip, drag-reorder the queue, and veto upcoming songs from the UI

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy the example config and fill in your details:

```bash
cp example-radio_config.json radio_config.json
```

Set your OpenAI API key (required for DJ scripts and TTS):

```bash
export OPENAI_API_KEY="sk-..."
# or add it to a local .env file (gitignored)
echo 'OPENAI_API_KEY="sk-..."' >> .env
```

Start the server:

```bash
make run
# or: uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` and hit **Play**.

## Configuration

`radio_config.json` (gitignored, auto-created from `example-radio_config.json`) controls everything.

### Top-level fields

| Field | Default | Description |
|---|---|---|
| `music_folder` | `~/Music` | Path to your music library |
| `tts_provider` | `"tone"` | `"openai"` for real TTS, `"tone"` for a local beep placeholder |
| `script_provider` | `"template"` | `"openai"` for LLM scripts, `"template"` for canned sentences |
| `openai_text_model` | `"gpt-4o-mini"` | Model used for DJ script generation |
| `openai_tts_model` | `"gpt-4o-mini-tts"` | Model used for TTS synthesis |
| `openai_tts_voice` | `"alloy"` | Default TTS voice (OpenAI voice name) |
| `playlist_artist_repeat_window` | `3` | Minimum tracks between same-artist plays |

### Station

```json
"station": {
  "name": "RadioDunc 107.2 FM",
  "tagline": "Your music, forever.",
  "format": "Eclectic Mixtape",
  "description": "No rules, only great songs.",
  "era": null,
  "genre_focus": ["indie rock", "alternative"],
  "core_artists": [],
  "dj_name": "DJ Name",
  "dj_style": "warm storyteller with dry wit",
  "voice": null,
  "voice_instructions": null,
  "dj_prompt_template": null,
  "dj_roster": []
}
```

**Prompt fields** — all of these feed directly into the LLM script on every transition:

- **`dj_style`** — the DJ's on-air personality. The more vivid, the better: `"warm storyteller with dry wit"` gets a different result from `"hyper-energetic 90s Top 40 host who's had too much coffee"`.
- **`era`** — adds `Era: X.` to the station context, nudging the DJ to reference that period (e.g. `"80s and 90s"`, `"late 60s psychedelia"`). Leave `null` for no era bias.
- **`genre_focus`** — list of genres injected into the station context (e.g. `["indie rock", "post-punk"]`). Prompts the DJ to make genre-aware connections between tracks. Purely a prompt hint — does not filter which tracks get played.
- **`description`** — free-form station flavour text appended to the format line in the prompt.
- **`voice`** — overrides `openai_tts_voice` for this station's DJ.
- **`voice_instructions`** — natural-language delivery hint passed to the TTS model. This is where the magic is: describe the exact vocal character you want, as specifically as you like (pacing, affect, quirks, references).

**Playlist field** — affects track selection, not the script:

- **`core_artists`** — when non-empty, the queue is built *exclusively* from tracks by these artists (exact match on the artist metadata tag). Everything else in your library is excluded. Falls back to the full library if no tracks match. Use this to make a focused artist-spotlight station: `["David Bowie", "Brian Eno", "Roxy Music"]`.

`dj_prompt_template` accepts a Python format string with these placeholders: `{station_name}`, `{dj_name}`, `{dj_style}`, `{station_format}`, `{station_description}`, `{station_era}`, `{station_genre_focus}`, `{previous_track}`, `{next_track}`, `{current_time}`, `{current_weekday}`, `{weather_block}`, `{news_block}`, `{ad_block}`, `{reason_block}`, `{max_sentences}`. Omit it to use the built-in default.

### DJ roster (scheduled personas)

Add entries to `dj_roster` to swap DJ personality by day/hour:

```json
"dj_roster": [
  {
    "name": "Saturday Night Sam",
    "style": "high-energy party host",
    "voice": "fable",
    "voice_instructions": "Upbeat and punchy. Fast-paced with infectious energy.",
    "days": ["friday", "saturday"],
    "start_hour": 20,
    "end_hour": 23
  }
]
```

The first roster entry whose `days`/`start_hour`/`end_hour` matches the current time wins. Falls back to the base station DJ when nothing matches. `voice` and `voice_instructions` are optional per entry.

### Alerts

```json
"alerts": {
  "weather_location": "Portland, OR",
  "local_time_zone": "America/Los_Angeles",
  "weather": { "enabled": true, "every_n_breaks": 4 },
  "news": {
    "enabled": true,
    "rss_url": "https://www.theguardian.com/world/rss",
    "every_n_breaks": 5,
    "headline_count": 3,
    "voices": [
      { "voice": "onyx", "name": "Alex Morgan", "voice_instructions": "Calm, measured BBC newsreader. Authoritative, neutral, clearly enunciated." }
    ],
    "prompt_template": null
  },
  "ads": {
    "enabled": true,
    "voices": [
      { "voice": "echo", "voice_instructions": "Classic radio announcer. Warm, punchy, slightly retro." },
      { "voice": "onyx", "voice_instructions": "Deep and authoritative. Like a prestige brand voiceover." }
    ],
    "pool_size": 100,
    "every_n_breaks": 6
  }
}
```

- **weather** — pulls live conditions from Open-Meteo, reported in Celsius. Set `weather_latitude` and `weather_longitude` to bypass geocoding (useful for small cities or suburbs the Open-Meteo geocoder doesn't recognise — find coords at latlong.net). `weather_location` is still used as the spoken name in DJ lines.
- **news** — a full news bulletin segment that plays right after the DJ's hand-off. Fetches the top `headline_count` items from any RSS feed (default: Guardian World), and asks the LLM to write a short professional bulletin with an intro / body / outro structure. Voiced by one of the configured `voices` (default: `onyx` as "Alex Morgan", with BBC-newsreader instructions). Each voice can have its own `name`, which is referenced in the bulletin's intro and outro. Never cached — every bulletin is fresh.
- **ads** — generates fake ad-break scripts via OpenAI; one voice is picked at random per break. Once `pool_size` clips are cached, new breaks are drawn from the pool instead of generating fresh ones

## Project structure

```
app/
  main.py          — FastAPI routes
  config.py        — config schema and file load/save
  database.py      — SQLAlchemy engine/session
  models.py        — ORM models (Track, DJClip)
  schemas.py       — Pydantic request/response schemas
  scanner.py       — library scan and metadata extraction
  dj_scripts.py    — DJ script generation (prompt building, OpenAI call)
  tts.py           — TTS synthesis and clip cache
  weather.py       — Open-Meteo weather fetch
  news.py          — RSS news headline fetch
  ui/
    index.html     — single-page UI
    app.js         — Web Audio playback, queue management, UI logic
    styles.css     — dark theme with pink accents
example-radio_config.json  — template config (committed)
radio_config.json          — your local config (gitignored)
```

## Running tests

```bash
pytest -q
# or: make test
```

## API overview

| Method | Path | Description |
|---|---|---|
| `POST` | `/library/scan` | Scan music folder, extract metadata |
| `GET` | `/library/status` | Track count and last scan timestamp |
| `GET` | `/tracks` | List all scanned tracks |
| `POST` | `/player/play` | Start playback (builds initial queue) |
| `POST` | `/player/next` | Advance to next track, trigger DJ clip |
| `POST` | `/player/stop` | Stop playback |
| `GET` | `/player/status` | Current player state |
| `GET` | `/player/queue` | Current queue |
| `DELETE` | `/player/queue/{position}` | Remove a queue item |
| `POST` | `/player/queue/reorder` | Drag-reorder queue items |
| `POST` | `/player/queue/inject` | Inject a requested track |
| `POST` | `/player/queue/extend` | Append more tracks to the queue |
| `GET` | `/media/{track_id}` | Serve audio file |
| `GET` | `/config` | Read current config |
| `PUT` | `/config` | Update config |

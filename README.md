# Local AI Radio Station Generator (Backend Skeleton)

This repository contains a FastAPI backend skeleton for scanning a local music library and storing metadata for a future AI radio station app.

## Features (current)

- SQLite database (`radio.db`)
- SQLAlchemy models:
  - `Track`
  - `Station`
  - `DJClip`
- Config file support (checked-in `example-radio_config.json` + local `radio_config.json`) for:
  - default music folder to scan
  - weather alerts location
  - local time zone
  - news alert preferences
  - station persona presets (format/tagline/DJ naming/style/voice hints)
  - optional `station_generation_seed` for deterministic station shuffle order
- configurable `playlist_artist_repeat_window` anti-repeat setting for queue scheduling
- `GET /config` and `PUT /config`
- `POST /library/scan` endpoint:
  - accepts optional folder path
  - falls back to configured `music_folder`
  - scans recursively for `.mp3`, `.flac`, `.m4a`, `.ogg`
  - extracts metadata using Mutagen
  - deduplicates by `file_path`
- `GET /tracks` endpoint to list scanned tracks
- `POST /stations/generate` endpoint for phase 2 station creation
- `GET /stations` endpoint to list generated stations
- `POST /stations/{station_id}/queue` endpoint for phase 3 playlist scheduling
- `POST /stations/{station_id}/dj-clip` endpoint for phase 5 clip synthesis + cache reuse
- Basic error handling for invalid paths and scan failures

## Project Structure

- `app/main.py` — FastAPI app and routes
- `app/config.py` — app config schema + file load/save
- `app/database.py` — SQLAlchemy engine/session setup
- `app/models.py` — ORM models
- `app/schemas.py` — Pydantic schemas
- `app/scanner.py` — audio scan + metadata extraction logic
- `app/stations.py` — station generation logic
- `example-radio_config.json` — template config committed to git
- `radio_config.json` — local personal config (gitignored, auto-created)
- `requirements.txt` — dependencies

## Setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run the server:

```bash
uvicorn app.main:app --reload
```

Or with Make:

```bash
make run
```

Server will start at `http://127.0.0.1:8000`.

## Running Tests

Run the test suite directly:

```bash
pytest -q
```

Or with Make:

```bash
make test
```

## Common Make Targets

- `make install` — install Python dependencies from `requirements.txt`
- `make run` — run the FastAPI app with auto-reload
- `make test` — run the unit test suite

## API Usage

### Read config

```bash
curl http://127.0.0.1:8000/config
```

### Update config

```bash
curl -X PUT http://127.0.0.1:8000/config \
  -H "Content-Type: application/json" \
  -d '{
    "music_folder": "~/Music",
    "station_generation_count": 6,
    "alerts": {
      "weather_location": "Portland, OR",
      "local_time_zone": "America/Los_Angeles",
      "news": {
        "enabled": true,
        "categories": ["local", "music"],
        "briefing_minutes": 20
      }
    }
  }'
```

### Scan a library

```bash
curl -X POST http://127.0.0.1:8000/library/scan \
  -H "Content-Type: application/json" \
  -d '{"folder_path": "/path/to/your/music"}'
```

You can also omit `folder_path` to use `music_folder` from your local `radio_config.json` (auto-created from `example-radio_config.json`).

### List tracks

```bash
curl http://127.0.0.1:8000/tracks
```

### Generate stations

Station generation now uses `station_presets` from config rather than hardcoded code presets. Set `station_generation_seed` to any integer to get reproducible preset shuffle order for a given library.

```bash
curl -X POST http://127.0.0.1:8000/stations/generate \
  -H "Content-Type: application/json" \
  -d '{"count": 6}'
```

### List stations

```bash
curl http://127.0.0.1:8000/stations
```

## Notes

- Metadata extraction depends on file tags; missing tags are stored as `null`.
- Phase 2.5 adds config-driven station persona presets, deterministic optional seed control, and fail-fast config validation for invalid preset entries.

## Project Planning

See `plan.md` for phased roadmap and status tracking.


### Generate station queue

```bash
curl -X POST http://127.0.0.1:8000/stations/1/queue \
  -H "Content-Type: application/json" \
  -d '{"size": 12, "seed": 123}'
```

This queue generator prefers station `core_artists` when available, avoids same-artist repetition within `playlist_artist_repeat_window`, and supports deterministic output via request `seed`.


### Synthesize a DJ clip

```bash
curl -X POST http://127.0.0.1:8000/stations/1/dj-clip \
  -H "Content-Type: application/json" \
  -d "{"script_text":"You're listening to Night Drive FM.","voice":"default"}"
```

The clip is cached by a script+voice hash, so repeated requests return the same stored audio path.

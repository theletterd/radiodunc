# Local AI Radio Station Generator (Backend Skeleton)

This repository contains a FastAPI backend skeleton for scanning a local music library and storing metadata for a future AI radio station app.

## Features (current)

- SQLite database (`radio.db`)
- SQLAlchemy models:
  - `Track`
  - `Station`
  - `DJClip`
- Config file support (`radio_config.json`) for:
  - default music folder to scan
  - weather alerts location
  - local time zone
  - news alert preferences
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
- Basic error handling for invalid paths and scan failures

## Project Structure

- `app/main.py` — FastAPI app and routes
- `app/config.py` — app config schema + file load/save
- `app/database.py` — SQLAlchemy engine/session setup
- `app/models.py` — ORM models
- `app/schemas.py` — Pydantic schemas
- `app/scanner.py` — audio scan + metadata extraction logic
- `app/stations.py` — station generation logic
- `radio_config.json` — configurable local defaults
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

Server will start at `http://127.0.0.1:8000`.

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

You can also omit `folder_path` to use `music_folder` from `radio_config.json`.

### List tracks

```bash
curl http://127.0.0.1:8000/tracks
```

### Generate stations

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
- Phase 2 now includes station generation and configurable weather/news/local-time constants.

## Project Planning

See `plan.md` for phased roadmap and status tracking.

# Local AI Radio Station Generator (Backend Skeleton)

This repository contains a FastAPI backend skeleton for scanning a local music library and storing metadata for a future AI radio station app.

## Features (current)

- SQLite database (`radio.db`)
- SQLAlchemy models:
  - `Track`
  - `Station`
  - `DJClip`
- `POST /library/scan` endpoint:
  - accepts a folder path
  - scans recursively for `.mp3`, `.flac`, `.m4a`, `.ogg`
  - extracts metadata using Mutagen
  - deduplicates by `file_path`
- `GET /tracks` endpoint to list scanned tracks
- Basic error handling for invalid paths and scan failures

## Project Structure

- `app/main.py` — FastAPI app and routes
- `app/database.py` — SQLAlchemy engine/session setup
- `app/models.py` — ORM models
- `app/schemas.py` — Pydantic schemas
- `app/scanner.py` — audio scan + metadata extraction logic
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

### Scan a library

```bash
curl -X POST http://127.0.0.1:8000/library/scan \
  -H "Content-Type: application/json" \
  -d '{"folder_path": "/path/to/your/music"}'
```

### List tracks

```bash
curl http://127.0.0.1:8000/tracks
```

## Notes

- Metadata extraction depends on file tags; missing tags are stored as `null`.
- This is Phase 1 backend skeleton; station generation, playlist scheduling, DJ scripting, TTS, and playback orchestration are planned next.


## Project Planning

See `plan.md` for phased roadmap and status tracking.

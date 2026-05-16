# Local AI Radio Station Generator — Project Plan

This document tracks phased delivery for the local AI radio project and is intended to be updated as milestones are completed.

## Status Legend

- `TODO` — not started
- `IN PROGRESS` — active development
- `DONE` — completed and validated
- `BLOCKED` — waiting on dependency/decision

---

## Phase 1 — Library Scanner + Metadata Store

**Status:** `DONE` (backend skeleton implemented)

### Goals
- Scan a local folder recursively for supported audio files.
- Extract metadata (title, artist, album, year/date, genre, duration, bitrate).
- Persist track metadata in SQLite with deduplication by `file_path`.
- Expose endpoints for scanning and listing tracks.

### Deliverables
- FastAPI app bootstrapped.
- SQLAlchemy models for `Track`, `Station`, `DJClip`.
- `POST /library/scan`.
- `GET /tracks`.
- Setup docs and requirements.

### Exit Criteria
- Running app can scan a sample directory.
- Duplicate re-scan does not create duplicate rows.
- Invalid folder paths return clear API error.

---

## Phase 2 — Station Generation

**Status:** `DONE` (initial generation + config implemented)

### Goals
- Analyze scanned track library and derive multiple fictional station concepts.
- Capture station identity/persona metadata suitable for DJ scripting and scheduling.
- Persist generated stations.

### Deliverables
- `POST /stations/generate` endpoint.
- `GET /stations` endpoint.
- Station generation service using library summary stats.
- Config schema (weather/news/fake ads flags, DJ style, core artists).

### Exit Criteria
- Generates 5–10 distinct stations from a non-trivial library.
- Station definitions persisted and retrievable.
- Reasonable diversity across format/persona outputs.


---

## Phase 2.5 — Persona & Config Hardening

**Status:** `DONE` (config-driven personas + seed + validation implemented)

### Goals
- Move announcer persona definitions from hardcoded presets to configuration-driven data.
- Make station generation deterministic when desired and easier to tune without code edits.
- Tighten config validation and startup behavior for local-vs-template config files.

### Deliverables
- Config schema support for `station_presets` (format, tagline, dj_name strategy, dj_style, optional voice hints).
- Station generation update to read persona presets from config with safe defaults/fallbacks.
- Optional seed parameter/config value for reproducible station generation order.
- Startup/config validation errors that clearly identify invalid preset entries.
- README update with persona configuration examples and migration notes.

### Exit Criteria
- Editing `radio_config.json` can fully change generated station personas without code changes.
- Invalid persona config fails fast with actionable validation messages.
- Generated station diversity remains comparable to current preset approach.
- Optional deterministic mode reproduces identical station sets for same library + seed.

---

## Phase 3 — Playlist Scheduler

**Status:** `DONE` (queue scheduler + endpoint + anti-repeat config implemented)

### Goals
- Build track selection logic per station profile.
- Avoid artist repetition within a configurable recent window.
- Produce deterministic queue generation when requested.

### Deliverables
- Playlist scheduling module/service.
- Queue creation endpoint(s) for a station.
- Configuration for anti-repeat constraints.

### Exit Criteria
- Queue populated with station-aligned tracks.
- No same-artist repeats within configured threshold.
- Scheduler can regenerate queue on demand.

---

## Phase 4 — DJ Script Generation

**Status:** `TODO`

### Goals
- Generate short between-track DJ scripts in station-specific voice.
- Include periodic IDs, weather/news stingers, and fake ads based on station config.

### Deliverables
- Prompt templates + guardrails for concise scripts.
- Script generation endpoint/service.
- Structured context payload (prev/next track, station persona, timing budget).

### Exit Criteria
- Scripts consistently remain within 1–3 sentences.
- Scripts reference contextual track/station details.
- Style variation is consistent with station persona.

---

## Phase 5 — TTS + Clip Caching

**Status:** `TODO`

### Goals
- Convert DJ scripts to speech clips.
- Cache generated clips to avoid repeat synthesis.

### Deliverables
- TTS provider adapter interface.
- Clip cache keyed by script/voice hash.
- Audio file storage and retrieval helpers.

### Exit Criteria
- Same script+voice request returns cached clip.
- New script generates playable clip.
- Cache metadata stored and queryable.

---

## Phase 6 — Playback Orchestration (MVP)

**Status:** `TODO`

### Goals
- Play queue as `song → DJ clip → next song` sequence.
- Track playback state and controls.

### Deliverables
- Player service with queue/state machine.
- Control endpoints (play, next, stop, status).
- Basic local playback integration.

### Exit Criteria
- End-to-end playback loop works for a station.
- Control endpoints update state reliably.
- Player recovers gracefully from missing/bad clips.

---

## Phase 7 — Web UI (Local)

**Status:** `TODO`

### Goals
- Provide local UI for scanning, station selection, and playback controls.

### Deliverables
- Library setup view.
- Station picker cards.
- Player view (now playing, up next, station info, controls).

### Exit Criteria
- User can complete end-to-end workflow from browser.
- UI reflects real-time player status.

---

## Phase 8 — Radio Polish

**Status:** `TODO`

### Goals
- Improve realism and production quality.

### Deliverables
- Music ducking/crossfades.
- Loudness normalization.
- Time-of-day programming.
- Better weather/news insertion cadence.
- Expanded persona controls.

### Exit Criteria
- Broadcast feels coherent and “radio-like.”
- Audio transitions are smooth and non-jarring.

---

## Open Questions / Decisions Log

- Choose first TTS provider and fallback strategy.
- Decide local-only vs optional cloud LLM/TTS support model.
- Define persistence strategy for generated playlists and playback history.
- Establish test strategy for audio pipeline (unit vs integration fixtures).

---

## Next Recommended Work Item

Move to **Phase 2.5** by making announcer personas configuration-driven and hardening station-generation config validation.

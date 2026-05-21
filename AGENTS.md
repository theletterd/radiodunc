# AGENTS.md — RadioDunc developer notes

Notes for AI agents working on this codebase. Covers non-obvious design decisions, gotchas, and patterns that aren't obvious from reading the code.

## What this is

A personal AI radio station. FastAPI backend + single-page Web Audio frontend. No broadcast/streaming server — the browser fetches individual audio files from the API and plays them in sequence via Web Audio API. The "radio" illusion comes from pre-generated DJ voice clips that play between tracks.

## Running it

```bash
source .venv/bin/activate
make run          # uvicorn app.main:app --reload
make test         # pytest -q AND npm test (both Python + JS)
make test-py      # Python only
make test-js      # JS only (vitest, faster — sub-second)
```

Tests use an in-memory SQLite DB (see `tests/conftest.py`) and mock OpenAI calls. Always run `make test` after touching Python or app/ui/app.js — both suites are fast.

## Key architecture decisions

### Single-station model
There is exactly one station, defined in `radio_config.json`. The old multi-station model was removed. `config.station` is a `StationConfig`. Ignore anything in the code that looks like it supports multiple stations — it's dead.

### Player state lives in the DB
`PlayerState` (a single-row SQLite table) holds the current queue, now-playing info, and break counters. It's serialised as JSON columns. Load it with `db.query(PlayerState).first()`.

### DJ clip cache
`DJClip` rows store `(script_text, voice, voice_instructions)` → MP3 file path. The cache key is `sha256(voice + "\n" + instructions + "\n" + script_text)`. Clips are never regenerated if the hash matches. Ad clips have `is_ad=True` and are pooled: once `pool_size` ad clips are cached, new ad breaks pick randomly from the existing pool instead of generating fresh ones.

### TTS flow
`get_or_create_dj_clip()` in `tts.py` is the single entry point. It checks the DB, generates if missing, writes an MP3 to `generated/<subdir>/`, commits a `DJClip` row, and returns `(clip, path, was_cached)`. `clip_type` arg routes to the right subdir: `transitions/`, `ads/`, `news/`, or `station_ids/`. Default is `transitions`. OpenAI TTS is asked for `response_format="mp3"` — smaller than WAV with no audible difference for speech. The `ToneTTSProvider` (dev fallback) still writes WAV.

### Network drive reliability
Track files live on a network mount. `GET /media/{track_id}` reads the whole file into memory with `Path.read_bytes()` before returning a `Response` — this avoids mid-stream failures from the mount dropping. The frontend also retries failed audio loads up to 3× via `loadWithRetry()`.

### Pre-generation of DJ clips (late-generate pattern)
The client triggers prefetch ~20 seconds before each track ends by calling `POST /player/prefetch`. The endpoint reads current queue state and spawns `_prefetch_dj_clip` in a background thread. The result is stored in the in-memory `_prefetch_cache` keyed by `target_idx`. On the next `/player/next`, `_take_prefetched(target_idx)` pops it and uses it without re-generating. Skips bypass the cache (`reason=="skip"` invalidates because the prompt context differs).

Late-generate (not generate-on-transition) is deliberate: it means a quick skip in the first 80% of a track *doesn't* burn an OpenAI call on a clip we'd throw away. The 20 s lead is enough for a typical 2–3 s round trip.

### News clip caching (always-async, background TTL refresh)
News is expensive (~5 s LLM + TTS combined). `get_news_clip()` **never blocks** on regeneration. State machine:
- fresh (< 20 min) → return cached
- aging (20–30 min) → return cached AND spawn background refresh
- expired (> 30 min) → return `None` AND spawn refresh
- empty cache → return `None` AND spawn refresh

`_attach_news` treats `None` as "skip the news segment this round" — the user just gets a transition without news that one time, and the background refresh has fresh material by the next news-cadence hit. The startup warmup (in `player_play`) seeds the cache so the user's first news transition almost always hits the fresh path.

An `_news_refresh_in_flight` flag prevents concurrent refreshes. **Important**: the thread spawn is deliberately *outside* the cache lock — `_refresh_news_background` re-acquires the same lock in its `finally` clause, so spawning inside would deadlock if the thread ever ran synchronously (which it does in tests with a fake `Thread`).

### Cache warmup on player_play
When the user hits Play, `_warm_caches_background` spawns a daemon thread that pre-generates the things the first transition would otherwise have to wait for: station-ID phrases (5 parallel LLM calls if uncached), the first news bulletin, and one stinger TTS clip if the pool is empty. Pure best-effort — wrapped in `except Exception` so a warmup failure can never break playback.

### Segment attachment helpers
`player_next` delegates each optional segment to a helper: `_attach_news`, `_attach_ad`, `_attach_station_id`. Each returns `(url, script)` (or just `url` for station_id) and owns its own logging, error handling, and cache interaction. Cadence checks stay in `player_next`.

### Station ID stingers
Short "This is RadioDunc 107.2 FM" throws played right after every **ad break OR news segment** (gates on `ad_clip_url or news_clip_url` in `player_next`). Phrases are LLM-generated once per station name and cached on disk in `generated_station_ids.json`. Generation runs **5 parallel LLM calls** in `ThreadPoolExecutor`, one per "vibe" (classic / hyped / warm / cheeky / confident) — this gives real tonal variety, which a single big-list prompt does poorly because LLMs anchor on the last item. Default `phrase_count` is 40 (8 per vibe × 5 vibes). TTS clips are cached forever via the standard hash.

### Skip stinger (different from the post-segment one above)
When a user hits Next, there's unavoidable dead-air while the DJ clip generates. The client schedules a **3 s setTimeout** at the start of `triggerTransition('user')`. If the timer fires before the DJ clip is ready, it fetches `GET /player/stinger-url` (which returns a random clip from the cached station-ID pool) and plays it. `stingerEndTime` tracks the AudioContext end so `djStart` is `max(ctx.currentTime + 0.05, stingerEndTime + 0.1)` — DJ waits for the stinger if it's still playing instead of overlapping. Auto-advance doesn't get a stinger (prefetch covers that latency).

### Voice preview endpoint
`POST /tts/preview {text, voice, voice_instructions}` returns a `clip_url`. Used by the persona-editor UI's ▶ Preview button so users can audition a voice change without waiting for a transition. Clips live under `generated/previews/` and are cached forever via the standard hash so repeated previews of the same combo are free.

### Anti-anchoring: Python picks from a list, LLM gets one focused brief
Same pattern shows up in two places: station-ID vibes (5 batches) and ad categories (16 in `AD_CATEGORIES`). Whenever the LLM has to pick from an embedded list, it over-indexes on the last item (the dating-app issue, before the fix). Move the variance to Python and inject a single category/vibe per call. This is the right reach whenever output feels samey.

`risque_chance` on `AdBreakPreferences` follows the same principle for a different axis: Python rolls a die per call (default 0.1) to decide whether to inject the risqué tone hint, rather than asking the LLM to "occasionally" do it.

### Per-task LLM temperature
`config.openai_text_temperature` (default 1.2) controls sampling variance for text generation — a small bump above the API default (1.0) gives noticeably more variety in DJ banter, ad copy, and stinger phrases without hurting coherence at short lengths. `_call_openai_text(prompt, config, *, temperature=None)` accepts a per-call override. News bulletins pass `temperature=0.7` because we want professional and predictable, not creative.

### Negative-prompt anti-patterns
When LLM output keeps falling into a stock phrase (e.g. "Well, well, well…" on skips, dating-app spots in ads), the fix is usually to **name the antipattern explicitly** in the prompt and give a positive alternative. See `reason_block` for `reason="skip"` in `_build_prompt` for the pattern.

### Ad break follows flag
When an ad break is about to play, `ad_break_follows=True` is passed to DJ script generation. The prompt tells the DJ to tease the break ("coming up after the break, [song]"). The ad clip itself uses a random voice from `config.alerts.ads.voices`.

### News segment
When `alerts.news.enabled` is true and the break is on cadence, `_attach_news` consults the news cache (see "News clip caching" above) and returns the URL + script. Bulletins are generated via `generate_news_script()` (fetches top N headlines from the RSS feed, asks LLM to write an intro/body/outro structured bulletin in the voice of a specific newsreader by name). The DJ teases the bulletin via `news_break_follows=True` in the DJ script request (same pattern as `ad_break_follows`). On the frontend, news shows a blue `📰 News` badge.

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
| `personality` | LLM prompt | Injected as `DJ: {name} ({personality}).` — what the DJ SAYS: attitude, slang, vibe. Separate from voice/voice_instructions which control HOW they sound. (Old `dj_style` field auto-migrates.) |
| `era` | LLM prompt | Adds `Era: X.` to station context — nudges DJ to reference that period |
| `genre_focus` | LLM prompt | Adds `Genre focus: X, Y.` — prompts genre-aware track connections |
| `description` | LLM prompt | Free-form flavour text appended to the format line |
| `voice_instructions` | TTS | Natural-language delivery hint sent to the TTS model |
| `core_artists` | Scheduler only | When non-empty, queue is built **exclusively** from these artists (exact metadata match). Falls back to full library if none match. |

`genre_focus` and `core_artists` are easy to confuse: `genre_focus` only affects what the DJ *says*, `core_artists` only affects which *tracks get played*.

## DJ persona scheduling

`dj_roster` is an ordered list of `DJPersona` entries. Each persona has a list of `DJShift` entries — each `{day, start_hour, end_hour}` — so one persona can have different hours on different days (e.g. fri 20-23 + sat 19-01 as a single persona). `pick_active_persona()` returns the **first match in roster order**, so order = priority. `active_station()` applies the match by merging fields onto the base `StationConfig` (`dj_name`, `personality`, `voice`, `voice_instructions`, `dj_prompt_template`). The base station is the fallback when no persona matches.

Old configs with `days` + `start_hour` + `end_hour` (a single hour range applied to all selected days) auto-migrate to `shifts` on load via a `model_validator`. Don't write that old shape in new code; the migration only exists for backwards compatibility.

### Schedule editor UI
A weekly grid (7 columns × 24 rows) lives in a left-sidebar takeover (click "📅 The Schedule"). Persona shifts render as coloured blocks (palette of 8 hues rotates by roster index); the current hour is highlighted with a pulsing white outline; wrap-around shifts render as TWO blocks across midnight. Blocks support:
- **click** → opens a persona editor drawer (form: name, personality, voice, voice_instructions with ▶ Preview button, shifts list)
- **drag top/bottom edge** → snap-to-hour resize (skipped on wrap-around blocks for simplicity)
- **drag body** → snap-to-cell move (4 px click-vs-drag threshold; duration preserved; clamped to mon-sun and 0-23)

A `_suppressNextClick` mutex stops a successful drag from also opening the editor on mouseup.

## Frontend (app/ui/)

- `app.js` — all logic; no build step, plain ES modules not used, single file
- `api(url, opts)` — thin fetch wrapper; skips `.json()` on 204 / empty body
- `loadWithRetry(el, url)` — retries `<audio>` src load up to 3× with 2 s gaps
- `triggerTransition()` — called when a track ends or user hits Next; fetches `/player/next`, then `Promise.allSettled([fetchDjClip, loadTrack, fetchNewsClip, fetchAdClip, fetchStationIDClip])` in parallel before playing
- `scheduleSegment(buf, startAt, label)` — places any clip on the AudioContext timeline; segment order is DJ → News → Ad → Station ID → Track
- `_modeTimers` — array of `{id, audioTime, mode, label}` entries; each clip schedules a badge transition. `audioTime` is the AudioContext clock target so timers can be **rescheduled correctly across pause/resume** (real wall-clock delay doesn't match AudioContext time after suspend)
- `setOnAirMode(mode)` — switches the `nowPlaying` element's mode and animates the label change with `el.animate()` (slide-up out, slide-up in). `renderPlayer()` only writes to `nowPlaying` when `onAirMode === 'track'` so background polls can't stomp a live badge
- **Pause/resume**: `AudioContext.suspend()` freezes everything; resume reschedules each `_modeTimer` based on `audioTime - ctx.currentTime`
- **Resume after page refresh**: server still says `is_playing=true` but client has no `ctx`. Play button detects this and calls `resumeAfterRefresh()` which reuses `_playCurrentTrackFromServer()` instead of rebuilding the queue via `/player/play`. Current track restarts from position 0 (no in-track persistence)
- **Prefetch timer**: client schedules `POST /player/prefetch` at `(duration - 20 s)` per track. Cancelled at the start of every transition and on stop, same lifecycle as `autoTriggerTimer`
- **Skip stinger timer**: client schedules `_playSkipStinger` at `+3 s` from `triggerTransition('user')` start. Fetches `GET /player/stinger-url`, decodes, schedules via `scheduleSegment`. If the DJ clip arrives first, the timer is cancelled. If the stinger plays, `djStart` waits for `stingerEndTime` so they don't overlap.
- Queue drag-and-drop uses HTML5 native events; reorder hits `POST /player/queue/reorder`
- Volume is persisted to `localStorage`

### Playback instrumentation
`_logPlayback(event, fields)` captures full state (`serverIsPlaying, paused, transitioning, hasCtx, onAirMode`) at every playback entry point — `triggerTransition`, `startPlayback`, `resumeAfterRefresh`, `stopPlayback`, `pausePlayback`, `resumePlayback`, the autoTrigger callback, and the visibilitychange handler. Used to diagnose intermittent bugs (e.g. spurious playback after laptop wake) from a console transcript.

`triggerTransition` also has a defensive guard at the top: if `serverState?.is_playing` is false, it `console.warn`s and bails. Suspected source of the lid-wake bug class — a stale timer firing after `stopPlayback` cleared the local state. Symptom is suppressed; logs tell us when it would have happened.

## JS tests (Vitest + happy-dom)

`tests/js/` holds UI tests run via `npm test` (or `make test` for both Python + JS). Vitest config in `vitest.config.js`; global stubs (AudioContext, Audio.play, Element.animate, fetch) in `tests/js/setup.js`.

`tests/js/_loadApp.js` loads `app/ui/app.js` into the test's global scope. Because Node's module-context eval doesn't promote function declarations to globalThis the way browser script-tag eval does, the loader appends a small re-export block listing every function name the tests need (`EXPORTED_NAMES`). It also injects accessors for `let`-bound internals (`__getCtx`, `__setPaused`, `__getModeTimers`, etc.) since lexical bindings can't be assigned to globalThis directly from outside the module scope.

When adding a new top-level function and wanting to test it: add its name to `EXPORTED_NAMES`. When you need to read/write a private `let` binding: add an accessor in the suffix block.

## DB migrations

Done inline in `_migrate_drop_legacy_schema()` at startup in `main.py` using raw SQL `PRAGMA table_info` checks. Add new migrations there — no Alembic.

## Logging

`ContextFormatter` in `main.py` appends `key=value` extras from `logger.info(..., extra={...})` calls. Always pass structured context via `extra=` rather than f-strings where it makes sense. Timing is captured with `time.perf_counter()` and logged as `elapsed_s`.

**Levels**: lifecycle events (play start/stop, library scans, news cache refreshes, weather lookups) stay at INFO. **Per-event noise goes to DEBUG**: every `/player/next`, every queue mutation, every prefetch, every TTS call, every clip cache hit. The middleware request.start/end pair is also DEBUG (10 s status polls would spam INFO otherwise). The `_log_event(event, level=logging.DEBUG, **fields)` helper takes a level kwarg; module-level `logger.debug` / `logger.info` for direct calls. Default log level is INFO via `LOG_LEVEL` env var.

## What not to touch

- `radio_config.json` — Duncan's personal config, gitignored. Propose changes, don't overwrite silently. New clones bootstrap from `example-radio_config.json` (committed) into this file on first load.
- `generated/` — runtime cache of TTS MP3s + image-gen PNGs (renamed from `generated_audio/` in PR #159 after avatars joined the party). Not committed. Subdirs: `transitions/`, `ads/`, `news/`, `station_ids/`, `previews/`, `dj_icons/`.
- `app/seed/dj_icons/` — bundled avatar PNGs that ship with the example roster. The `/media/dj-icon/{id}` route serves from `generated/dj_icons/` first and falls back to here, so fresh clones see the shipped DJs illustrated without anyone having to regenerate. Filenames are the DJ UUIDs from `example-radio_config.json`.
- `radio.db` — SQLite data file, not committed.

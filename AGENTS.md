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
Queue items injected via `POST /player/queue/inject` carry `"requested": True`. Both the live-generation path in `player_next` AND the background `_prefetch_dj_clip` worker (PR #160) detect this and pass `reason="request"` to `generate_dj_script`, which adds a "someone called in for this" flavour to the prompt. Without the prefetch-path fix, a requested track that happened to be prefetched would lose its request framing — the cached "auto" clip would shadow the live path. `queue_inject` also clears the prefetch cache for `position="next"` inserts (the next-track changed) but NOT for `position="end"` inserts (tail-appended; the immediate next is unchanged).

### Self-ID directive
Real DJs say "you're listening to X, with yours truly Y" occasionally. The LLM doesn't volunteer this without a nudge — and a permanent nudge would make every transition a self-ID. `_build_prompt` rolls `random.random() < _SELF_ID_CHANCE` (= `1/3`) and on a hit injects a `self_id_block` directive with the actual DJ + show names baked in; on a miss the block is empty. Probabilistic Python-side because the LLM is stateless across calls — "occasionally" can't be enforced from inside the prompt. `_SELF_ID_CHANCE` is the only tunable; 0 disables.

### Show-takeover handoff
When a show changes on air (DJ swap, same DJ different show, real DJ → Ghost, etc), the next transition opens with a live handoff that's true to the incoming DJ's personality — warm if they'd be warm, mock-shady if their style clashes with the outgoing DJ, gushing if they're a fan, dryly relieved if "finally". The directive explicitly names personality as the hook, so the LLM uses the persona field rather than emitting a neutral baton-pass. `_build_prompt` remembers the previous prompt's `(dj_id, show_id, dj_name)` tuple in module-level `_last_handoff_state` and injects `handoff_block` whenever the tuple changes. First prompt after process start is bootstrap-only — no fire, because we can't reference a takeover we didn't witness. Warm branch when the outgoing DJ name is known and different (handoff colour aimed at them); cold branch otherwise (same DJ flipping the framing into a new show; or no prior name). Suppresses `self_id_block` when it fires — a handoff already self-IDs, doubling up reads as over-announcing. Tests use an autouse fixture that calls `_reset_handoff_state()` so order-of-execution can't bleed tuples between cases.

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

## DJ-vs-Show architecture

Two separate concepts on `StationConfig`:

- **`djs: list[DJ]`** — reusable DJ identities. Each `DJ` has `id` (UUID), `name`, `personality`, `voice`, `voice_instructions`, `prompt_template`. No scheduling info — a DJ is just a character.
- **`shows: list[Show]`** — bindings that put a DJ on the air. Each `Show` has `id` (UUID), optional `name` (e.g. "After Hours"), `dj_id` (FK into `djs` — `None` means the station-level fallback DJ, "The Ghost", hosts the slot), and `shifts: list[DJShift]` (`{day, start_hour, end_hour}`).

The resolver — `pick_active_persona(station, now)` in `dj_scripts.py` — walks `station.shows` in order, returns the DJ for the first Show whose shifts cover `now`, or `None` if no Show matches or the matching Show has `dj_id=None`. `active_station()` then merges the matched DJ's fields onto the base station via `model_copy`, preserving station-level identity (`dj_name`/`personality`/etc.) as the fallback when nothing matches.

`active_station()` flattens the DJ identity into the station fields and **drops the id**. Callers that need the active DJ's id (the on-air-badge avatar URL is the main one) use `active_dj()` instead — thin wrapper over `pick_active_persona` that handles the same timezone-aware `now`-defaulting. The on-air badge avatar reads `serverState.station.active_dj_id` (exposed separately on `StationOut`).

**The Ghost** is the convention for the station-level default DJ. In Duncan's config it's "The Ghost — the spectre that haunts the empty hallways of this radio station", with `voice: onyx` and theatrical/slow voice instructions. The schema doesn't enforce this — `station.dj_name`/etc. are just text — but configs that name their fallback persona deliberately (rather than e.g. "DJ Default") give the silent hours a personality.

**`{show_name}` and `{show_block}`** placeholders are exposed to the DJ prompt template. `{show_name}` is the raw active-Show name (empty when no Show matches); `{show_block}` is a pre-formatted hint sentence ("Current show: 'After Hours' — if its vibe contrasts with your DJ persona, that's a hook to play with."). The default prompt template uses `{show_block}`; custom templates can use either.

Legacy `dj_roster: list[DJPersona]` from before #144 is gone from the schema but a `mode="before"` validator in `StationConfig` still expands any `dj_roster` it finds in raw JSON into `djs` + `shows` on load. Old configs load cleanly without manual edits; the field is invisible to anything written after the migration.

### Schedule editor UI

A weekly grid (7 columns × 24 rows) lives in a left-sidebar takeover (click "📅 The Schedule"). Show shifts render as coloured blocks — palette of 8 hues, keyed by **DJ index in `djs[]`** (so the same DJ in two different Shows shares a colour). Default-DJ Shows (`dj_id=None`) get a dashed-outline / muted treatment. Current hour highlighted with a pulsing white outline; wrap-around shifts render as TWO blocks across midnight.

Block labels read **"`<show name>` with `<DJ name>`"** (when the Show has a name) or just the DJ name (when not). The text wraps to multiple lines on narrow cells instead of truncating. 1-hour blocks fall back to a single-letter monogram (show name initial if set, else DJ name).

Block interactions:
- **click** → opens the **Show editor drawer** (form: optional Show name, DJ picker dropdown with "(Default DJ)" + alphabetical DJs + "+ Create new DJ…", shifts list).
- **drag top/bottom edge** → snap-to-hour resize (skipped on wrap-around blocks).
- **drag body** → snap-to-cell move (4 px click-vs-drag threshold; duration preserved; clamped to mon-sun, 0-23).

`_suppressNextClick` mutex stops a successful drag from also opening the editor.

DJ identities live in a **separate sidebar takeover** — 🎙 DJ Roster (click the sidebar button). List rows show name + personality preview + voice + "used in N show(s)" counter + an "⚠ not in any show" badge when N=0. Clicking a row opens the **DJ editor**: name, personality, voice picker with ▶ Preview, voice instructions, and (for existing DJs) a "Used in N show(s)" footer listing the shift ranges. Deleting a DJ reassigns hosted Shows to the Default DJ slot rather than dropping them — the confirm prompt lists the affected shows.

Adding a DJ from inside the Show editor goes through an **inline modal** (`+ Create new DJ…` option in the DJ picker) — minimal name/personality/voice/instructions form that creates the DJ, closes the modal, and pre-selects it in the picker. Full identity editing happens in the DJ Roster takeover.

### DJ avatars
Stylised vector portraits per DJ, generated via OpenAI `gpt-image-1` low (~$0.011/call). Two-step pipeline: text model (gpt-4o-mini at `temperature=0.5`) rephrases the DJ's personality into a **SFW visual brief**, then the image model renders the brief. The text pipeline exists because image moderation is much stricter than text moderation — personality strings written for voice ("flirty", "smoky", "teasing") reliably trip 400s when sent raw to the image model. The rephrase is invisible to the user; if it fails, the whole pipeline aborts rather than falling back to the raw personality (which would just trip the same 400).

Manual trigger only via "Regenerate avatar" button in the DJ editor (`POST /djs/{dj_id}/avatar`). Files live at `generated/dj_icons/{uuid}.png`, served by `/media/dj-icon/{id}` with a **fallback to `app/seed/dj_icons/`**. The seed dir ships avatar PNGs matching the UUIDs in `example-radio_config.json`, so fresh clones get an illustrated roster out of the box without anyone having to regenerate. Seed PNGs are downscaled to 256×256 (UI displays at max 96 px) — that's the 95% size reduction noted in PR #159's diff.

Avatars surface in three places: the DJ Roster row (`.dj-avatar-md`, 60 px), the DJ editor header (`.dj-avatar-lg`, 96 px), and the on-air badge (`.dj-avatar-xs`, 22 px, inline in the "🎙️ On air with …" text). All use the same `.dj-avatar` base — a coloured circle (matching the DJ's grid palette colour) with the `<img>` layered on top via `onerror="this.remove()"` so a 404 leaves the coloured placeholder rather than a broken image icon.

Per-DJ cache-bust: `_djAvatarTs` Map updates per DJ on regenerate so refetching one avatar doesn't bust the others. Browser cache key is `?v=<server-generated-at>`.

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

## Backend module layout

`main.py` holds the FastAPI app, routes, and segment-attachment helpers. The subsystems that used to live there are their own modules:

- `prefetch.py` — the DJ-clip prefetch cache + background worker (`prefetch_dj_clip`, `take_prefetched`, `clear`). main.py spawns the worker thread and consumes/invalidates the cache; the cache state and lock live here.
- `news_cache.py` — the news bulletin clip cache (`get_news_clip`, `build_news_clip`, `wait_for_fresh_news`, `invalidate`) with its TTL state machine and refresh thread. The upstream RSS *headline* cache is separate, in `news.py`.
- `migrations.py` — inline DB migrations (below).
- `logging_setup.py` — `ContextFormatter`, `configure_logging`, `log_event`. Exists so the cache modules can log without importing main (circular).

Tests mirror the layout: `test_prefetch.py`, `test_news_cache.py`, `test_migrations.py` alongside `test_main.py`. When monkeypatching a dependency of moved code, patch it in the module that *calls* it (e.g. `app.prefetch.load_config`, not `app.main.load_config`).

## DB migrations

Done in `migrations.py` using raw SQL via `engine.begin()`, run once at main.py module load through `migrations.run_all()`. No Alembic. Two functions today, each idempotent:

- `migrate_drop_legacy_schema()` — drops legacy multi-station tables (`stations`, `favorite_stations`, `recent_stations`), drops obsolete `player_state` columns from the old multi-station era, adds `dj_clips.is_ad` if missing.
- `migrate_dj_clip_paths_after_generated_rename()` — rewrites `dj_clips.audio_path` from `generated_audio/…` → `generated/…` for installs that pre-date the #159 rename. Without this, every clip cached before the rename 404s at serve time because `_safe_media_path` resolves against a directory that no longer exists.

Add new migrations as sibling functions called from `run_all()`. Each should filter to only the rows that need touching so re-running on a fresh DB is a cheap no-op.

## Logging

`ContextFormatter` in `main.py` appends `key=value` extras from `logger.info(..., extra={...})` calls. Always pass structured context via `extra=` rather than f-strings where it makes sense. Timing is captured with `time.perf_counter()` and logged as `elapsed_s`.

**Levels**: lifecycle events (play start/stop, library scans, news cache refreshes, weather lookups) stay at INFO. **Per-event noise goes to DEBUG**: every `/player/next`, every queue mutation, every prefetch, every TTS call, every clip cache hit. The middleware request.start/end pair is also DEBUG (10 s status polls would spam INFO otherwise). The `_log_event(event, level=logging.DEBUG, **fields)` helper takes a level kwarg; module-level `logger.debug` / `logger.info` for direct calls. Default log level is INFO via `LOG_LEVEL` env var.

## What not to touch

- `radio_config.json` — Duncan's personal config, gitignored. Propose changes, don't overwrite silently. New clones bootstrap from `example-radio_config.json` (committed) into this file on first load.
- `generated/` — runtime cache of TTS MP3s + image-gen PNGs (renamed from `generated_audio/` in PR #159 after avatars joined the party). Not committed. Subdirs: `transitions/`, `ads/`, `news/`, `station_ids/`, `previews/`, `dj_icons/`.
- `app/seed/dj_icons/` — bundled avatar PNGs that ship with the example roster. The `/media/dj-icon/{id}` route serves from `generated/dj_icons/` first and falls back to here, so fresh clones see the shipped DJs illustrated without anyone having to regenerate. Filenames are the DJ UUIDs from `example-radio_config.json`.
- `radio.db` — SQLite data file, not committed.

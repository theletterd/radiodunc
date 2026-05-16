# RadioDunc — Handoff & Plan

Status doc for picking the project up mid-stream. The first half describes what just happened and what needs to be committed. The second half is the architectural plan for finishing the "Next-button-triggered DJ crossfade" feature.

---

## 1. Where we are right now

The repo had three problems we set out to fix:

1. A pile of dead code (unused endpoints, models, schemas, config fields, UI elements).
2. A wildly over-engineered HLS broadcast pipeline (`app/broadcast.py`) being used for what is fundamentally a single-listener local audio player.
3. No actual fade-from-track-to-DJ on the "Next" button — the broadcast pipeline does a server-side ffmpeg-rendered transition that doesn't respond to user input in real time.

**This commit batch tackles #1 only.** #2 and #3 are still ahead — that's the plan in §3.

### What was deleted in the working tree (not yet committed)

| File | Removed |
|---|---|
| `app/main.py` | duplicate `_daypart_greeting` / `_local_time_announcement` helpers, `_active_listener_count`, `_enqueue_admin_command`, `/listeners/heartbeat`, `/player/admin/command`, `/player/stream` passthrough, `admin_commands_json` column from the migration shim, related imports |
| `app/models.py` | `ListenerSession` table, `admin_commands_json` column on `PlayerState` |
| `app/schemas.py` | `PlayerAdminCommandRequest`, `ListenerHeartbeatRequest`, `ListenerHeartbeatResponse`, unused `Any` import |
| `app/config.py` | `time_announcement_every_breaks`, `dj_break_every_tracks`, `weather_insert_every_breaks`, `news_insert_every_breaks` |
| `app/playout_worker.py` | `_consume_admin_commands`, `_admin_commands`, the `_tick` call that invoked them |
| `app/ui/index.html` | orphan `<audio id="overlayAudio">` |
| `tests/test_main.py` | `test_listener_heartbeat_tracks_last_seen_by_session_id`, stale imports, the now-nonexistent `time_announcement_every_breaks` kwarg |
| `.gitignore` | added `*~`, `radio.db`, `generated_audio/` |

Diff size: **+12 / −175** across 8 files.

### Verified

- Every touched file compiles (`python -m py_compile`).
- Repo-wide grep for each deleted symbol returns zero matches.
- Test suite was *not* runnable from the agent sandbox (Linux 3.10 can't load the venv's macOS-built `pydantic_core` C extension). **You need to run `pytest -q` locally before committing.**

---

## 2. Finishing the dead-code commit (do this first)

A few stray files the agent couldn't delete from its sandbox — clean them up by hand:

```bash
cd /Users/duncan/programming/radiodunc

# stray editor backups + leftover lock + my accidental probe file
rm -f .env~ radio_config.json~ foo.txt .git/index.lock

# verify
source .venv/bin/activate
pytest -q

# commit
git add -A
git status   # sanity check before commit
git commit -m "Trim dead code

- remove unused listener heartbeat endpoint, model, and schemas
- remove unused admin command endpoint, schema, worker handler,
  and PlayerState column
- remove dead /player/stream passthrough and duplicate daypart
  helpers from main.py
- drop unused cadence config fields
  (dj_break_every_tracks, weather/news/time_announcement_every_breaks)
- drop orphan overlayAudio element from index.html
- gitignore editor backups and runtime artifacts (radio.db, generated_audio/)"
```

---

## 3. The architectural plan — kill HLS, mix in the browser

### Why we're abandoning the current broadcast pipeline

`app/broadcast.py` runs a two-slot ffmpeg HLS encoder with a warm handoff. It looks impressive but it's the wrong tool for a single-listener local app, and it has concrete bugs:

1. **The first-track infinite loop bug.** `/player/play` in `main.py` starts the encoder on the *first* track with `-stream_loop -1`, so by default the listener hears track #1 looping forever. The actual queue progression only happens if `PlayoutWorker._orchestrate_transition_window` fires successfully 35 seconds before `planned_end_epoch` — and if anything fails (DJ clip generation, path validation, the 6-second HLS readiness wait), you're stuck on track #1.

2. **Every transition kills ffmpeg and restarts it.** `_replace_active_stream` terminates the old process; `_wait_until_hls_ready` blocks up to 6 seconds waiting for the new one to produce a manifest and first segment. The two-slot scheme exists to mitigate the 404s the player gets while the old segments are being deleted by `_clear_slot`.

3. **HLS is inherently latent.** `-hls_time 2 -hls_list_size 4` means ≈8s of forced buffering. A "Next" click can't take effect until the player drains buffered segments it already downloaded. The 5-second manifest TTL and 10-second segment cache add even more drag.

4. **The transition filter graph is a one-shot tape splice.** `[0:a]volume='if(...)';adelay;amix;concat` renders a single fixed trajectory. Once fired, you cannot: start the fade earlier, change the next track, alter duck level, or pause without killing the whole pipeline. It's a render, not a live mixer.

5. **Two timelines that drift.** The worker tracks `planned_end_epoch` based on Mutagen's `duration_seconds`. The ffmpeg encoder has no idea — it just emits segments paced by `-re`. The listener's playback position drifts from the worker's notion of "now" the longer the session runs.

6. **Epoch-based segment numbering + discontinuity flags.** `-hls_start_number_source epoch` plus deleted segments produces intermittent 404s and crankiness, especially in Safari's AVPlayer.

For a single-user local app, the correct architecture is: backend serves discrete files, browser mixes with Web Audio API. Sample-accurate, zero ffmpeg, zero HLS, instant Next button.

(If you ever want multi-listener server-side mixing, the right tool is **Liquidsoap + Icecast** — not a custom HLS pipeline. Liquidsoap has first-class jingles, fades, and request queues. But that's a different project.)

### Target architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          Browser                                │
│                                                                 │
│   AudioContext                                                  │
│     ├── currentTrack <audio> ─► MediaElementSource ─► gainA ─┐  │
│     ├── djClip      <audio> ─► MediaElementSource ─► gainDJ ─┼──► destination
│     └── nextTrack   <audio> ─► MediaElementSource ─► gainB ──┘  │
│                                                                 │
│   Scheduler: on Next press, schedule gain ramps on              │
│   ctx.currentTime timeline for sample-accurate crossfade        │
└────────────────────────────┬────────────────────────────────────┘
                             │ plain HTTP range requests
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                       FastAPI backend                           │
│                                                                 │
│  GET /player/state              → current queue snapshot        │
│  POST /player/play              → build queue, set state        │
│  POST /player/next              → return                        │
│       { current_track_url,                                      │
│         dj_clip_url,            ← synthesized on demand,        │
│         next_track_url,           cached by script hash         │
│         next_track_metadata }                                   │
│  GET  /media/track/{id}         → range-served mp3              │
│  GET  /media/dj-clip/{hash}     → range-served wav              │
└─────────────────────────────────────────────────────────────────┘
```

Backend's job shrinks to: manage queue state, generate scripts/clips on demand, serve files. No ffmpeg, no HLS, no encoder, no segments.

### The Next-button flow

When user presses Next:

1. Frontend (synchronously, same event tick):
   - Schedules `gainA.gain.linearRampToValueAtTime(0, now + 2.0)` — fade current track to silence over 2 s.
2. Frontend calls `POST /player/next`. Backend:
   - Advances `queue_index`.
   - Resolves next track from the queue.
   - Generates DJ script (transition style: "back-announce previous, intro next").
   - Synthesizes DJ clip via cached TTS provider (returns immediately if hash cached).
   - Returns `{ dj_clip_url, next_track_url, next_track_metadata }`.
3. Frontend, on response:
   - Sets `djClipAudio.src = dj_clip_url`, calls `.load()`. Browser starts fetching.
   - At `ctx.currentTime + 1.5` (overlapping the fade tail by 500 ms): start DJ clip at `gainDJ = 1.0`.
   - Sets `nextTrackAudio.src = next_track_url`, calls `.load()` (prefetch during DJ).
   - On DJ clip `ended` event (or scheduled at `djAudio.duration - 0.3`): start next track and ramp `gainB` 0 → 1 over 1 s.
4. Same pattern fires automatically at `currentTrack.duration - 8` for natural end-of-track DJ breaks.

All gain ramps are scheduled on the `AudioContext` timeline so they're sample-accurate. The Next button feels instant.

### Concrete file-by-file changes

**Delete:**
- `app/broadcast.py` (entire file)
- All `/broadcast/*` endpoints in `app/main.py`
- `BroadcastStatusResponse` from `app/schemas.py`
- `broadcast_engine` global, `_safe_media_path` reference into broadcast, `_last_manifest_stale_log_at_epoch`
- HLS resilience code in `app/ui/app.js`: `ensureLiveStreamLoaded`, `destroyHlsController`, `canAttemptStreamRecovery`, `STREAM_RECOVERY_WINDOW_MS`, `streamRecoveryAttemptTimes`, the `ended`-event recovery handler, `lastStreamReloadAtMs`, `endedRecoveryInFlight`, `STREAM_RELOAD_COOLDOWN_MS`, `STREAM_RECOVERY_WINDOW_MS`, `MAX_STREAM_RECOVERIES_PER_WINDOW`
- `<script src="…hls.js…">` from `app/ui/index.html`
- `playout_worker._orchestrate_transition_window`, `_prepare_transition_assets`, `_commit_transition_or_fallback` (the whole transition-rendering pathway). The worker becomes a simpler timing/cleanup loop, or potentially deletes entirely if all state lives client-side.
- `cleanup_ephemeral_clips` and the `persist=False` branch in `tts.get_or_create_dj_clip` — always persist clips. Cached by hash, cheap.

**Add:**
- `app/main.py`:
  - `POST /player/next` (replacing the existing skeleton) returns the JSON triple `{ current_track_url, dj_clip_url, next_track_url, next_track_metadata }`. Synthesizes the DJ clip inline. Returns within ~1–3 s depending on TTS provider.
  - `GET /media/track/{track_id}` — replace the implicit usage via `/player/current-media` with an explicit by-id route. (Or keep `/player/current-media`; either way the URL just needs to be stable and range-capable.)
  - `GET /media/dj-clip/{clip_hash}` — serve cached DJ clips by hash. The existing `DJClip.script_hash` column already supports this.
  - New schemas: `PlayerNextResponse { current_track_url, dj_clip_url, next_track_url, next_track_metadata }`.

- `app/ui/app.js` — rewrite the audio layer:
  - Construct `AudioContext` lazily on first user gesture (autoplay policy).
  - Create three `<audio>` elements (currentTrack, djClip, nextTrack), each wrapped in `MediaElementAudioSourceNode` + `GainNode`, all summed into `ctx.destination`.
  - `startPlayback()` calls `/player/play`, sets `currentTrack.src = response.current_track_url`, ramps `gainA` 0→1 over 0.5 s, calls `play()`.
  - `pressNext()` schedules `gainA` fade-out, fires `/player/next`, on response schedules DJ start + nextTrack handoff as described above.
  - Volume slider drives a master `GainNode` between the sum point and `destination`.

**Keep mostly as-is:**
- `app/dj_scripts.py`, `app/tts.py` — DJ script generation and OpenAI TTS already work. Just always persist clips.
- `app/scheduler.py` — queue builder is fine.
- `app/stations.py`, `app/scanner.py`, `app/config.py` — unchanged.

### Suggested commit cadence

1. **Commit A — wire up `/player/next` JSON endpoint and direct media routes.** Backend-only change. Tests for the new response shape and clip caching by hash. UI still on HLS, broken but ignorable for one commit.
2. **Commit B — rewrite UI audio layer using Web Audio API.** Drop hls.js, drop HLS recovery code, add `AudioContext` + three audio elements + gain scheduling. Wire the Next button to the new endpoint. Stream URL no longer touched.
3. **Commit C — delete `app/broadcast.py`, all `/broadcast/*` endpoints, `BroadcastStatusResponse`, transition pathway in `playout_worker.py`, `cleanup_ephemeral_clips`, the persist=False branch.** Should be a giant negative diff.
4. **Commit D — polish.** End-of-track auto-DJ trigger, master gain volume, audible "fade out → DJ → fade in" tuning of timings, sensible Stop/Pause behavior.

### Open questions to decide before starting

- **Crossfade timings:** default values to use? I suggested 2.0 s fade-out, 500 ms DJ overlap into the tail, 1.0 s fade-in. Tunable via config?
- **DJ clip generation latency:** OpenAI TTS for `gpt-4o-mini-tts` is roughly 1–3 s. Should the backend pre-generate the *next* DJ clip immediately after the current one starts (i.e., one-ahead prefetch), so a Next press has zero TTS wait? This is straightforward to add — playout_worker becomes a "background DJ clip preparer" instead of a transition committer.
- **Track end behavior:** natural end-of-track should auto-fire the same flow with no user input. Trigger at `currentTrack.duration - 8 s` via `timeupdate` listener.
- **Playout worker fate:** with client-side mixing, much of `playout_worker.py` becomes unnecessary. It could either disappear entirely (queue state managed only when `/player/next` is called) or shrink to "prefetch next DJ clip in background." Lean toward deleting it unless we want the prefetch — simpler is better.
- **Stop button:** with no encoder running, `Stop` just means "pause all audio elements, set `is_playing=false`." Easy.

### Things NOT to do

- Don't try to keep HLS "for compatibility." There's only one listener (you).
- Don't try to do server-side mixing with ffmpeg pipes. Same trap.
- Don't add hls.js back. The whole point is that the browser plays plain audio files.
- Don't render DJ clips as MP3 if WAV is faster — Web Audio doesn't care, and you'll save TTS round-trip processing.

---

## 4. Pre-existing context worth knowing

- Stations are generated from a config-driven persona list (`station_presets` in `radio_config.json`).
- TTS provider is currently OpenAI (`gpt-4o-mini-tts`, voice `alloy`) per `radio_config.json`. Falls back to a sine-tone WAV provider if the key is missing.
- Script provider is OpenAI text (`gpt-4o-mini`).
- `OPENAI_API_KEY` is loaded from `.env` (already gitignored).
- DJClip cache key is `sha256(voice + "\n" + script_text.strip())` — same script + voice always returns the cached file.
- Library is scanned via `POST /library/scan` and stored in `radio.db` (SQLite). Currently 1000-track scan limit.
- Music folder per local config: `~/Desktop/radiostation`.
- The frontend is vanilla JS — no framework, no build step. Edit `app/ui/app.js` directly.

---

## 5. Quick-reference: file inventory after the rewrite

```
app/
  main.py             # endpoints: scan, stations, queue, player state, /player/next (rewritten), media routes
  config.py           # AppConfig + load/save
  database.py         # SQLAlchemy engine
  models.py           # Track, Station, DJClip, PlayerState, FavoriteStation, RecentStation
  schemas.py          # Pydantic request/response models
  scanner.py          # mutagen-based metadata extraction
  stations.py         # station generation from presets
  scheduler.py        # queue building
  dj_scripts.py       # OpenAI text generation
  tts.py              # OpenAI + tone TTS providers, persistent clip cache
  weather.py          # open-meteo lookup (used by dj_scripts when include_weather=True)
  prompt_library.py   # prompt fragments
  ui/
    index.html
    app.js            # Web Audio API mixer (rewritten)
    styles.css

# DELETED:
#   app/broadcast.py
#   app/playout_worker.py  (or heavily simplified)
```

Good luck. Ping if anything in this plan looks wrong.

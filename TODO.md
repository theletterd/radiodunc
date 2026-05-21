# RadioDunc — Backlog

## ✅ Move more settings into the UI

Shipped: "⚙ Station Settings" sidebar takeover that parallels the
schedule editor. Six collapsible sections (Station identity, Weather,
News, Ads, Station IDs, AI). Save merges back onto the live config so
fields the panel doesn't expose (dj_roster, voice pools) round-trip
untouched.

Deliberately still json-only:
- `alerts.news.voices` / `alerts.ads.voices` — arrays of voice+
  instructions entries; need their own mini-editor (probably next
  iteration).
- `*.prompt_template` overrides — power-user knobs, easy to misuse.
- `local_time_zone`, `tts_provider`, `openai_*_model`,
  `playlist_artist_repeat_window`, `core_artists`, `music_folder` —
  infrequent enough to leave in json.

## ☐ DJ avatars — extra surfaces

Slice 1 shipped: manual "Regenerate avatar" button in the DJ editor,
generated via `gpt-image-1` low quality (~$0.011/click), served from
`generated_audio/dj_icons/{dj_id}.png`. Avatars show in the DJ Roster
list rows + the editor itself; coloured-circle fallback when no avatar
has been generated yet.

Shipped after slice 1:
- **On-air badge avatar** — small 22 px circle in front of the "🎙️ On
  air with [DJ]" text in the player. Server exposes `active_dj_id` on
  `StationOut` so the client can build the avatar URL (active_station's
  model_copy drops the id along the way; surfaced separately).
- **Roster row avatar bumped to 60 px** — and the row layout flipped
  from vertical-stack to flex-row with the text content stacked to the
  right of the avatar, so the bigger image doesn't tower over the
  three-line text column.

Still to do, whenever:
- **Schedule grid blocks** — small avatar circle in the corner of each
  block. Layout work to fit it cleanly alongside the
  "<show> with <DJ>" label.
- **Auto-regenerate trigger** — currently button-only. Could opt in to
  auto-regen on personality changes ($0.011/save) if button-only turns
  out to be friction.

## ☐ Move per-segment audio gain into config

Currently hardcoded as JS constants in `app/ui/app.js` (lines 13–16):
`DJ_GAIN = 2.1`, `NEWS_GAIN = 1.7`, `AD_GAIN = 1.6`, `STINGER_GAIN = 2.0`.
Music has no segment-level boost (effectively 1.0). The compressor flattens
dynamic range afterwards, so audible differences are smaller than raw
ratios suggest.

Worth promoting to an `audio_levels` block under `AppConfig` (probably
`audio_levels: {dj, news, ads, stingers}`) once we've settled on values
we like — currently tweaking means a code edit + reload, which makes
the constants feel more permanent than they are. Hot-reload via the
existing `/config` change hook would also pick this up cleanly.

## ✅ Hot-reload config on PUT /config

Shipped. `update_config` loads the old config before save, then calls
`_on_config_changed(old, new)` (in `app/main.py`) which selectively flushes
the two in-memory caches that bake config values into their contents:

- **DJ-clip prefetch cache** (`_prefetch_cache`) — cleared on any change to
  `station`, `alerts`, or the text/TTS generation knobs
  (`tts_provider`, `script_provider`, `openai_text_model`/`_temperature`,
  `openai_tts_model`/`_voice`). The prefetched audio was synthesised
  against the old persona/cadence/voice.
- **News bulletin cache** (`_news_cache`) — cleared when `station.name`,
  `station.spoken_name`, the entire `alerts.news` subtree, or any
  generation knob changes. Tweaking `alerts.ads.risque_chance` does NOT
  drop the news (verified by test).

Intentionally left to expire naturally:
- **Weather summary cache** — 30-min TTL handles it. The cost of one
  extra HTTP fetch on save is small enough that an explicit invalidator
  isn't worth the code.
- **Station-ID stinger phrases** (disk file) — file is keyed by station
  name and regenerated lazily when needed. An explicit eviction shaves
  one LLM round-trip on the next stinger pull; not worth the surface.

Hook failures are logged and swallowed so a botched cache flush never
500s the PUT — the new config is already on disk by then. Tests live in
`tests/test_main.py` (the `test_config_change_*` block).

## ✅ Front-end audio compression
Shipped in PR #134. `DynamicsCompressorNode` sits between `masterGain` and
`ctx.destination` in `initAudio()` with broadcast defaults (threshold -18
dB, ratio 4:1, attack 5 ms, release 100 ms). Auto-flattens the loud/quiet
gap between voices and between voices vs music. The per-voice trim plumbing
was subsequently ripped out (PR #135) since the compressor handles it.

## ✅ Separate DJ from show

Shipped across 7 PRs (#144 design doc, #145–#150 vertical slices, #151
follow-up polish on the block label). End state matches the design doc:

- `StationConfig` has `djs[]` (reusable identities) + `shows[]`
  (bindings that link a DJ to a set of shifts). The legacy `dj_roster`
  field is gone from the schema; a `mode="before"` validator still
  expands any legacy `dj_roster` it finds in raw JSON so old configs
  load cleanly without manual edits.
- Resolver (`pick_active_persona`) walks `shows[]` only; legacy fallback
  removed.
- `{show_name}` placeholder available in the DJ prompt template, plus a
  pre-formatted `{show_block}` hint sentence that prompts the LLM to
  riff on persona/show contrast (Raven Vale's "Velvet Hours" on Tuesday
  afternoons is the sleeper feature).
- Schedule grid renders Shows; DJ-stable colours; "<show> with <DJ>"
  block label that wraps cleanly on narrow cells.
- Show editor handles scheduling; DJ Roster takeover handles identity;
  "+ Create new DJ…" inline modal keeps the Show flow uninterrupted;
  Delete DJ reassigns Shows to Default rather than dropping them.

## ✅ Schedule grid unclickable after persona edit → back

Not actually a persona-edit bug — it was the 60-second auto-refresh
(`_scheduleAutoRefresh`) calling `renderSchedule()` without re-running
`_attachBlockClickHandlers()`. Every other caller did the pair correctly;
the interval timer was the one forgotten path. Symptom showed up most
often after persona editing because that's when the user lingers in
scheduler mode long enough for the timer to fire.

Fix: folded `_attachBlockClickHandlers()` into `renderSchedule` itself
so it can't be forgotten again. Removed the now-redundant explicit calls
from the six other call sites. Regression test in
`tests/js/schedule.test.js` re-renders in place and asserts that a click
on the new block still opens the editor.

## ☐ Chunked scan commits

`scan_library` in `app/scanner.py` currently `db.add(track)`s every new file
and does a single `db.commit()` at the end. On large libraries (10k+
files) this means:

- The whole new-track set sits in the session's identity map until commit
  → memory grows with library size.
- One huge transaction → if the scan crashes or the user kills it
  partway through, NOTHING was written. Forces a fresh start instead of
  resuming where it left off.
- The user sees nothing in the UI's "track count" until the entire scan
  finishes.

Fix: flush + commit every N tracks (probably N=200 or so — small enough
to bound memory, large enough that commit overhead doesn't dominate).
The `existing` duplicate check stays correct because committed rows are
still visible to subsequent queries in the same session.

Stretch: a `/library/scan/progress` endpoint or websocket so the UI can
show a live counter ("imported 2,340 of ~8,000…") instead of staring
at "Scanning…" for a minute.

## ☐ Spurious unprompted playback after lid-close / wake

Repro: hit Stop, close laptop lid, walk away. On lid-open, the player started
playing on its own without any user gesture.

**Status**: instrumentation shipped in PR #119 (Nov 2026). Every playback
entry point now logs through `_logPlayback(event, fields)` with full state
context, and `triggerTransition` defensively bails with a `console.warn` if
`serverState?.is_playing` is false. So:

- Symptom is suppressed: the user no longer hears unexpected playback when
  this bug fires (the guard catches it before any audio plays).
- Diagnosis is automatic: next time the bug repros, the browser console
  shows exactly which entry point fired and why — look for the
  `triggerTransition blocked — serverState says not playing` warn and the
  preceding entry log.

Waiting on a fresh repro to chase the root cause. Likely suspect: a stale
`autoTrigger` setTimeout that survived `stopPlayback` (macOS power
management may suspend/resume the JS event loop without firing
`clearTimeout` cleanly). If confirmed, fix is to give timers a
generation token, or have the autoTrigger callback re-check
`serverState.is_playing` itself.

## ✅ Always-async news generation
Shipped in PR #118. `get_news_clip` never blocks on regeneration anymore;
cache miss = skip the segment + queue a refresh. The warmup on `player_play`
keeps the cache warm in normal use, so the skip path almost never fires.

**Follow-up (May 2026):** with sparse news cadence (every_n_breaks ≥ 10),
the skip path WAS firing in practice — when the 30-min cache expires between
cadence hits, news disappears for an entire extra cycle. `_attach_news` now
waits up to `NEWS_BLOCK_ON_MISS_S` (8 s) on the just-spawned refresh before
falling back to skip. `get_news_clip`'s contract is unchanged.

## ✅ Persona definition refactor — split personality from voice
Shipped in PR #120. `dj_style` → `personality` on both `StationConfig`
and `DJPersona`, with silent migration validators that accept the old
keys. UI form label now reads "Personality — what they SAY". Voice
direction lives in `voice` + `voice_instructions` as before.

## ☐ Future ideas (not committed yet)

- **Cost guardrail** — track $/day OpenAI usage in the DB, surface in
  the UI, optional soft cap with a warning toast. Lets you experiment
  with bigger phrase pools / longer prompts without anxiety.
- **Like / dislike signal** — heart/x buttons in the player that bias
  the scheduler. Connects what you actually enjoy to what plays.
- **Stinger pool variety on warmup** — currently the startup warmup
  seeds ONE stinger clip; could top up the pool gradually so the first
  few skip-stingers have variety from minute one.
- **Multi-listener / shareable URL** — would need a real broadcast
  layer (icecast, HLS, or just polling-based sync). Big architectural
  shift; only worth it if you want friends to tune in.

## DJ / personality system
- ✅ Consolidate DJ config: station fields are the default DJ, `dj_roster` entries are scheduled overrides
- ✅ Renamed `voice_hint` → `voice` everywhere (consistent with `voice_instructions`)
- ✅ Multiple ad-break voices/personalities — `AdVoice` list with per-voice instructions, random pick per break

## DJ script / prompts
- ✅ When an ad break is coming up, DJ is told to tease it (`ad_break_follows` flag → prompt block)
- ✅ Injected tracks flagged as audience requests (`requested: true` in queue item → `reason = "request"` in prompt)

## UI — library panel
- ✅ Hide the scanner behind a collapsible dropdown (it's rarely used)
- ✅ Add a "Library status" widget to the sidebar showing track count, last scan time, etc.

## UI — queue
- ✅ Queue shows all upcoming tracks (was capped at 5), scrollable with max-height
- ✅ Queue start size bumped to 30 tracks
- ✅ Drag-to-reorder queue items (HTML5 drag-and-drop, blue drop indicator, ⠿ handle)
- ✅ "Add more tracks" button extends the queue on demand

## UI — layout / bugs
- ✅ Search bar overflow fixed (box-sizing: border-box on inputs globally)
- ✅ Queue delete 204 response no longer throws a JS error (api() skips JSON parse on empty)
- ✅ Ad break badge timer is cancelled on new transition — no more stale early resets

## Ad breaks
- ✅ Cache ad clips forever; configurable `pool_size` (default 100), picks randomly from pool once full

## Observability
- ✅ Add timing logs for LLM and TTS steps (`elapsed_s` logged on all OpenAI calls)

## UI — now playing
- ✅ Show the MP3 filename in the now-playing display (monospace, below track title)

## Config
- ✅ Sync radio_config.json to match the new example config shape (voice → voices list for ads, voice_instructions on roster entries)

## Docs
- ✅ Update README to reflect current architecture (Web Audio, single-station config, persona system, etc.)

---

## Done

- ✅ End of queue handled gracefully (clean stop + message)
- ✅ Auto-trigger uses correct duration (loadedmetadata only)
- ✅ Dead `DJ_OVERLAP_S` constant removed
- ✅ Volume slider persists across reload (localStorage)
- ✅ Next button visual feedback (disabled + "Loading…")
- ✅ DJ / music volume balance (`DJ_GAIN = 1.8`)
- ✅ Upcoming queue display with veto buttons
- ✅ Track request search bar with queue inject
- ✅ Removed `ADMIN_API_TOKEN` auth (local app)
- ✅ Single curated station — collapsed multi-station model into config-driven `StationConfig`
- ✅ Adjustable base DJ prompt (`dj_prompt_template` with placeholder substitution)
- ✅ Per-day DJ personas (`dj_roster` with day/hour scheduling)
- ✅ DJ reacts to manual Next skips (`reason: "skip"` flag)
- ✅ Real weather reports on a cadence (`alerts.weather.every_n_breaks`)
- ✅ News headlines from RSS on a cadence (`alerts.news.rss_url`, `every_n_breaks`)
- ✅ LLM-generated ad breaks with a second voice (`alerts.ads`, default voice "echo")
- ✅ Auto-trigger reliability fix (handles metadata-already-loaded case)
- ✅ Filename fallback when artist/title metadata missing
- ✅ Transition "now playing" display (label updates the moment `/player/next` returns)
- ✅ Ad-break UI badge ("📻 Ad break" flashes during ad playback)
- ✅ Pre-generate next DJ clip in background (eliminates TTS latency on auto-advance)
- ✅ Local time added to DJ prompt (`current_time`, `current_weekday` placeholders)
- ✅ Per-DJ `voice_instructions` field passed to OpenAI TTS API
- ✅ Weather reports in Celsius
- ✅ Network drive reliability — track files read fully into memory before serving (retry on 503 client-side)

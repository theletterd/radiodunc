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

## ☐ DJ roster with AI-generated icons

Each DJ gets a small avatar generated from their personality+voice
description (DALL-E or similar). Shown:
- as a chip in the schedule grid's legend
- inside the persona block on the grid (small circle in the corner)
- in the persona editor next to the name field
- in the "On air with [DJ]" badge when that DJ is live

Generation flow: on save, if the persona's personality changed, queue
a background image-gen job. Cache the result keyed by personality hash
in generated_audio/dj_icons/{hash}.png (or a new generated_dj_icons/
subdir). Default to a coloured initial if no icon yet (matches the
palette colour we already assign per persona). Backend serves the
icon at /media/dj-icon/{persona_name_slug}.

Cost: ~$0.04 per DJ icon at DALL-E pricing; small one-time per DJ.

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

## ☐ Separate DJ from show

Right now a "persona" entry conflates two concepts: the DJ as a character
(name, personality, voice, voice_instructions, gain trim) AND the show they
host (which day/hour slots they own). You can't have one DJ host multiple
distinct shows without duplicating the DJ definition.

End state: a DJ is a reusable identity. A show binds a DJ to a slot:

  djs:
    - id: jessica_danger
      name: "Ms. Jessica Danger"
      personality: "sultry late-night intimacy"
      voice: sage
      voice_instructions: "..."
      voice_gain_offset_db: -3

  shows:
    - dj: jessica_danger
      shifts: [{day: friday, start_hour: 22, end_hour: 23}, ...]
    - dj: jessica_danger        # same DJ, different show
      shifts: [{day: monday, start_hour: 7, end_hour: 9}]
    - dj: jessica_danger
      shifts: [{day: saturday, start_hour: 10, end_hour: 12}]

UI implication: the schedule editor's persona drawer splits into two
tabs/panels — "DJ identity" (the character) and "Shows" (the slots that
reference this DJ). The grid renders shows, click → edit the DJ behind it,
"swap to different DJ" picker for power moves.

Migration: the current dj_roster shape is one-to-one (each persona owns
its own shifts). On load, expand each persona into one DJ + one show
record. Old configs keep working without manual edits.

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

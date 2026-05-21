# RadioDunc — Backlog

Active backlog only. Anything shipped lives in git history (search the PR
list); the short "recently shipped" list at the bottom is just a quick scan
of what's landed lately, not a permanent archive.

## ☐ DJ avatars — schedule grid block corners

Slice 1 shipped (manual "Regenerate avatar" button) along with the on-air
badge avatar + roster row treatment. The remaining surface is the schedule
grid: a small avatar circle in the corner of each Show block. Layout work
to fit it cleanly alongside the "<show> with <DJ>" label without crowding.

## ☐ Move per-segment audio gain into config

Currently hardcoded as JS constants in `app/ui/app.js` (lines 13–16):
`DJ_GAIN = 2.1`, `NEWS_GAIN = 1.7`, `AD_GAIN = 1.6`, `STINGER_GAIN = 2.0`.
Music has no segment-level boost (effectively 1.0); the compressor flattens
dynamic range afterwards, so audible differences are smaller than raw
ratios suggest.

Blocked on Duncan wanting to tweak values manually first before we settle
on what to commit. Once values feel right, promote to an `audio_levels`
block under `AppConfig` (probably `audio_levels: {dj, news, ads, stingers}`)
so future tweaks don't need a code edit + reload. Hot-reload via the
existing `/config` change hook would pick this up cleanly.

## Monitoring (not actionable until something changes)

### Spurious unprompted playback after lid-close / wake

Original repro: hit Stop, close laptop lid, walk away — on lid-open, the
player started playing on its own.

Instrumentation + a defensive guard shipped in PR #119: every playback
entry point logs through `_logPlayback(event, fields)` with full state
context, and `triggerTransition` bails with a `console.warn` if
`serverState?.is_playing` is false. So the symptom is suppressed (the
guard catches it before any audio plays) and the next repro will name
the offending entry point in the console.

No fix until a fresh repro. Likely suspect: a stale `autoTrigger`
setTimeout that survived `stopPlayback`. If confirmed, fix is to give
timers a generation token or have the autoTrigger callback re-check
`serverState.is_playing` itself.

## ☐ Future ideas (not committed yet)

- **Like / dislike signal** — heart/x buttons in the player that bias the
  scheduler. Connects what you actually enjoy to what plays.
- **Stinger pool variety on warmup** — currently the startup warmup seeds
  one stinger clip; could top up the pool gradually so the first few
  skip-stingers have variety from minute one. (~hour of work.)
- **Multi-listener / shareable URL** *(probably never)* — would need a
  real broadcast layer (icecast, HLS, or polling-based sync). Big
  architectural shift; only worth it if friends end up wanting to tune in.
- **Live-progress UI for library scans** — `/library/scan/progress`
  endpoint or websocket so the UI shows a live "imported 2,340 of ~8,000…"
  counter instead of staring at "Scanning…" for a minute. The chunked
  commits in PR #156 already provide the durability foundation (rows are
  committed in batches as the scan runs); this is the UI half.

## Recently shipped

A quick orientation for what's landed in the last sweep of work. Look at
the PR description for the full design notes; AGENTS.md describes the
current state of each system.

- **#163** — startup migration rewrites `dj_clips.audio_path` from
  legacy `generated_audio/…` → `generated/…` so cached stingers, ads,
  news, and transitions all play after the dir rename. Required for any
  install that pre-dates #159.
- **#162** — `{self_id_block}` placeholder in the default DJ prompt;
  ~1-in-3 transitions inject a "weave in your name and show name"
  directive so the DJ does the classic "you're listening to X, with
  yours truly Y" patter occasionally without grating.
- **#161** — "Add to playlist" button on search results, appending to
  the queue tail rather than the next slot. `QueueInjectRequest.position`
  is now `"next"` | `"end"`.
- **#160** — DJ-clip prefetch worker reads `requested` flag on the
  target queue item and uses `reason="request"` accordingly. Fixes the
  bug where caller-requested tracks didn't get acknowledged.
- **#159** — `generated_audio/` → `generated/` rename (avatars share
  the dir, so the audio-specific name was misleading). Seed roster:
  `example-radio_config.json` ships the real DJ set + matching avatar
  PNGs in `app/seed/dj_icons/`. `/media/dj-icon/{id}` falls back to the
  seed dir, so fresh clones get a fully-illustrated roster out of the
  box. (Caveat: this PR introduced the stale-paths bug fixed by #163.)
- **#158** — on-air badge avatar background matches the DJ's palette
  colour (per-DJ client-side cache), not hardcoded pink.
- **#157** — on-air badge avatar (inline 22 px circle in the "🎙️ On air
  with …" text) + DJ Roster row avatars bumped from 28 → 60 px with a
  flex-row layout so the bigger image doesn't tower over the text.
  Server exposes `active_dj_id` on `StationOut` so the client can build
  the avatar URL.
- **#156** — chunked scan commits: `scan_library` flushes + commits
  every `SCAN_COMMIT_CHUNK_SIZE = 200` tracks, then `expire_all`s the
  session, so memory stays bounded and a crash mid-scan keeps the
  already-committed chunks in the DB.
- **#153** — DJ avatars slice 1: manual "Regenerate avatar" button in
  the DJ editor; two-step pipeline (gpt-4o-mini text rephrase →
  gpt-image-1 low) to dodge image-moderation false positives on
  personality strings.
- **#152** — wall-clock timestamps on `[playback]` console logs so the
  lid-wake repro logs are easy to tell apart from earlier in the session.
- **#144–#151** — the DJ-vs-Show refactor across 7 PRs (design doc +
  6 vertical slices + a block-label polish). See AGENTS.md for the
  current state of `djs[]` + `shows[]` + The Ghost.

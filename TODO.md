# RadioDunc — Backlog

## ☐ Persona definition refactor — split personality from voice

Today a persona is `dj_style` (free-text) + `voice` + `voice_instructions`.
The free-text style field mixes two distinct concerns: **what they say**
(personality, slang, attitude, what they'd talk about) and **how they sound**
(pacing, accent, delivery characteristics). These should be separate fields.

The base prompt template (DJ role, ad-break teases, time mentions, weather/
news handoffs, station-name pronunciation rules) should remain unchangeable
scaffolding — personas slot personality + voice into clearly-named placeholders
inside it, rather than overriding the whole template.

End state lets us swap in distinct vibes cleanly:
  - "Flirty late-night naughty DJ" (personality) + (breathy, low, intimate voice)
  - "Perky kids morning DJ" (personality) + (bright, energetic voice)

Implementation when picked up:
- Rename `dj_style` → `personality` on StationConfig + DJPersona, with a
  migration validator that keeps existing configs working
- Document the base prompt template as the locked-in scaffolding
- UI Phase 3 should expose personality and voice_description as separate fields

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

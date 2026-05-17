# RadioDunc — Backlog

## DJ / personality system
- [ ] Consolidate DJ config: one "default DJ" on the station, others as scheduleable roster entries — default plays when nothing else matches
- [ ] Rename/clarify `voice_hint` → `voice` and `voice_instructions` → `voice_instructions` (consistent naming, no ambiguity)
- [ ] Multiple ad-break voices/personalities — pool of defined ad voices, pick one per break

## DJ script / prompts
- [ ] When an ad break is coming up, tell the DJ to tease it: "coming up after the break, <next song>"
- [ ] When a track was injected via the request queue, tell the DJ it was an audience request: "someone called in requesting this one"

## UI — queue
- [ ] Make the queue display longer (show more upcoming tracks)
- [ ] Drag-to-reorder queue items
- [ ] "Add more" button to extend the queue

## UI — layout / bugs
- [ ] "Request a track" search bar overflows the container on the right — needs a max-width / overflow fix
- [ ] Hitting 'x' on the up-next list removes the item but shows an error in the UI — handle the 204/empty response gracefully
- [ ] Ad break UI badge disappears too early — extend the hide timer to match actual ad audio duration

## Ad breaks
- [ ] Cache ad clips forever; once we have ~100, stop generating new ones and pick randomly from the pool

## Observability
- [ ] Add timing logs for LLM and TTS steps (how long each segment takes end-to-end)

## UI — now playing
- [ ] Show the MP3 filename in the now-playing display

## Docs
- [ ] Update README to reflect current architecture (Web Audio, single-station config, persona system, etc.)

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

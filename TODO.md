# RadioDunc — Backlog

## UX / player polish

- [ ] **Transition "now playing" display** — show incoming track title/artist in the UI
  as soon as `POST /player/next` returns, rather than waiting for the server state poll.

- [ ] **Show filename as fallback label** — when `artist`/`title` metadata is missing,
  the now-playing label and queue display show "Unknown - Unknown". Fall back to the
  bare filename (strip path and extension) so the UI is always readable.

- [ ] **Display "Ad break" indicator in UI** — when `PlayerNextResponse.ad_clip_url`
  is present, show a brief "Ad break" badge in the now-playing area so the listener
  knows what the second voice is.

---

## One-ahead prefetch (performance)

- [ ] **Pre-generate next DJ clip in background** — after `player_next` returns, kick off
  a background thread that generates the *following* DJ clip (N+1 → N+2 transition) and
  caches it by hash. Pressing Next a second time would then return immediately with a
  cached clip instead of waiting 1–3 s for TTS.

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

# RadioDunc — Backlog

## UX / player polish

- [ ] **Transition "now playing" display** — show incoming track title/artist in the UI
  as soon as `POST /player/next` returns, rather than waiting for the server state poll.

- [ ] **Show filename as fallback label** — when `artist`/`title` metadata is missing,
  the now-playing label and queue display show "Unknown - Unknown". Fall back to the
  bare filename (strip path and extension) so the UI is always readable.

---

## Station / DJ personality

---

## Content enrichment

- [ ] **Real weather reports** — `dj_scripts.py` already has `include_weather` support
  wired to open-meteo; just enable it by default and pass the listener's location from
  config.

- [ ] **News alerts** — pull a short headline feed (RSS or a lightweight LLM web search)
  and inject a 1-sentence news item into the DJ script on a configurable cadence
  (e.g. every 3rd break).

- [ ] **LLM-generated ad breaks** — synthesise fake ads with a voice *different from the
  DJ* (pick a second voice from the OpenAI roster). Trigger on a configurable cadence
  (e.g. every 4th track). Backend: generate script → synthesise with alt voice →
  return as a special `ad_clip_url` alongside `dj_clip_url`.

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

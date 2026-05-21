'use strict';

// ── Timing constants ─────────────────────────────────────────────────────────
// Adjust these to taste; all audio scheduling uses AudioContext time (sample-accurate).
const FADE_OUT_S     = 9.0;  // current track fades 1→0 over this many seconds
const FADE_IN_S      = 1.2;  // next track fades 0→1 over this many seconds
// Per-segment peak gain. Speech RMS sits below a full music mix at the same
// digital peak, so spoken segments need a lift above unity to feel level.
// We split by segment type because ad voices are often delivered with more
// energy than DJ banter (especially "fable" / "echo" infomercial voices),
// so the SAME gain makes ads feel louder than DJ.
// OpenAI TTS comes out near -3 dB peaks; keep gains below ~2.2 to avoid clipping.
const DJ_GAIN        = 2.1;  // the DJ themselves — sit hot, like real radio
const NEWS_GAIN      = 1.7;  // professional, sit slightly behind the DJ
const AD_GAIN        = 1.6;  // ad voices are already energetic; don't pile on
const STINGER_GAIN   = 2.0;  // DJ-voice throws, close to DJ but not above
const DJ_EDGE_S      = 0.2;  // DJ clip's own tiny in/out fade
const AUTO_PREROLL_S = 10;   // start transition this many seconds before track end

// Auto-grow a textarea to fit its content. Wired up after the persona form
// renders so the personality / voice-instructions boxes expand as you type
// (and start at the right size for pre-existing content). Sets height to
// 'auto' first so the textarea can SHRINK on delete, then to scrollHeight
// so it grows to fit. Min-height comes from CSS.
function _autoResizeTextarea(el) {
  if (!el) return;
  const resize = () => {
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  };
  el.addEventListener('input', resize);
  resize();
}


// ── App state ────────────────────────────────────────────────────────────────
let serverState = null;

// ── Web Audio ────────────────────────────────────────────────────────────────
// Created lazily on first user gesture (autoplay policy).
let ctx        = null;
let masterGain = null;

// Two audio slots. We alternate which is "active" on each transition.
//   slot: { el: HTMLAudioElement, gainNode: GainNode }
const slots = { A: null, B: null };
let activeSlot = 'A';  // 'A' or 'B'

function curSlot()  { return slots[activeSlot]; }
function altSlot()  { return slots[activeSlot === 'A' ? 'B' : 'A']; }
function swapSlot() { activeSlot = activeSlot === 'A' ? 'B' : 'A'; }

// ── Transition guard ─────────────────────────────────────────────────────────
let transitioning    = false;
let autoTriggerTimer = null;
let _prefetchTimer   = null;
let _stingerTimer    = null;
// AudioContext time when the in-flight skip-stinger ends; 0 if none. Used to
// defer the DJ clip's start so they don't overlap.
let stingerEndTime   = 0;

// ── Pause state ───────────────────────────────────────────────────────────────
let paused = false;
// Sentinel: true if the auto-trigger timer was running when we paused (reschedule on resume).
let _autoTriggerRemaining = null;

// ── On-air mode ───────────────────────────────────────────────────────────────
// Tracks what's currently playing so renderPlayer() never stomps a badge.
// Values: 'track' | 'dj' | 'ad'
let onAirMode   = 'track';
// Each entry: { id: timeoutId, audioTime: number, mode: string, label: string|null }
let _modeTimers = [];
let _labelAnim  = null; // in-progress label animation (KeyframeEffect)

function clearModeTimers() {
  _modeTimers.forEach(t => clearTimeout(t.id));
  _modeTimers = [];
}

function scheduleMode(mode, delayMs, label = null) {
  // audioTime is the AudioContext clock instant this mode should fire at.
  // We store it so we can reschedule correctly after a pause/resume.
  const audioTime = ctx ? ctx.currentTime + delayMs / 1000 : 0;
  const entry = { id: null, audioTime, mode, label };
  entry.id = setTimeout(() => {
    _modeTimers = _modeTimers.filter(t => t !== entry);
    setOnAirMode(mode, label);
  }, delayMs);
  _modeTimers.push(entry);
}

function setOnAirMode(mode, label = null) {
  onAirMode = mode;
  const el = document.getElementById('nowPlaying');
  el.dataset.mode = mode;
  if (mode === 'ad') {
    animateLabel(el, '📻 Ad break');
  } else if (mode === 'news') {
    animateLabel(el, '📰 News');
  } else if (mode === 'dj') {
    // Show the active DJ's name when we have it (server keeps station.dj_name
    // persona-aware via active_station). When we also have the DJ's id —
    // exposed separately on serverState.station.active_dj_id, since
    // active_station flattens the DJ identity into name/personality and
    // drops the id along the way — slot a small avatar in front of the
    // text so the badge has more presence. Falls back to plain text when
    // any of that's missing (state not loaded yet, Default DJ hosting,
    // or no avatar generated for this DJ — the <img> onerror leaves the
    // coloured placeholder circle in place).
    const dj   = serverState?.station?.dj_name;
    const djId = serverState?.station?.active_dj_id;
    if (dj && djId) {
      // Background colour matches the DJ's schedule-grid + roster colour
      // so the placeholder circle (shown briefly while the image loads, or
      // permanently for DJs without a generated avatar) reads as the same
      // DJ across every surface.
      const colour = _djColourFor(djId);
      const html =
        `<span class="badge-avatar dj-avatar dj-avatar-xs" style="background:${colour};">` +
        `<img src="${_djAvatarUrl(djId)}" alt="" onerror="this.remove()" />` +
        `</span>🎙️ On air with ${_escapeText(dj)}`;
      animateLabel(el, html, { html: true });
    } else {
      animateLabel(el, dj ? `🎙️ On air with ${dj}` : '🎙️ On air');
    }
  } else {
    animateLabel(el, label || serverState?.now_playing_label || el.textContent || '-');
  }
}

async function animateLabel(el, newContent, { html = false } = {}) {
  // Early-bail when nothing's changed. For html mode we compare innerHTML
  // (whitespace-sensitive but predictable since we control both sides).
  if (html ? el.innerHTML === newContent : el.textContent === newContent) return;

  // Cancel any in-flight animation before starting a new one
  if (_labelAnim) { try { _labelAnim.cancel(); } catch (_) {} }

  const out = el.animate(
    [{ transform: 'translateY(0)', opacity: 1 },
     { transform: 'translateY(-110%)', opacity: 0 }],
    { duration: 200, easing: 'ease-in' }
  );
  _labelAnim = out;
  try { await out.finished; } catch (_) { return; }
  if (_labelAnim !== out) return; // superseded by a newer call

  if (html) el.innerHTML = newContent; else el.textContent = newContent;

  const inn = el.animate(
    [{ transform: 'translateY(110%)', opacity: 0 },
     { transform: 'translateY(0)',    opacity: 1 }],
    { duration: 200, easing: 'ease-out' }
  );
  _labelAnim = inn;
  inn.finished.then(() => { if (_labelAnim === inn) _labelAnim = null; }).catch(() => {});
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function savedVolume()    { return Number(localStorage.getItem('volume') ?? serverState?.volume ?? 80); }
function musicVolume()    { return Math.max(0, Math.min(1, savedVolume() / 100)); }

// Capture state at every playback entry point so we can diagnose intermittent
// bugs (e.g. unprompted playback after laptop wake) from a console transcript
// — the bug's repro is rare so we want full context every time anything
// playback-shaped fires.
//
// Wall-clock timestamps in the prefix (HH:MM:SS.mmm) so post-mortem console
// scrolls make it obvious which log lines are from "right now" vs left over
// from earlier in the session. Devtools' built-in timestamp setting works
// too but isn't reliable across browser sessions or copy/paste — baking it
// into the line itself is friction-free.
function _ts() {
  const d = new Date();
  const pad2 = (n) => String(n).padStart(2, '0');
  const pad3 = (n) => String(n).padStart(3, '0');
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}.${pad3(d.getMilliseconds())}`;
}

function _logPlayback(event, fields = {}) {
  console.log(`[playback ${_ts()}] ${event}`, {
    serverIsPlaying: serverState?.is_playing,
    paused, transitioning, hasCtx: !!ctx, onAirMode,
    ...fields,
  });
}

async function api(path, opts = {}) {
  const method = opts.method || 'GET';
  const isGet  = method === 'GET';
  const url    = isGet ? `${path}${path.includes('?') ? '&' : '?'}_=${Date.now()}` : path;
  const resp   = await fetch(url, {
    ...opts, method,
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    cache: isGet ? 'no-store' : opts.cache,
  });
  if (!resp.ok) throw new Error(`${method} ${path} → ${resp.status}: ${await resp.text()}`);
  if (resp.status === 204 || resp.headers.get('content-length') === '0') return null;
  return resp.json();
}

// ── AudioContext bootstrap ────────────────────────────────────────────────────
// Call from a user-gesture handler so the context starts in "running" state.
function initAudio() {
  if (ctx) return;
  ctx        = new AudioContext();
  masterGain = ctx.createGain();
  masterGain.gain.value = musicVolume(); // pre-set from localStorage before first status poll

  // Dynamics compressor sits at the end of the chain so every audio source
  // — music tracks, DJ banter, news, ads, stingers, previews — passes through
  // it on the way to the speakers. Broadcast-style settings: attenuate loud
  // peaks above -18 dBFS at a 4:1 ratio with a soft 12 dB knee, fast attack
  // (5 ms) so it catches transients, slowish release (100 ms) so it doesn't
  // pump audibly between syllables. Auto-flattens the loud/quiet gap between
  // voices (and between voices vs music) so the listener doesn't reach for
  // the volume knob mid-show. The per-voice gain trim still applies upstream
  // — for the typical voice it's effectively a no-op now, but it remains the
  // right knob for extreme outlier voices the compressor can't keep up with.
  const compressor = ctx.createDynamicsCompressor();
  compressor.threshold.value = -18;  // dB — start squashing above this level
  compressor.knee.value = 12;        // dB — soft transition into the threshold
  compressor.ratio.value = 4;        // 4:1 — gentle, not crushing
  compressor.attack.value = 0.005;   // 5 ms — fast enough to catch peaks
  compressor.release.value = 0.1;    // 100 ms — releases between phrases, not syllables

  masterGain.connect(compressor);
  compressor.connect(ctx.destination);

  for (const key of ['A', 'B']) {
    const el       = new Audio();
    el.crossOrigin = 'anonymous';
    const source   = ctx.createMediaElementSource(el);
    const gainNode = ctx.createGain();
    gainNode.gain.value = 0;
    source.connect(gainNode);
    gainNode.connect(masterGain);
    slots[key] = { el, gainNode };
  }
}

// ── Fetch + decode audio into an AudioBuffer ─────────────────────────────────
// Used for DJ clips: AudioBufferSourceNode.start(when) is frame-accurate.
async function fetchAndDecode(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`fetch ${url} → ${resp.status}`);
  return ctx.decodeAudioData(await resp.arrayBuffer());
}

// Click-prevention only — keeps short stingers from losing their first consonant.
// Longer DJ/news/ad clips use DJ_EDGE_S (0.2s) where the soft ramp is fine.
const STINGER_FADE_S = 0.02;

// Place an arbitrary buffer on the AudioContext timeline with a small in/out fade.
// Options:
//   fadeIn / fadeOut — override DJ_EDGE_S per segment (use a tiny value for
//                      short clips whose first/last consonant would otherwise
//                      be eaten by the default fade).
//   gain            — peak gain. Defaults to DJ_GAIN; callers pass NEWS_GAIN /
//                     AD_GAIN / STINGER_GAIN so segment loudness can be tuned
//                     independently of DJ banter.
// Returns the end time so callers can chain segments back-to-back.
function scheduleSegment(buf, startAt, label, { fadeIn = DJ_EDGE_S, fadeOut = DJ_EDGE_S, gain = DJ_GAIN } = {}) {
  // Clamp so fade-in and fade-out can never overlap on a very short clip.
  const inS  = Math.min(fadeIn,  buf.duration / 2);
  const outS = Math.min(fadeOut, buf.duration / 2);

  const src  = ctx.createBufferSource();
  src.buffer = buf;
  const g    = ctx.createGain();
  src.connect(g);
  g.connect(masterGain);
  g.gain.setValueAtTime(0, startAt);
  g.gain.linearRampToValueAtTime(gain, startAt + inS);
  g.gain.setValueAtTime(gain, startAt + buf.duration - outS);
  g.gain.linearRampToValueAtTime(0, startAt + buf.duration);
  src.start(startAt);
  console.log(`[audio] ${label}: start=${startAt.toFixed(3)} dur=${buf.duration.toFixed(2)} gain=${gain.toFixed(2)} in=${inS.toFixed(2)}`);
  return startAt + buf.duration;
}

// ── Auto-trigger scheduling ──────────────────────────────────────────────────
function clearAutoTrigger() {
  clearTimeout(autoTriggerTimer);
  autoTriggerTimer = null;
}

function scheduleAutoTrigger(trackDurationSec) {
  clearAutoTrigger();
  if (!trackDurationSec || !Number.isFinite(trackDurationSec)) return;
  const elapsed  = curSlot().el.currentTime || 0;
  const delaySec = Math.max(0, trackDurationSec - elapsed - AUTO_PREROLL_S);
  console.log(`[audio] auto-trigger in ${delaySec.toFixed(1)}s (dur=${trackDurationSec.toFixed(1)}s)`);
  autoTriggerTimer = setTimeout(() => {
    _logPlayback('autoTrigger.fired');
    triggerTransition('auto');
  }, delaySec * 1000);
}

// ── Prefetch scheduling ───────────────────────────────────────────────────────
// Fires a lightweight POST /player/prefetch ~20 s before the track ends so the
// server has time to generate the next DJ clip before the transition arrives.
const PREFETCH_LEAD_S = 20;

function clearPrefetchTimer() {
  clearTimeout(_prefetchTimer);
  _prefetchTimer = null;
}

function schedulePrefetch(trackDurationSec) {
  clearPrefetchTimer();
  if (!trackDurationSec || !Number.isFinite(trackDurationSec)) return;
  const elapsed  = curSlot().el.currentTime || 0;
  const delaySec = Math.max(0, trackDurationSec - elapsed - PREFETCH_LEAD_S);
  _prefetchTimer = setTimeout(() => {
    _prefetchTimer = null;
    fetch('/player/prefetch', { method: 'POST' }).catch(() => {});
  }, delaySec * 1000);
}

// ── Skip-stinger ──────────────────────────────────────────────────────────────
// On user-initiated Next, the DJ clip can take 5–10 s to generate + decode.
// To cover the dead-air gap we play a short cached station-ID clip ~3 s in.
// If the DJ clip arrives before the timer fires, it's cancelled. If the stinger
// is mid-playback when the DJ clip is ready, djStart is shifted to stingerEnd.
const SKIP_STINGER_DELAY_MS = 3000;

function clearStingerTimer() {
  if (_stingerTimer) clearTimeout(_stingerTimer);
  _stingerTimer = null;
}

async function _playSkipStinger() {
  _stingerTimer = null;
  if (!ctx) return;
  try {
    const resp = await fetch('/player/stinger-url');
    if (!resp.ok) return;
    const { clip_url: clipUrl } = await resp.json();
    if (!clipUrl || !ctx) return;
    const buf = await fetchAndDecode(clipUrl);
    if (!ctx) return;
    const start = ctx.currentTime + 0.05;
    scheduleSegment(buf, start, 'Skip stinger', { fadeIn: STINGER_FADE_S, fadeOut: STINGER_FADE_S, gain: STINGER_GAIN });
    stingerEndTime = start + buf.duration;
    setOnAirMode('dj');  // brief pink "On air" badge
  } catch (err) {
    console.warn('[stinger] failed:', err);
  }
}

// Fire cb as soon as the element has duration info. Works whether metadata
// has already loaded (calls immediately) or hasn't yet (waits for the event).
function whenDuration(el, cb) {
  if (el.duration && Number.isFinite(el.duration)) {
    cb();
  } else {
    el.addEventListener('loadedmetadata', cb, { once: true });
  }
}

// Load a track URL into an <audio> element, retrying on error (e.g. network
// drive temporarily unavailable → 503). Resolves when metadata is ready,
// rejects after all attempts are exhausted.
function loadWithRetry(el, url, { attempts = 3, delayMs = 2000 } = {}) {
  return new Promise((resolve, reject) => {
    let remaining = attempts;
    function attempt() {
      el.src = url;
      el.load();
      function onMeta() { cleanup(); resolve(); }
      function onError() {
        cleanup();
        remaining -= 1;
        if (remaining <= 0) {
          reject(new Error(`Failed to load track after ${attempts} attempts: ${url}`));
        } else {
          console.warn(`[audio] Track load failed, retrying in ${delayMs}ms (${remaining} left)…`);
          setTimeout(attempt, delayMs);
        }
      }
      function cleanup() {
        el.removeEventListener('loadedmetadata', onMeta);
        el.removeEventListener('error', onError);
      }
      el.addEventListener('loadedmetadata', onMeta, { once: true });
      el.addEventListener('error', onError, { once: true });
    }
    attempt();
  });
}

// ── Core crossfade transition ────────────────────────────────────────────────
//
// Timeline (happy path — API returns in < FADE_OUT_S):
//
//   t=0               t=FADE_OUT_S-DJ_OVERLAP_S        t=FADE_OUT_S
//   ├── current track fading out ──────────────────────────────────►
//                       ├── DJ clip playing ──────────────────────────── ► djEnd
//                                                    ├── next track fading in ──►
//
// If the API or decode takes longer than FADE_OUT_S, the current track has
// already gone silent; we just start the DJ clip the moment it's decoded.
// There may be a brief silence, but there's no crackle or restart.
async function triggerTransition(reason) {
  _logPlayback('triggerTransition.enter', { reason });
  if (transitioning) {
    console.log('[audio] transition suppressed (already in progress)');
    return;
  }
  // Defensive: triggerTransition should NEVER fire when the server says we're
  // stopped. If it does, a stale timer survived stopPlayback (or some other
  // path snuck through) — log loudly and bail rather than starting playback
  // the user didn't ask for. Suspected source of the lid-wake bug.
  if (!serverState?.is_playing) {
    console.warn(`[playback ${_ts()}] triggerTransition blocked — serverState says not playing`, { reason });
    return;
  }
  transitioning = true;
  clearAutoTrigger();
  clearPrefetchTimer();
  clearStingerTimer();
  stingerEndTime = 0;
  clearModeTimers();
  console.log('[audio] transition start:', reason);

  // On user skip, kick off a stinger after 3 s if the DJ clip isn't ready yet.
  // _playSkipStinger checks ctx and bails if the transition has already started
  // playing the DJ clip (stingerEndTime is reset to 0 once we schedule DJ).
  if (reason === 'user') {
    _stingerTimer = setTimeout(_playSkipStinger, SKIP_STINGER_DELAY_MS);
  }

  try {
    await ctx.resume();
    const t = ctx.currentTime;

    // 1. Schedule current track fade-out immediately (sample-accurate ramp).
    const curGain = curSlot().gainNode.gain;
    curGain.cancelScheduledValues(t);
    curGain.setValueAtTime(curGain.value, t);
    curGain.linearRampToValueAtTime(0, t + FADE_OUT_S);

    // 2. Advance queue on server, get DJ clip + next track URLs.
    let next;
    try {
      next = await api('/player/next', {
        method: 'POST',
        body: JSON.stringify({ reason: reason === 'user' ? 'skip' : 'auto' }),
      });
    } catch (err) {
      const isEndOfQueue = err.message?.includes('400');
      if (isEndOfQueue) {
        console.log('[audio] end of queue — stopping playback');
        document.getElementById('scanStatus').textContent = 'End of queue.';
        await stopPlayback();
      } else {
        console.error('[audio] /player/next failed — restoring gain:', err);
        const now = ctx.currentTime;
        curGain.cancelScheduledValues(now);
        curGain.setValueAtTime(curGain.value, now);
        curGain.linearRampToValueAtTime(1.0, now + 0.3);
      }
      return;
    }

    // Update filename immediately (no animation needed here).
    const fname = next.current_track_metadata?.file_path?.split('/').pop() || '';
    document.getElementById('nowPlayingFile').textContent = fname;

    // 3. Decode DJ clip and load next track in parallel.
    //    The gainNode on the alt slot is already at 0 from the previous transition
    //    or from init; just make sure.
    const alt = altSlot();
    alt.gainNode.gain.cancelScheduledValues(ctx.currentTime);
    alt.gainNode.gain.setValueAtTime(0, ctx.currentTime);

    let djBuf = null;
    const [djResult] = await Promise.allSettled([
      fetchAndDecode(next.dj_clip_url),
      loadWithRetry(alt.el, next.current_track_url),
    ]);
    if (djResult.status === 'fulfilled') {
      djBuf = djResult.value;
    } else {
      console.warn('[audio] DJ clip unavailable, crossfading without it:', djResult.reason);
    }

    // Optional news + ad + station-ID clips play after the DJ clip.
    let newsBuf = null;
    let adBuf = null;
    let sidBuf = null;
    const segmentFetches = [];
    if (next.news_clip_url) {
      segmentFetches.push(
        fetchAndDecode(next.news_clip_url)
          .then(b => { newsBuf = b; })
          .catch(err => console.warn('[audio] news clip unavailable, skipping:', err))
      );
    }
    if (next.ad_clip_url) {
      segmentFetches.push(
        fetchAndDecode(next.ad_clip_url)
          .then(b => { adBuf = b; })
          .catch(err => console.warn('[audio] ad clip unavailable, skipping:', err))
      );
    }
    if (next.station_id_clip_url) {
      segmentFetches.push(
        fetchAndDecode(next.station_id_clip_url)
          .then(b => { sidBuf = b; })
          .catch(err => console.warn('[audio] station ID clip unavailable, skipping:', err))
      );
    }
    if (segmentFetches.length) await Promise.allSettled(segmentFetches);

    // Cancel any pending skip-stinger now that the DJ clip is ready. If the
    // stinger has already started, stingerEndTime is set and we'll defer
    // djStart so they don't overlap; if it hasn't fired yet, the timer is
    // killed and we proceed normally.
    clearStingerTimer();

    // 4. Place clips on the AudioContext timeline. djStart may already be in
    //    the past (we awaited above); AudioContext handles that gracefully.
    //    Order: [Skip stinger ➜] DJ → News → Ad → Station ID → Track.
    const djStart = Math.max(ctx.currentTime + 0.05, stingerEndTime + 0.1);
    let cursor  = djStart;
    let djEnd   = djStart;
    let newsStart = null;
    let adStart   = null;
    let sidStart  = null;

    if (djBuf)   { djEnd = scheduleSegment(djBuf, djStart, 'DJ clip', { gain: DJ_GAIN }); cursor = djEnd; }
    if (newsBuf) { newsStart = cursor + 0.1; cursor = scheduleSegment(newsBuf, newsStart, 'News clip', { gain: NEWS_GAIN }); }
    if (adBuf)   { adStart   = cursor + 0.1; cursor = scheduleSegment(adBuf,   adStart,   'Ad clip',   { gain: AD_GAIN }); }
    if (sidBuf)  { sidStart  = cursor + 0.1; cursor = scheduleSegment(sidBuf,  sidStart,  'Station ID', { fadeIn: STINGER_FADE_S, fadeOut: STINGER_FADE_S, gain: STINGER_GAIN }); }
    djEnd = cursor; // re-use existing variable name for the "everything is done" timestamp

    // 4b. Schedule on-air mode indicators. Each clip gets its own badge.
    //     Station ID reuses the pink 'dj' badge — it's a DJ-voice throw back to music.
    const nowT = ctx.currentTime;
    const scheduleAt = (mode, audioTime, label = null) =>
      scheduleMode(mode, Math.max(0, (audioTime - nowT) * 1000), label);

    if (djBuf) {
      scheduleAt('dj', djStart);
      if (newsBuf) scheduleAt('news', newsStart);
      if (adBuf)   scheduleAt('ad',   adStart);
      if (sidBuf)  scheduleAt('dj',   sidStart);
      scheduleAt('track', cursor, next.current_track_label);
    } else {
      // No DJ clip: jump straight to whatever's first, or to the track.
      if (newsBuf) {
        scheduleAt('news', newsStart || djStart);
        if (adBuf)  scheduleAt('ad', adStart);
        if (sidBuf) scheduleAt('dj', sidStart);
        scheduleAt('track', cursor, next.current_track_label);
      } else if (adBuf) {
        scheduleAt('ad', adStart || djStart);
        if (sidBuf) scheduleAt('dj', sidStart);
        scheduleAt('track', cursor, next.current_track_label);
      } else {
        setOnAirMode('track', next.current_track_label);
      }
    }

    // 5. Schedule next track gain ramp and el.play().
    //    We start the ramp slightly before djEnd so the tracks overlap a little.
    //    We call el.play() just 100ms before the gain opens to avoid burning
    //    through the start of the track during the DJ clip.
    const trackGainStart = Math.max(ctx.currentTime + 0.1, djEnd - FADE_IN_S * 0.3);
    const trackPlayAt    = trackGainStart - 0.1; // 100ms before gain opens

    alt.gainNode.gain.setValueAtTime(0, trackGainStart);
    alt.gainNode.gain.linearRampToValueAtTime(1.0, trackGainStart + FADE_IN_S);

    const playDelayMs = Math.max(0, (trackPlayAt - ctx.currentTime) * 1000);
    setTimeout(() => {
      alt.el.currentTime = 0;
      alt.el.play().catch(e => console.warn('[audio] next track play() failed:', e));
    }, playDelayMs);

    // 6. Swap slots: alt becomes the new active track.
    //    After the swap, altSlot() returns the old active slot (now fading out).
    swapSlot();
    const oldSlot    = altSlot();
    const cleanupMs  = Math.max(100, (t + FADE_OUT_S + 0.5 - ctx.currentTime) * 1000);
    setTimeout(() => {
      oldSlot.gainNode.gain.cancelScheduledValues(ctx.currentTime);
      oldSlot.gainNode.gain.setValueAtTime(0, ctx.currentTime);
      oldSlot.el.pause();
      oldSlot.el.src = '';
    }, cleanupMs);

    // 7. Schedule auto-trigger for the new track.
    //    By the time the next track starts playing, metadata is almost always
    //    loaded already (we set src early during the transition). whenDuration
    //    handles both cases — fires immediately or waits for loadedmetadata.
    const triggerSetupMs = Math.max(0, (trackGainStart + 0.3 - ctx.currentTime) * 1000);
    setTimeout(() => {
      whenDuration(curSlot().el, () => {
        scheduleAutoTrigger(curSlot().el.duration);
        schedulePrefetch(curSlot().el.duration);
      });
    }, triggerSetupMs);

    // 8. Refresh display after the new track has settled.
    setTimeout(async () => {
      try { serverState = await api('/player/status'); renderAll(); } catch (_) {}
    }, triggerSetupMs + 500);

  } finally {
    transitioning = false;
  }
}

// ── Start playback ────────────────────────────────────────────────────────────
// Loads the server's current_track into slot A and starts playback with a gentle
// fade-in. Shared between startPlayback (fresh queue) and resumeAfterRefresh.
async function _playCurrentTrackFromServer() {
  if (!serverState?.current_track?.id) throw new Error('Server returned no current track');

  for (const key of ['A', 'B']) {
    slots[key].gainNode.gain.cancelScheduledValues(ctx.currentTime);
    slots[key].gainNode.gain.setValueAtTime(0, ctx.currentTime);
    slots[key].el.pause();
    slots[key].el.src = '';
  }
  activeSlot = 'A';

  const cur = curSlot();
  await loadWithRetry(cur.el, `/media/track/${serverState.current_track.id}`);
  await cur.el.play();

  cur.gainNode.gain.setValueAtTime(0, ctx.currentTime);
  cur.gainNode.gain.linearRampToValueAtTime(1.0, ctx.currentTime + 0.5);

  scheduleAutoTrigger(cur.el.duration);
  schedulePrefetch(cur.el.duration);

  renderAll();
}

async function startPlayback() {
  _logPlayback('startPlayback.enter');
  paused = false;
  _autoTriggerRemaining = null;
  initAudio();
  await ctx.resume();

  const resp = await api('/player/play', {
    method: 'POST',
    body: JSON.stringify({ queue_size: 30 }),
  });
  serverState = resp.state;
  await _playCurrentTrackFromServer();
}

// After a page refresh while paused (or while playing), the server still thinks
// is_playing=true but the client has no AudioContext. This picks up the current
// track at position 0 and resumes without rebuilding the queue.
async function resumeAfterRefresh() {
  _logPlayback('resumeAfterRefresh.enter');
  paused = false;
  _autoTriggerRemaining = null;
  initAudio();
  await ctx.resume();
  serverState = await api('/player/status');
  await _playCurrentTrackFromServer();
}

// ── Stop playback ─────────────────────────────────────────────────────────────
async function stopPlayback() {
  _logPlayback('stopPlayback.enter');
  clearAutoTrigger();
  clearPrefetchTimer();
  clearStingerTimer();
  stingerEndTime = 0;
  clearModeTimers();
  onAirMode = 'track';
  transitioning = false;
  paused = false;
  _autoTriggerRemaining = null;
  const t = ctx?.currentTime ?? 0;
  for (const s of Object.values(slots)) {
    if (!s) continue;
    s.gainNode?.gain.cancelScheduledValues(t);
    s.gainNode?.gain.setValueAtTime(0, t);
    s.el?.pause();
    if (s.el) s.el.src = '';
  }
  try {
    const resp = await api('/player/stop', { method: 'POST' });
    serverState = resp.state;
  } catch (_) {
    if (serverState) serverState = { ...serverState, is_playing: false };
  }
  renderAll();
}

// ── Pause playback ────────────────────────────────────────────────────────────
async function pausePlayback() {
  _logPlayback('pausePlayback.enter');
  if (!ctx || paused) return;
  paused = true;

  // Freeze the AudioContext — all scheduled audio and gain ramps freeze too.
  await ctx.suspend();

  // Cancel the auto-trigger timer; on resume we reschedule it from el.currentTime.
  if (autoTriggerTimer !== null) {
    clearAutoTrigger();
    _autoTriggerRemaining = true;
  }

  // Suspend mode timers: cancel each setTimeout, keep the audioTime targets.
  _modeTimers.forEach(t => clearTimeout(t.id));
  // Leave _modeTimers in place with their audioTime values intact — resume will reschedule.

  renderPlayer();
}

// ── Resume playback ───────────────────────────────────────────────────────────
async function resumePlayback() {
  _logPlayback('resumePlayback.enter');
  if (!ctx || !paused) return;
  paused = false;

  await ctx.resume();

  // Reschedule mode timers based on how far the AudioContext is now vs. their targets.
  const nowAudio = ctx.currentTime;
  _modeTimers = _modeTimers.map(t => {
    const remainingSec = t.audioTime - nowAudio;
    const delayMs = Math.max(0, remainingSec * 1000);
    const entry = { ...t, id: null };
    entry.id = setTimeout(() => {
      _modeTimers = _modeTimers.filter(x => x !== entry);
      setOnAirMode(entry.mode, entry.label);
    }, delayMs);
    return entry;
  });

  // Reschedule the auto-trigger timer.
  if (_autoTriggerRemaining) {
    _autoTriggerRemaining = null;
    const cur = curSlot();
    if (cur?.el?.duration) {
      scheduleAutoTrigger(cur.el.duration);
    }
  }

  renderPlayer();
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderPlayer() {
  const station = serverState?.station;
  document.getElementById('stationName').textContent = station?.name || 'RadioDunc';
  document.getElementById('stationMeta').textContent = station?.tagline || 'Loading station…';
  document.getElementById('volume').value = savedVolume();
  if (masterGain) masterGain.gain.value = musicVolume();

  // Only push the server label when no badge is active; the mode timers own the
  // nowPlaying element while a DJ clip or ad is playing.
  if (onAirMode === 'track') {
    const label = serverState?.now_playing_label || '-';
    const el = document.getElementById('nowPlaying');
    el.dataset.mode = 'track';
    if (el.textContent !== label) animateLabel(el, label);
  }

  const track = serverState?.current_track;
  const filename = track?.file_path ? track.file_path.split('/').pop() : '';
  document.getElementById('nowPlayingFile').textContent = filename;

  const isPlaying = !!serverState?.is_playing;
  // After a refresh, server says we're playing but ctx is null — treat that as
  // a "ready to resume" state, not a live one.
  const audioLive = isPlaying && !!ctx && !paused;
  document.getElementById('nowPlayingCard').classList.toggle('active', audioLive);

  const flags = document.getElementById('playerFlags');
  if (isPlaying && !ctx) {
    flags.textContent = 'Ready — click Play to resume';
  } else if (isPlaying && paused) {
    flags.textContent = 'Paused';
  } else if (isPlaying) {
    const statusText = transitioning ? 'Transitioning…' : 'On air';
    flags.innerHTML = `<span class="live-dot"></span>${statusText}`;
  } else {
    flags.textContent = 'Stopped';
  }

  const playBtn = document.getElementById('playBtn');
  playBtn.textContent = audioLive ? 'Pause' : 'Play';
}

async function renderQueue() {
  const list = document.getElementById('queueList');
  if (!list) return;
  if (!serverState?.is_playing) { list.innerHTML = ''; return; }
  let preview;
  try { preview = await api('/player/queue'); } catch (_) { list.innerHTML = ''; return; }
  list.innerHTML = '';
  if (!preview.items.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No upcoming tracks';
    list.appendChild(li);
    return;
  }

  let dragSrc = null; // position of the item being dragged

  for (const item of preview.items) {
    const li = document.createElement('li');
    li.dataset.position = item.position;
    li.draggable = true;

    const handle = document.createElement('span');
    handle.textContent = '⠿';
    handle.className = 'handle';

    const span = document.createElement('span');
    span.textContent = item.label;
    span.className = 'track-label';

    const btn = document.createElement('button');
    btn.textContent = '✕';
    btn.title = 'Remove from queue';
    btn.style.cssText = 'padding:1px 6px; font-size:0.8em;';
    btn.onclick = async () => {
      try {
        await api(`/player/queue/${item.position}`, { method: 'DELETE' });
        renderQueue();
      } catch (e) {
        alert('Could not remove track: ' + (e.message || e));
      }
    };

    li.addEventListener('dragstart', e => {
      dragSrc = item.position;
      e.dataTransfer.effectAllowed = 'move';
      setTimeout(() => { li.style.opacity = '0.4'; }, 0);
    });
    li.addEventListener('dragend', () => {
      li.style.opacity = '';
      list.querySelectorAll('li').forEach(el => { el.style.borderTopColor = 'transparent'; });
    });
    li.addEventListener('dragover', e => {
      if (dragSrc === null || dragSrc === item.position) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      list.querySelectorAll('li').forEach(el => { el.style.borderTopColor = 'transparent'; });
      li.style.borderTopColor = '#38bdf8';
    });
    li.addEventListener('drop', async e => {
      e.preventDefault();
      if (dragSrc === null || dragSrc === item.position) return;
      const from = dragSrc;
      const to   = item.position;
      dragSrc = null;
      try {
        await api('/player/queue/reorder', { method: 'POST', body: JSON.stringify({ from_position: from, to_position: to }) });
        renderQueue();
      } catch (err) {
        console.warn('[queue] reorder failed:', err);
        renderQueue();
      }
    });

    li.appendChild(handle);
    li.appendChild(span);
    li.appendChild(btn);
    list.appendChild(li);
  }

  // Append "Add more" button as a sibling of the list (not inside it),
  // but only once — reuse the existing button if already present.
  const parent = list.parentNode;
  let extendBtn = document.getElementById('extendQueueBtn');
  if (!extendBtn) {
    extendBtn = document.createElement('button');
    extendBtn.id = 'extendQueueBtn';
    extendBtn.style.cssText = 'margin-top:10px; width:100%; font-size:0.85rem;';
    extendBtn.onclick = async () => {
      extendBtn.disabled = true;
      extendBtn.textContent = 'Adding…';
      try {
        await api('/player/queue/extend', { method: 'POST', body: JSON.stringify({ count: 10 }) });
        await renderQueue();
      } catch (e) {
        extendBtn.disabled = false;
        extendBtn.textContent = '+ Add more tracks';
      }
    };
    parent.appendChild(extendBtn);
  }
  extendBtn.disabled = false;
  extendBtn.textContent = '+ Add more tracks';
}

function renderAll() { renderPlayer(); renderQueue(); }

// ── DJ Schedule grid ──────────────────────────────────────────────────────────
const DAYS_FULL = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday'];
const DAYS_SHORT = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
// Tuned palette — distinct hues that sit well on the dark theme.
const PERSONA_COLORS = [
  '#e879a0',  // pink (matches site accent)
  '#fb923c',  // orange
  '#60a5fa',  // blue
  '#a78bfa',  // purple
  '#34d399',  // teal
  '#fbbf24',  // amber
  '#f87171',  // coral
  '#22d3ee',  // cyan
];

function _personaColor(index) {
  return PERSONA_COLORS[index % PERSONA_COLORS.length];
}

// JS getDay(): 0=Sunday..6=Saturday. We want 0=Monday..6=Sunday for our grid.
function _jsDayToGridIndex(jsDay) { return (jsDay + 6) % 7; }

function _appendCell(grid, content, className, col, row) {
  const el = document.createElement('div');
  el.className = className;
  el.style.gridColumn = String(col);
  el.style.gridRow = String(row);
  if (content) el.textContent = content;
  grid.appendChild(el);
  return el;
}

// Resolve a Show's display: DJ name (primary), show name (caption), and the
// colour applied to the block. dj_id=null means the Default DJ hosts the slot
// — those get a distinct dashed-border treatment so the schedule reads at a
// glance ("here's where the station's own DJ takes over").
function _showDisplay(show, djsById, djColorByIdx, stationDjName) {
  const isDefault = !show.dj_id;
  const dj = isDefault ? null : djsById.get(show.dj_id);
  const djName = dj ? dj.name : (stationDjName || 'Default DJ');
  // Colour: use the DJ's position in djs[] so the same DJ is always the same
  // colour, even when they host multiple shows. Default-DJ slots render in
  // a neutral slate with a dashed outline; never picks up a palette colour.
  const colour = (dj && djColorByIdx.has(dj.id))
    ? djColorByIdx.get(dj.id)
    : '#334155';  // matches the legend's "default" swatch
  return { dj, djName, isDefault, colour, showName: show.name || '' };
}

function _appendShowBlock(grid, show, display, col, rowStart, rowEndExclusive, meta = {}) {
  const block = document.createElement('div');
  block.className = 'grid-persona-block';
  if (display.isDefault) block.classList.add('default-dj');
  block.style.gridColumn = String(col);
  block.style.gridRow = `${rowStart} / ${rowEndExclusive}`;
  block.style.backgroundColor = display.colour;
  // Tooltip carries DJ + show name + (if present) the DJ's personality so you
  // get a quick read on hover without having to open the editor.
  const tooltipParts = [display.djName];
  if (display.showName) tooltipParts.push(`Show: ${display.showName}`);
  if (display.dj?.personality) tooltipParts.push(display.dj.personality);
  block.title = tooltipParts.join(' — ');

  // Label layout: show name leads ("<showName> with <djName>") when set;
  // DJ name alone otherwise. Wraps naturally to multiple lines on narrow
  // cells so long combinations like "The Morning Stumble with Taco Steve"
  // stay readable instead of getting truncated to "The Mor…".
  // 1-hour blocks fall back to a single-letter monogram (of the show name if
  // present, else the DJ name) so the grid doesn't get visually crowded.
  const height = rowEndExclusive - rowStart;
  if (height >= 2) {
    const label = document.createElement('div');
    label.className = 'block-label';
    if (display.showName) {
      // Two spans so CSS can dim the "with <DJ>" tail and let the show name
      // read as the primary line.
      const lead = document.createElement('span');
      lead.className = 'block-label-lead';
      lead.textContent = display.showName;
      const tail = document.createElement('span');
      tail.className = 'block-label-tail';
      tail.textContent = ` with ${display.djName}`;
      label.appendChild(lead);
      label.appendChild(tail);
    } else {
      label.textContent = display.djName;
    }
    block.appendChild(label);
  } else {
    // 1-hour blocks: monogram. Prefer the show name's initial when set
    // (matches the lead in the longer-block layout).
    const monoSource = display.showName || display.djName;
    block.textContent = monoSource.slice(0, 1);
  }

  // Tag with the show id (stable across renders, even if list order changes)
  // and the shift index so click + drag handlers can resolve the backing row.
  block.dataset.showId = show.id;
  if (meta.shiftIdx != null) block.dataset.shiftIdx = String(meta.shiftIdx);
  if (meta.isWrap)            block.dataset.isWrap   = '1';
  if (meta.wrapHalf)          block.dataset.wrapHalf = meta.wrapHalf;  // 'today' | 'tomorrow'

  // Add resize handles on non-wrap shifts. Wrap shifts (start > end render as
  // two blocks across midnight) are still editable via the form.
  if (!meta.isWrap) {
    const top = document.createElement('div');    top.className    = 'resize-handle top';
    const bottom = document.createElement('div'); bottom.className = 'resize-handle bottom';
    top.dataset.edge = 'top';
    bottom.dataset.edge = 'bottom';
    block.appendChild(top);
    block.appendChild(bottom);
  }
  grid.appendChild(block);
}

async function renderSchedule() {
  const grid = document.getElementById('scheduleGrid');
  const legend = document.getElementById('scheduleLegend');
  if (!grid) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const station = config.station || {};
  const djs   = station.djs   || [];
  const shows = station.shows || [];

  // Build lookups. djColorByIdx pins each DJ to a stable palette index so
  // every Show hosted by the same DJ paints in the same colour.
  const djsById = new Map(djs.map(d => [d.id, d]));
  const djColorByIdx = new Map(djs.map((d, i) => [d.id, _personaColor(i)]));

  // Which DJs actually appear in any Show? Drives the legend so we don't show
  // chips for orphan DJs (created but never scheduled). Slice 6's DJ Roster
  // view will be the place to see those.
  const djsUsed = new Set(shows.map(s => s.dj_id).filter(Boolean));
  const anyDefaultSlot = shows.some(s => !s.dj_id);

  // ── Legend
  legend.innerHTML = '';
  // Default-DJ chip — always present so users know who hosts unscheduled
  // hours. Use the dashed-border swatch when a Show explicitly uses the
  // default slot; solid otherwise still reads as "this is the fallback".
  const baseChip = document.createElement('span');
  baseChip.className = 'legend-item';
  const baseHint = anyDefaultSlot ? ' (default)' : ' (default, unused)';
  baseChip.innerHTML = `<span class="legend-swatch default-swatch"></span>` +
                       `${station.dj_name || 'Default DJ'}${baseHint}`;
  legend.appendChild(baseChip);

  djs.forEach((dj) => {
    if (!djsUsed.has(dj.id)) return;  // orphan DJ — surfaces in the Roster, not here
    const chip = document.createElement('span');
    chip.className = 'legend-item';
    // Tooltip lists the show names this DJ hosts (when any have a name). Makes
    // the contrast between DJ identity and show identity legible at a glance.
    const hosted = shows.filter(s => s.dj_id === dj.id);
    const namedShows = hosted.map(s => s.name).filter(Boolean);
    const tip = namedShows.length
      ? `${dj.name} — hosts: ${namedShows.join(', ')}`
      : `${dj.name}`;
    chip.title = tip;
    chip.innerHTML = `<span class="legend-swatch" style="background:${djColorByIdx.get(dj.id)};"></span>${dj.name}`;
    legend.appendChild(chip);
  });

  // Surface Shows that exist but won't air (no shifts). They're invisible on
  // the grid by design (the resolver skips them), but lurking-and-uneditable
  // is worse than a soft "click to fix" affordance.
  const unscheduled = shows.filter(s => !(s.shifts || []).length);
  unscheduled.forEach((show) => {
    const display = _showDisplay(show, djsById, djColorByIdx, station.dj_name);
    const chip = document.createElement('span');
    chip.className = 'legend-item legend-unscheduled';
    chip.dataset.showId = show.id;
    chip.title = 'No shifts — this show won\'t air. Click to add shifts.';
    chip.innerHTML = `<span class="legend-swatch warning-swatch"></span>` +
                     `${display.djName}${display.showName ? ` · ${display.showName}` : ''} <span class="muted">(no shifts)</span>`;
    chip.addEventListener('click', () => _openShowEditor(show.id));
    legend.appendChild(chip);
  });

  // ── Grid scaffold
  grid.innerHTML = '';
  // Empty top-left corner cell
  _appendCell(grid, '', 'grid-corner', 1, 1);
  // Day headers (row 1, cols 2-8)
  DAYS_SHORT.forEach((d, i) => _appendCell(grid, d, 'grid-day-header', i + 2, 1));
  // Hour labels (col 1, rows 2-25). Show every 3rd hour to reduce visual noise.
  for (let h = 0; h < 24; h++) {
    _appendCell(grid, h % 3 === 0 ? String(h).padStart(2, '0') : '', 'grid-hour-label', 1, h + 2);
  }

  // ── Show blocks (one block per shift; iterate in shows[] order so overlap
  // ordering is predictable and matches the resolver's first-match-wins rule)
  shows.forEach((show) => {
    const display = _showDisplay(show, djsById, djColorByIdx, station.dj_name);
    const shifts = show.shifts || [];
    shifts.forEach((shift, shiftIdx) => {
      const dayIdx = DAYS_FULL.indexOf(shift.day);
      if (dayIdx === -1) return;
      const col = dayIdx + 2;
      const start = Number(shift.start_hour);
      const end = Number(shift.end_hour);
      if (start <= end) {
        _appendShowBlock(grid, show, display, col, start + 2, end + 3, { shiftIdx });
      } else {
        // Wraps past midnight: render two blocks (today + tomorrow)
        _appendShowBlock(grid, show, display, col, start + 2, 26,
                         { shiftIdx, isWrap: true, wrapHalf: 'today' });
        const tomorrowCol = ((dayIdx + 1) % 7) + 2;
        _appendShowBlock(grid, show, display, tomorrowCol, 2, end + 3,
                         { shiftIdx, isWrap: true, wrapHalf: 'tomorrow' });
      }
    });
  });

  // ── NOW indicator: glowing outline on the current hour cell
  const now = new Date();
  const nowCol = _jsDayToGridIndex(now.getDay()) + 2;
  const nowRow = now.getHours() + 2;
  _appendCell(grid, '', 'grid-now-indicator', nowCol, nowRow);

  // The grid's DOM is fully replaced on every render, taking the previous
  // blocks' click/mousedown listeners with it. Re-wire here so callers can't
  // forget — the 60-second auto-refresh used to leave the grid unresponsive
  // until the drawer was closed and reopened.
  _attachBlockClickHandlers();
}

// Refresh the NOW indicator + roster view periodically. 60s is the natural
// cadence since the indicator only moves by the hour, but we re-fetch the
// roster too so live config edits are reflected without a page reload.
function _scheduleAutoRefresh() {
  setInterval(() => {
    if (document.getElementById('wrap')?.dataset.mode === 'scheduler') renderSchedule();
  }, 60_000);
}

// ── Scheduler mode (sidebar takeover) ───────────────────────────────────────
const OPENAI_VOICES = ['alloy','ash','ballad','coral','echo','fable','onyx','nova','sage','shimmer','verse'];

function _setSchedulerMode(on) {
  const wrap = document.getElementById('wrap');
  if (!wrap) return;
  wrap.dataset.mode = on ? 'scheduler' : 'default';
  if (on) {
    _setSchedulerSubView('grid');
    renderSchedule();
  }
}

function _setSchedulerSubView(name) {
  const panel = document.querySelector('.sidebar-scheduler');
  if (panel) panel.dataset.subView = name;
}

// After renderSchedule() repaints the grid, wire up block interactions:
// - Click body → open the editor for that persona
// - Drag top/bottom handle → snap-to-hour resize, persists via PUT /config
//
// We do this in a separate pass so renderSchedule stays purely a draw function.
async function _attachBlockClickHandlers() {
  const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');
  blocks.forEach(block => {
    // Wrap-around shifts can't be moved/resized in v1; keep them as a plain
    // click-to-edit affordance with no grab cursor.
    block.style.cursor = block.dataset.isWrap ? 'pointer' : 'grab';
    block.addEventListener('mousedown', _onBlockMouseDown);
    block.addEventListener('click', _onBlockClick);
  });

  document.querySelectorAll('#scheduleGrid .resize-handle').forEach(handle => {
    handle.addEventListener('mousedown', _startResizeDrag);
  });
}

let _suppressNextClick = false;

// Set _suppressNextClick safely: also queues an auto-reset on the next macrotask
// so the flag doesn't get stuck-true if the post-drag click never fires (e.g.
// mouseup lands off the dragged block, or renderSchedule replaces the DOM
// before the click is dispatched). Without this, ALL subsequent block clicks
// got eaten silently until the schedule was closed and reopened.
function _suppressClickOnce() {
  _suppressNextClick = true;
  setTimeout(() => { _suppressNextClick = false; }, 0);
}

function _onBlockMouseDown(e) {
  // Resize handles take precedence (they have their own mousedown handler).
  if (e.target.classList.contains('resize-handle')) return;
  // Wrap-around blocks don't move; let the click through to open the editor.
  if (e.currentTarget.dataset.isWrap) return;
  _startMoveDrag(e);
}

function _onBlockClick(e) {
  // A move-drag completing fires mouseup, then click. Skip the click in that case.
  if (_suppressNextClick) {
    _suppressNextClick = false;
    e.stopPropagation();
    e.preventDefault();
    return;
  }
  if (e.target.classList.contains('resize-handle')) return;
  const showId = e.currentTarget.dataset.showId;
  if (showId) _openShowEditor(showId);
}

// ── Drag-to-resize for shift blocks ─────────────────────────────────────────
// Snap-to-hour. We compute the row-height from a hour-label cell, then convert
// pointer deltas to hour deltas. The block is repositioned on-grid via inline
// grid-row updates during drag; PUT /config fires once on release.
function _gridRowHeightPx() {
  const cell = document.querySelector('#scheduleGrid .grid-hour-label');
  // .grid-hour-label is single-row, so its height is the row height. The 2px
  // grid gap is accounted for by getBoundingClientRect (excludes gap).
  // Fall back to 22px (the CSS default) if not measurable.
  return cell ? cell.getBoundingClientRect().height + 2 /* gap */ : 24;
}

let _dragState = null;  // active drag, or null

async function _startResizeDrag(e) {
  e.preventDefault();
  e.stopPropagation();
  const handle = e.currentTarget;
  const block = handle.closest('.grid-persona-block');
  if (!block) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const showId = block.dataset.showId;
  const shiftIdx = Number(block.dataset.shiftIdx);
  const show = (config.station?.shows || []).find(s => s.id === showId);
  const shift = show?.shifts?.[shiftIdx];
  if (!shift) return;

  _dragState = {
    config,
    show,
    shift,
    block,
    edge: handle.dataset.edge,
    startY: e.clientY,
    rowHeight: _gridRowHeightPx(),
    originalStartHour: Number(shift.start_hour),
    originalEndHour: Number(shift.end_hour),
    newStartHour: Number(shift.start_hour),
    newEndHour: Number(shift.end_hour),
  };
  block.classList.add('dragging');

  // Floating readout follows the cursor with the current hour range.
  const readout = document.createElement('div');
  readout.className = 'drag-readout';
  document.body.appendChild(readout);
  _dragState.readout = readout;
  _updateDragReadout(e.clientX, e.clientY);

  document.addEventListener('mousemove', _onDragMove);
  document.addEventListener('mouseup',   _onDragEnd, { once: true });
}

function _onDragMove(e) {
  if (!_dragState) return;
  const deltaPx = e.clientY - _dragState.startY;
  const deltaHours = Math.round(deltaPx / _dragState.rowHeight);

  if (_dragState.edge === 'bottom') {
    // Resize end_hour. Min: 1h shift (end >= start). Max: 23.
    const newEnd = Math.max(_dragState.originalStartHour,
                            Math.min(23, _dragState.originalEndHour + deltaHours));
    _dragState.newEndHour = newEnd;
  } else {
    // Resize start_hour. Min: 0. Max: end_hour (shift stays >= 1h).
    const newStart = Math.min(_dragState.originalEndHour,
                              Math.max(0, _dragState.originalStartHour + deltaHours));
    _dragState.newStartHour = newStart;
  }

  // Live grid-row update for visual feedback.
  _dragState.block.style.gridRow = `${_dragState.newStartHour + 2} / ${_dragState.newEndHour + 3}`;
  _updateDragReadout(e.clientX, e.clientY);
}

function _updateDragReadout(x, y) {
  if (!_dragState?.readout) return;
  const fmt = (h) => String(h).padStart(2, '0') + ':00';
  _dragState.readout.textContent = `${fmt(_dragState.newStartHour)} – ${fmt(_dragState.newEndHour)}`;
  _dragState.readout.style.left = `${x + 14}px`;
  _dragState.readout.style.top  = `${y + 4}px`;
}

async function _onDragEnd() {
  if (!_dragState) return;
  document.removeEventListener('mousemove', _onDragMove);
  _dragState.block.classList.remove('dragging');
  _dragState.readout?.remove();
  // Defensive: a drag-resize that lands on the block body could otherwise
  // trigger _onBlockClick and open the editor.
  _suppressClickOnce();

  const changed = _dragState.newStartHour !== _dragState.originalStartHour
                || _dragState.newEndHour   !== _dragState.originalEndHour;
  if (!changed) {
    _dragState = null;
    return;
  }

  _dragState.shift.start_hour = _dragState.newStartHour;
  _dragState.shift.end_hour   = _dragState.newEndHour;
  const configToSave = _dragState.config;
  _dragState = null;

  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(configToSave) });
    await renderSchedule();
  } catch (err) {
    console.warn('[schedule] save after resize failed:', err);
    await renderSchedule();
  }
}

// ── Drag-to-move for shift blocks ───────────────────────────────────────────
// mousedown on block body → start tracking. Wait for movement past a small
// threshold before entering drag mode, so a quick click still opens the editor.
// Once in drag mode, snap-to-cell on both axes; clamp inside the grid; save on
// release. Wrap-around shifts are excluded (would need to handle the two-block
// rendering on day change — out of scope for v1).
const MOVE_THRESHOLD_PX = 4;
let _moveState = null;

function _gridColWidthPx() {
  const cell = document.querySelector('#scheduleGrid .grid-day-header');
  return cell ? cell.getBoundingClientRect().width + 2 /* gap */ : 100;
}

async function _startMoveDrag(e) {
  e.preventDefault();
  const block = e.currentTarget;
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const showId = block.dataset.showId;
  const shiftIdx = Number(block.dataset.shiftIdx);
  const show = (config.station?.shows || []).find(s => s.id === showId);
  const shift = show?.shifts?.[shiftIdx];
  if (!shift) return;

  const start = Number(shift.start_hour);
  const end = Number(shift.end_hour);
  // Skip wrap-around defensively (the mousedown guard should already have
  // bailed for these, but belt-and-braces).
  if (start > end) return;

  _moveState = {
    config,
    show,
    shift,
    block,
    startX: e.clientX,
    startY: e.clientY,
    rowHeight: _gridRowHeightPx(),
    colWidth: _gridColWidthPx(),
    originalDayIdx: DAYS_FULL.indexOf(shift.day),
    originalStart: start,
    duration: end - start,
    newDayIdx: DAYS_FULL.indexOf(shift.day),
    newStart: start,
    moved: false,
    readout: null,
  };

  document.addEventListener('mousemove', _onMoveDragMove);
  document.addEventListener('mouseup', _onMoveDragEnd, { once: true });
}

function _onMoveDragMove(e) {
  if (!_moveState) return;
  const dx = e.clientX - _moveState.startX;
  const dy = e.clientY - _moveState.startY;

  // Below the threshold, stay in click mode (no visual change yet).
  if (!_moveState.moved) {
    if (Math.abs(dx) < MOVE_THRESHOLD_PX && Math.abs(dy) < MOVE_THRESHOLD_PX) return;
    _moveState.moved = true;
    _moveState.block.classList.add('dragging');
    _moveState.block.style.cursor = 'grabbing';
    _moveState.readout = document.createElement('div');
    _moveState.readout.className = 'drag-readout';
    document.body.appendChild(_moveState.readout);
  }

  const dayDelta = Math.round(dx / _moveState.colWidth);
  const hourDelta = Math.round(dy / _moveState.rowHeight);

  _moveState.newDayIdx = Math.max(0, Math.min(6, _moveState.originalDayIdx + dayDelta));
  // Keep the shift inside 0..23. If duration is N hours, start is bounded by
  // [0, 23 - N] so end never exceeds 23.
  const maxStart = 23 - _moveState.duration;
  _moveState.newStart = Math.max(0, Math.min(maxStart, _moveState.originalStart + hourDelta));

  // Live grid position update.
  _moveState.block.style.gridColumn = String(_moveState.newDayIdx + 2);
  _moveState.block.style.gridRow = `${_moveState.newStart + 2} / ${_moveState.newStart + _moveState.duration + 3}`;

  // Readout follows the cursor: "Wed 14:00 – 16:00"
  const fmt = (h) => String(h).padStart(2, '0') + ':00';
  _moveState.readout.textContent =
    `${DAYS_SHORT[_moveState.newDayIdx]}  ${fmt(_moveState.newStart)} – ${fmt(_moveState.newStart + _moveState.duration)}`;
  _moveState.readout.style.left = `${e.clientX + 14}px`;
  _moveState.readout.style.top  = `${e.clientY + 4}px`;
}

async function _onMoveDragEnd(e) {
  if (!_moveState) return;
  document.removeEventListener('mousemove', _onMoveDragMove);

  if (!_moveState.moved) {
    // No movement → was a click. Let the click handler fire normally.
    _moveState = null;
    return;
  }

  _moveState.block.classList.remove('dragging');
  _moveState.block.style.cursor = 'grab';
  _moveState.readout?.remove();
  // Block the click that fires after mouseup on the same element.
  _suppressClickOnce();

  const dayChanged = _moveState.newDayIdx !== _moveState.originalDayIdx;
  const startChanged = _moveState.newStart !== _moveState.originalStart;
  if (!dayChanged && !startChanged) {
    _moveState = null;
    return;
  }

  _moveState.shift.day = DAYS_FULL[_moveState.newDayIdx];
  _moveState.shift.start_hour = _moveState.newStart;
  _moveState.shift.end_hour = _moveState.newStart + _moveState.duration;
  const configToSave = _moveState.config;
  _moveState = null;

  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(configToSave) });
    await renderSchedule();
  } catch (err) {
    console.warn('[schedule] save after move failed:', err);
    await renderSchedule();
  }
}

// ── Show editor form ────────────────────────────────────────────────────────
// schedulerEditing: null = no form open; '__new__' = new show; otherwise the
// id of the show being edited (UUID string).
let schedulerEditing = null;
let schedulerWorkingShow = null;  // mutable form state for the Show

// Backwards-compat alias for the test infra that reads the working state via
// __getSchedulerWorkingPersona. Same object — just two names so we can rename
// the JS without breaking the harness in lock-step.
function _getSchedulerWorking() { return schedulerWorkingShow; }

async function _openShowEditor(showId) {
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const shows = config.station?.shows || [];

  if (showId === '__new__' || showId === -1 || showId == null) {
    schedulerWorkingShow = {
      id: (globalThis.crypto?.randomUUID?.() ?? `tmp-${Math.random().toString(36).slice(2)}`),
      name: null,
      dj_id: null,           // default DJ slot by default; user can pick
      shifts: [],
    };
    schedulerEditing = '__new__';
  } else {
    const found = shows.find(s => s.id === showId);
    if (!found) return;
    // Deep clone so Cancel returns the original untouched.
    schedulerWorkingShow = JSON.parse(JSON.stringify(found));
    schedulerWorkingShow.shifts = schedulerWorkingShow.shifts || [];
    schedulerEditing = showId;
  }
  // Switch to the edit sub-view BEFORE rendering, so the form's container
  // isn't display:none when _autoResizeTextarea reads scrollHeight (which
  // returns 0 on hidden elements, defeating the resize).
  _setSchedulerSubView('edit');
  await _renderShowForm();
}

async function _renderShowForm() {
  const form = document.getElementById('showForm');
  if (!form || !schedulerWorkingShow) return;

  // Pull the live config so the DJ picker reflects any DJs created via the
  // inline modal in the same session.
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const djs = (config.station?.djs || []).slice().sort((a, b) => a.name.localeCompare(b.name));

  const s = schedulerWorkingShow;
  const isNew = schedulerEditing === '__new__';
  const noShifts = !(s.shifts || []).length;

  const djOption = (dj) =>
    `<option value="${_escapeAttr(dj.id)}"${s.dj_id === dj.id ? ' selected' : ''}>${_escapeText(dj.name)}</option>`;

  form.innerHTML = `
    <div>
      <label for="sf-name">Show name <span class="muted" style="text-transform:none; font-weight:400;">— optional, e.g. "Late Night Sessions"</span></label>
      <input type="text" id="sf-name" value="${_escapeAttr(s.name || '')}" maxlength="50"
             placeholder="Leave blank to use the DJ's name"
             autocomplete="off" data-lpignore="true" data-1p-ignore="true"
             aria-autocomplete="none" />
    </div>
    <div>
      <label for="sf-dj">DJ <span class="muted" style="text-transform:none; font-weight:400;">— who hosts this show</span></label>
      <select id="sf-dj" autocomplete="off" data-lpignore="true" data-1p-ignore="true">
        <option value=""${!s.dj_id ? ' selected' : ''}>(Default DJ)</option>
        ${djs.map(djOption).join('')}
        <option value="__new__">+ Create new DJ…</option>
      </select>
    </div>
    ${noShifts ? `
    <div class="empty-shifts-warning">
      ⚠ No shifts — this show won't air. Add at least one shift below.
    </div>` : ''}
    <div>
      <label>Shifts</label>
      <div id="pf-shifts" class="shifts-list"></div>
      <button type="button" id="pf-add-shift" class="add-shift-btn">+ Add shift</button>
    </div>
    <div class="preview-status" id="pf-preview-status"></div>
    <div class="persona-form-actions">
      <div class="left-group">
        <button type="button" id="pf-save" class="primary">${isNew ? 'Create show' : 'Save changes'}</button>
        <button type="button" id="pf-cancel">Cancel</button>
      </div>
      ${isNew ? '' : '<button type="button" class="delete-btn" id="pf-delete">Delete</button>'}
    </div>
  `;

  _renderShifts();

  form.querySelector('#sf-name').addEventListener('input', (e) => {
    const v = e.target.value.trim();
    schedulerWorkingShow.name = v || null;
  });
  form.querySelector('#sf-dj').addEventListener('change', (e) => {
    const v = e.target.value;
    if (v === '__new__') {
      // Snap the picker back to its current value while the modal is open —
      // if the user cancels, nothing should appear to have changed yet.
      e.target.value = s.dj_id || '';
      _openDJCreateModal();
    } else {
      schedulerWorkingShow.dj_id = v || null;
    }
  });
  form.querySelector('#pf-cancel').addEventListener('click', () => _setSchedulerSubView('grid'));
  form.querySelector('#pf-add-shift').addEventListener('click', () => {
    schedulerWorkingShow.shifts.push({ day: 'monday', start_hour: 9, end_hour: 17 });
    _renderShowForm();  // re-render so the empty-shifts warning disappears
  });

  if (!isNew) {
    form.querySelector('#pf-delete').addEventListener('click', _deleteShow);
  }
  form.querySelector('#pf-save').addEventListener('click', _saveShow);
}

// ── Inline "Create new DJ" modal ─────────────────────────────────────────────
// Triggered from the Show editor's DJ picker. Minimal subset of fields —
// slice 6's full DJ Roster view is the place to set up rich identities. This
// is just enough to keep the Show flow uninterrupted: name, personality,
// voice, voice instructions.
function _openDJCreateModal() {
  const modal = document.getElementById('djCreateModal');
  if (!modal) return;
  modal.classList.add('open');
  modal.innerHTML = `
    <div class="dj-create-modal-inner">
      <h3 style="margin:0 0 10px 0;">Create new DJ</h3>
      <p class="muted" style="margin:0 0 14px 0;">Quick-add a DJ for this show. You can flesh out the rest later in the DJ Roster.</p>
      <div>
        <label for="dj-modal-name">On-air handle</label>
        <input type="text" id="dj-modal-name" required
               autocomplete="off" data-lpignore="true" data-1p-ignore="true"
               aria-autocomplete="none" />
      </div>
      <div>
        <label for="dj-modal-personality">Personality <span class="muted" style="text-transform:none; font-weight:400;">— what they SAY</span></label>
        <textarea id="dj-modal-personality" required autocomplete="off"
                  data-lpignore="true" data-1p-ignore="true"></textarea>
      </div>
      <div>
        <label for="dj-modal-voice">Voice</label>
        <select id="dj-modal-voice" autocomplete="off" data-lpignore="true" data-1p-ignore="true">
          <option value="">(use station default)</option>
          ${OPENAI_VOICES.map(v => `<option value="${v}">${v}</option>`).join('')}
        </select>
      </div>
      <div>
        <label for="dj-modal-voice-instructions">Voice instructions <span class="muted" style="text-transform:none; font-weight:400;">— optional</span></label>
        <textarea id="dj-modal-voice-instructions" autocomplete="off"
                  data-lpignore="true" data-1p-ignore="true"></textarea>
      </div>
      <div class="dj-create-modal-actions">
        <button type="button" id="dj-modal-save" class="primary">Create DJ</button>
        <button type="button" id="dj-modal-cancel">Cancel</button>
      </div>
      <div class="preview-status" id="dj-modal-status"></div>
    </div>
  `;
  modal.querySelector('#dj-modal-cancel').addEventListener('click', _closeDJCreateModal);
  modal.querySelector('#dj-modal-save').addEventListener('click', _saveDJCreate);
  modal.querySelector('#dj-modal-name').focus();
}

function _closeDJCreateModal() {
  const modal = document.getElementById('djCreateModal');
  if (!modal) return;
  modal.classList.remove('open');
  modal.innerHTML = '';
}

async function _saveDJCreate() {
  const name = document.getElementById('dj-modal-name').value.trim();
  const personality = document.getElementById('dj-modal-personality').value.trim();
  const voice = document.getElementById('dj-modal-voice').value || null;
  const voiceInstructions = document.getElementById('dj-modal-voice-instructions').value.trim() || null;
  const status = document.getElementById('dj-modal-status');
  if (!name || !personality) {
    status.textContent = 'Name and personality are required.';
    return;
  }
  const newDJ = {
    id: (globalThis.crypto?.randomUUID?.() ?? `tmp-${Math.random().toString(36).slice(2)}`),
    name,
    personality,
    voice,
    voice_instructions: voiceInstructions,
    prompt_template: null,
  };
  status.textContent = 'Saving…';
  let config;
  try { config = await api('/config'); } catch (_) { status.textContent = 'Could not load config.'; return; }
  config.station = config.station || {};
  config.station.djs = config.station.djs || [];
  config.station.djs.push(newDJ);
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    _refreshDjColourCache(config);  // new DJ → fresh index → fresh colour
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
    return;
  }
  // Pre-select the new DJ in the Show picker and close the modal. We don't
  // need to re-fetch — _renderShowForm pulls a fresh config which includes
  // the just-saved DJ.
  schedulerWorkingShow.dj_id = newDJ.id;
  _closeDJCreateModal();
  await _renderShowForm();
}

// Format an hour boundary 0..24 as "midnight" / "noon" / "Nam" / "Npm".
// Used to translate the bare integer inputs into something a human can read.
function _fmtHourBoundary(h) {
  h = ((h % 24) + 24) % 24;
  if (h === 0) return 'midnight';
  if (h === 12) return 'noon';
  if (h < 12) return `${h}am`;
  return `${h - 12}pm`;
}

// Inclusive-hour shifts: end_hour=23 plays through to the start of the next
// hour (midnight). So we render the boundary as (end_hour + 1).
function _fmtShiftRange(start, end) {
  return `${_fmtHourBoundary(start)} → ${_fmtHourBoundary(end + 1)}`;
}

function _renderShifts() {
  const container = document.getElementById('pf-shifts');
  if (!container) return;
  container.innerHTML = '';

  schedulerWorkingShow.shifts.forEach((shift, i) => {
    const row = document.createElement('div');
    row.className = 'shift-row';
    row.innerHTML = `
      <select data-shift-i="${i}" data-field="day"
              autocomplete="off" data-lpignore="true" data-1p-ignore="true">
        ${DAYS_FULL.map(d => `<option value="${d}"${shift.day === d ? ' selected' : ''}>${d.charAt(0).toUpperCase() + d.slice(1)}</option>`).join('')}
      </select>
      <input type="number" data-shift-i="${i}" data-field="start_hour" min="0" max="23" value="${shift.start_hour}"
             autocomplete="off" data-lpignore="true" data-1p-ignore="true" />
      <input type="number" data-shift-i="${i}" data-field="end_hour" min="0" max="23" value="${shift.end_hour}"
             autocomplete="off" data-lpignore="true" data-1p-ignore="true" />
      <button type="button" class="remove-shift" data-shift-i="${i}">✕</button>
      <span class="shift-readout" data-shift-i="${i}">${_fmtShiftRange(shift.start_hour, shift.end_hour)}</span>
    `;
    container.appendChild(row);
  });

  // Wire up change handlers
  container.querySelectorAll('[data-shift-i]').forEach(el => {
    if (el.classList.contains('remove-shift')) {
      el.addEventListener('click', () => {
        schedulerWorkingShow.shifts.splice(Number(el.dataset.shiftI), 1);
        // Full re-render so the empty-shifts warning reappears once the last
        // shift is gone.
        _renderShowForm();
      });
    } else if (el.tagName === 'SPAN') {
      // Readout — no listeners needed.
    } else {
      const refreshReadout = () => {
        const i = Number(el.dataset.shiftI);
        const s = schedulerWorkingShow.shifts[i];
        const readout = container.querySelector(`span.shift-readout[data-shift-i="${i}"]`);
        if (readout) readout.textContent = _fmtShiftRange(s.start_hour, s.end_hour);
      };
      el.addEventListener('change', () => {
        const i = Number(el.dataset.shiftI);
        const field = el.dataset.field;
        const value = field === 'day' ? el.value : Number(el.value);
        schedulerWorkingShow.shifts[i][field] = value;
        refreshReadout();
      });
      // Live-update on every keystroke too, not just on blur.
      if (el.tagName === 'INPUT') el.addEventListener('input', () => {
        const i = Number(el.dataset.shiftI);
        const field = el.dataset.field;
        const v = Number(el.value);
        if (!Number.isNaN(v)) schedulerWorkingShow.shifts[i][field] = v;
        refreshReadout();
      });
    }
  });
}

function _readFormIntoWorkingShow() {
  // The form inputs write through to schedulerWorkingShow on input/change, so
  // by the time Save fires the state is already current. This is left as a
  // helper for tests/code that want to force-sync without dispatching events.
  const nameEl = document.getElementById('sf-name');
  if (nameEl) {
    const v = nameEl.value.trim();
    schedulerWorkingShow.name = v || null;
  }
  const djEl = document.getElementById('sf-dj');
  if (djEl && djEl.value !== '__new__') {
    schedulerWorkingShow.dj_id = djEl.value || null;
  }
}

async function _saveShow(event) {
  event?.preventDefault?.();
  _readFormIntoWorkingShow();
  const status = document.getElementById('pf-preview-status');

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  config.station = config.station || {};
  config.station.shows = config.station.shows || [];

  if (schedulerEditing === '__new__') {
    config.station.shows.push(schedulerWorkingShow);
  } else {
    const idx = config.station.shows.findIndex(s => s.id === schedulerEditing);
    if (idx === -1) {
      // Edited Show was removed out from under us (concurrent edit elsewhere).
      // Append rather than silently dropping the user's changes.
      config.station.shows.push(schedulerWorkingShow);
    } else {
      config.station.shows[idx] = schedulerWorkingShow;
    }
  }

  status.textContent = 'Saving…';
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    status.textContent = '';
    _setSchedulerSubView('grid');
    await renderSchedule();
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

async function _deleteShow() {
  if (schedulerEditing === '__new__') return;
  const label = schedulerWorkingShow.name
    ? `the show "${schedulerWorkingShow.name}"`
    : 'this show';
  if (!confirm(`Delete ${label}? The DJ identity stays in the roster.`)) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const shows = config.station.shows || [];
  const idx = shows.findIndex(s => s.id === schedulerEditing);
  if (idx !== -1) shows.splice(idx, 1);
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    _setSchedulerSubView('grid');
    await renderSchedule();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

function _escapeAttr(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function _escapeText(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── DJ Roster sidebar takeover ──────────────────────────────────────────────
// Parallel to the Schedule and Settings views: a sidebar mode dedicated to
// editing DJ identities (name, personality, voice, instructions). The Show
// editor handles scheduling; this view handles "who is the DJ". A DJ can be
// referenced by many Shows, so editing identity here is the one place that
// reflects everywhere.

let rosterEditing = null;        // null = no form; '__new__' = new; otherwise dj.id
let rosterWorkingDJ = null;      // mutable form state

// DJ avatar cache-busting. The /media/dj-icon/{id} URL is stable across
// regenerations (we always overwrite the same file), so without a query
// string the browser would happily serve a stale image after regenerate.
// Each page load gets a fresh baseline timestamp; regenerating a specific
// DJ replaces just that DJ's entry so unrelated avatars don't refetch.
const _DJ_AVATAR_PAGE_TS = Date.now();
const _djAvatarTs = new Map();   // dj_id -> server-side generated_at (integer seconds)
function _djAvatarUrl(djId) {
  const ts = _djAvatarTs.get(djId) ?? _DJ_AVATAR_PAGE_TS;
  return `/media/dj-icon/${djId}?v=${ts}`;
}

// DJ palette-colour lookup. Populated lazily from /config so the on-air
// badge avatar's placeholder circle picks up the same colour the DJ uses on
// the schedule grid + roster row — otherwise a brief flash of pink during
// the image load looks disconnected. Refreshed on init() and after every
// PUT /config (settings save / DJ create / DJ edit).
const _djColourById = new Map();
function _refreshDjColourCache(config) {
  _djColourById.clear();
  const djs = config?.station?.djs || [];
  djs.forEach((dj, i) => _djColourById.set(dj.id, _personaColor(i)));
}
function _djColourFor(djId) {
  // Fallback for the brief window before init's /config arrives, or for
  // genuinely-unknown ids (deleted DJs). Slate matches the existing
  // default-block treatment on the schedule grid.
  return _djColourById.get(djId) ?? '#334155';
}

function _setRosterMode(on) {
  const wrap = document.getElementById('wrap');
  if (!wrap) return;
  wrap.dataset.mode = on ? 'roster' : 'default';
  if (on) {
    _setRosterSubView('list');
    _renderRosterList();
  }
}

function _setRosterSubView(name) {
  const panel = document.querySelector('.sidebar-roster');
  if (panel) panel.dataset.subView = name;
}

async function _openRoster() {
  _setRosterMode(true);
}

// Format a single shift as "Monday 9am – noon" for editor/delete-confirmation
// readouts. Re-uses _fmtShiftRange so the formatting matches the schedule's.
function _fmtShiftReadable(shift) {
  const raw = String(shift.day || '');
  const day = raw.charAt(0).toUpperCase() + raw.slice(1);
  return `${day} ${_fmtShiftRange(Number(shift.start_hour), Number(shift.end_hour))}`;
}

async function _renderRosterList() {
  const container = document.getElementById('rosterList');
  if (!container) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const djs   = config.station?.djs   || [];
  const shows = config.station?.shows || [];

  container.innerHTML = '';

  if (djs.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.style.fontSize = '0.85rem';
    empty.textContent = 'No DJs yet. Click "+ New DJ" to add one — they\'ll show up here and become pickable in the Show editor.';
    container.appendChild(empty);
    return;
  }

  // Each DJ's row is colour-coded by its position in the *original* djs[]
  // ordering — the same key the schedule grid uses to colour blocks — so the
  // chip-on-the-row matches the block colour at a glance.
  const colourByIdx = new Map(djs.map((d, i) => [d.id, _personaColor(i)]));
  // Display order: alphabetical. Predictable as the roster grows.
  const sorted = djs.slice().sort((a, b) => a.name.localeCompare(b.name));

  sorted.forEach((dj) => {
    const hosted = shows.filter(s => s.dj_id === dj.id);
    const orphan = hosted.length === 0;
    const row = document.createElement('div');
    row.className = 'roster-row';
    row.dataset.djId = dj.id;

    // Two-column row: 60 px avatar on the left, content stack on the right.
    // The avatar is sized larger than the original 28 px swatch because it's
    // doing more visual work now (showing actual generated portraits, not
    // just colour-coding), so the row is built as flex-row with the text
    // stack vertically centred against the avatar to keep the eye balanced.
    // Avatar still has the DJ's grid-block colour as its background so it
    // gracefully falls back to a coloured circle if no avatar's been
    // generated — the <img> self-removes via onerror.
    const avatar = document.createElement('span');
    avatar.className = 'dj-avatar dj-avatar-md';
    avatar.style.background = colourByIdx.get(dj.id);
    avatar.innerHTML = `<img src="${_djAvatarUrl(dj.id)}" alt="" onerror="this.remove()" />`;
    row.appendChild(avatar);

    const content = document.createElement('div');
    content.className = 'roster-row-content';

    const nameLine = document.createElement('div');
    nameLine.className = 'roster-row-name';
    nameLine.textContent = dj.name;
    content.appendChild(nameLine);

    if (dj.personality) {
      const p = document.createElement('div');
      p.className = 'roster-row-personality';
      p.textContent = dj.personality;
      content.appendChild(p);
    }

    const meta = document.createElement('div');
    meta.className = 'roster-row-meta';
    const usesLabel = hosted.length === 1 ? '1 show' : `${hosted.length} shows`;
    meta.innerHTML =
      `<span>${dj.voice ? `Voice: ${_escapeText(dj.voice)}` : 'Voice: (default)'}</span>` +
      `<span>Used in ${usesLabel}</span>` +
      (orphan ? `<span class="orphan">⚠ not in any show</span>` : '');
    content.appendChild(meta);

    row.appendChild(content);
    row.addEventListener('click', () => _openDJEditor(dj.id));
    container.appendChild(row);
  });
}

async function _openDJEditor(djId) {
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const djs = config.station?.djs || [];

  if (djId === '__new__') {
    rosterWorkingDJ = {
      id: (globalThis.crypto?.randomUUID?.() ?? `tmp-${Math.random().toString(36).slice(2)}`),
      name: '',
      personality: '',
      voice: null,
      voice_instructions: null,
      prompt_template: null,
    };
    rosterEditing = '__new__';
  } else {
    const found = djs.find(d => d.id === djId);
    if (!found) return;
    rosterWorkingDJ = JSON.parse(JSON.stringify(found));
    rosterEditing = djId;
  }
  _setRosterSubView('edit');
  await _renderDJEditorForm();
}

async function _renderDJEditorForm() {
  const form = document.getElementById('djEditorForm');
  if (!form || !rosterWorkingDJ) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const shows = config.station?.shows || [];
  const dj = rosterWorkingDJ;
  const isNew = rosterEditing === '__new__';
  const hosted = isNew ? [] : shows.filter(s => s.dj_id === dj.id);
  const previewSample = dj.name
    ? `Hi, you're listening to ${dj.name} on RadioDunc.`
    : `Hi, you're listening to RadioDunc.`;

  // Avatar palette colour: pick by the DJ's index in the *original* djs[]
  // ordering (same key the schedule grid + roster row use) so the placeholder
  // circle visually agrees with everywhere else this DJ appears.
  const djIdx = (config.station?.djs || []).findIndex(d => d.id === dj.id);
  const avatarColour = djIdx >= 0 ? _personaColor(djIdx) : '#334155';

  form.innerHTML = `
    <div class="dj-editor-avatar-row">
      <span class="dj-avatar dj-avatar-lg" style="background:${avatarColour};">
        ${isNew ? '' : `<img src="${_djAvatarUrl(dj.id)}" alt="" onerror="this.remove()" />`}
      </span>
      <div class="dj-editor-avatar-actions">
        <button type="button" id="de-regen-avatar"${isNew ? ' disabled' : ''}>
          ↻ Regenerate avatar
        </button>
        <div class="dj-editor-avatar-hint">
          ${isNew
            ? 'Save first, then come back to generate an avatar.'
            : 'Stylised portrait, ~$0.01 per generation. Takes 5–15 s.'}
        </div>
      </div>
    </div>
    <div>
      <label for="de-name">On-air handle <span class="muted" style="text-transform:none; font-weight:400;">— the name the DJ goes by</span></label>
      <input type="text" id="de-name" value="${_escapeAttr(dj.name)}" required
             autocomplete="off" data-lpignore="true" data-1p-ignore="true"
             aria-autocomplete="none" />
    </div>
    <div>
      <label for="de-personality">Personality <span class="muted" style="text-transform:none; font-weight:400;">— what they SAY: attitude, slang, vibe</span></label>
      <textarea id="de-personality" required autocomplete="off"
                data-lpignore="true" data-1p-ignore="true">${_escapeText(dj.personality)}</textarea>
    </div>
    <div>
      <label for="de-voice">Voice</label>
      <div class="voice-row">
        <select id="de-voice" autocomplete="off" data-lpignore="true" data-1p-ignore="true">
          <option value="">(use station default)</option>
          ${OPENAI_VOICES.map(v => `<option value="${v}"${dj.voice === v ? ' selected' : ''}>${v}</option>`).join('')}
        </select>
        <button type="button" class="preview-btn" id="de-preview-btn">▶ Preview</button>
      </div>
    </div>
    <div>
      <label for="de-vi">Voice instructions <span class="muted" style="text-transform:none; font-weight:400;">— how they should sound (pacing, tone, accent…)</span></label>
      <textarea id="de-vi" autocomplete="off"
                data-lpignore="true" data-1p-ignore="true">${_escapeText(dj.voice_instructions || '')}</textarea>
    </div>
    <div class="preview-status" id="de-preview-status"></div>
    <div class="persona-form-actions">
      <div class="left-group">
        <button type="button" id="de-save" class="primary">${isNew ? 'Create DJ' : 'Save changes'}</button>
        <button type="button" id="de-cancel">Cancel</button>
      </div>
      ${isNew ? '' : '<button type="button" class="delete-btn" id="de-delete">Delete</button>'}
    </div>
    ${isNew ? '' : `
    <div class="dj-editor-uses">
      <h4>Used in ${hosted.length === 1 ? '1 show' : `${hosted.length} shows`}</h4>
      ${hosted.length === 0
        ? '<p class="no-uses">⚠ Not currently in any show — go to the schedule to put them on the air.</p>'
        : `<ul>${hosted.map(s => {
            const name = s.name ? `<span class="show-name">${_escapeText(s.name)}</span> — ` : '';
            const ranges = (s.shifts || []).length
              ? (s.shifts || []).map(_fmtShiftReadable).join('; ')
              : '<span style="color:#fbbf24;">no shifts</span>';
            return `<li>${name}${ranges}</li>`;
          }).join('')}</ul>`
      }
    </div>`}
  `;

  _autoResizeTextarea(form.querySelector('#de-personality'));
  _autoResizeTextarea(form.querySelector('#de-vi'));

  form.querySelector('#de-cancel').addEventListener('click', () => _setRosterSubView('list'));
  form.querySelector('#de-preview-btn').addEventListener('click', () => _previewDJVoice(previewSample));
  form.querySelector('#de-save').addEventListener('click', _saveDJEdit);
  if (!isNew) form.querySelector('#de-delete').addEventListener('click', _deleteDJ);
  if (!isNew) form.querySelector('#de-regen-avatar').addEventListener('click', _regenerateDJAvatar);
}

async function _regenerateDJAvatar() {
  if (rosterEditing === '__new__' || !rosterWorkingDJ) return;
  const btn = document.getElementById('de-regen-avatar');
  const status = document.getElementById('de-preview-status');
  btn.disabled = true;
  status.textContent = 'Generating avatar… (5–15 s)';
  try {
    const resp = await api(`/djs/${rosterWorkingDJ.id}/avatar`, { method: 'POST' });
    // Update the cache-bust key so the next render of THIS DJ's avatar URL
    // pulls the freshly-written file instead of any browser-cached copy at
    // the previous URL. Other DJs' avatars keep their existing cache key
    // (no avoidable refetches).
    _djAvatarTs.set(rosterWorkingDJ.id, resp.generated_at);
    // Re-render the form so the new <img> uses the fresh URL. The Roster
    // list will pick up the new URL the next time it re-renders (e.g. on
    // Cancel back to list, or after Save). The re-render replaces the
    // status element wholesale, so set the success message AFTER it.
    await _renderDJEditorForm();
    document.getElementById('de-preview-status').textContent = 'Avatar updated.';
  } catch (err) {
    status.textContent = `Avatar generation failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

function _readFormIntoWorkingDJ() {
  const dj = rosterWorkingDJ;
  dj.name = document.getElementById('de-name').value.trim();
  dj.personality = document.getElementById('de-personality').value.trim();
  const v = document.getElementById('de-voice').value;
  dj.voice = v || null;
  const vi = document.getElementById('de-vi').value.trim();
  dj.voice_instructions = vi || null;
}

async function _previewDJVoice(sampleText) {
  _readFormIntoWorkingDJ();
  const btn = document.getElementById('de-preview-btn');
  const status = document.getElementById('de-preview-status');
  btn.disabled = true;
  status.textContent = 'Synthesizing preview…';
  try {
    const resp = await api('/tts/preview', {
      method: 'POST',
      body: JSON.stringify({
        text: sampleText,
        voice: rosterWorkingDJ.voice,
        voice_instructions: rosterWorkingDJ.voice_instructions,
      }),
    });
    const audio = new Audio(resp.clip_url);
    status.textContent = 'Playing…';
    audio.onended = () => { status.textContent = ''; };
    audio.onerror = () => { status.textContent = 'Playback failed.'; };
    await audio.play();
  } catch (err) {
    status.textContent = `Preview failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function _saveDJEdit(event) {
  event?.preventDefault?.();
  _readFormIntoWorkingDJ();
  const status = document.getElementById('de-preview-status');

  if (!rosterWorkingDJ.name || !rosterWorkingDJ.personality) {
    status.textContent = 'Name and personality are required.';
    return;
  }

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  config.station = config.station || {};
  config.station.djs = config.station.djs || [];

  if (rosterEditing === '__new__') {
    config.station.djs.push(rosterWorkingDJ);
  } else {
    const idx = config.station.djs.findIndex(d => d.id === rosterEditing);
    if (idx === -1) {
      // Defensive: DJ was deleted out from under us. Append rather than
      // silently dropping the user's edits.
      config.station.djs.push(rosterWorkingDJ);
    } else {
      config.station.djs[idx] = rosterWorkingDJ;
    }
  }

  status.textContent = 'Saving…';
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    // djs[] mutated → refresh the colour cache so the on-air badge
    // (and anything else that reads _djColourFor) picks up the new ordering.
    _refreshDjColourCache(config);
    status.textContent = '';
    _setRosterSubView('list');
    await _renderRosterList();
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

async function _deleteDJ() {
  if (rosterEditing === '__new__') return;
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const djs   = config.station?.djs   || [];
  const shows = config.station?.shows || [];
  const dj = djs.find(d => d.id === rosterEditing);
  if (!dj) return;
  const affected = shows.filter(s => s.dj_id === dj.id);

  // Confirmation lists shows that'll be reassigned. We don't auto-delete
  // those Shows — they fall through to the Default DJ slot (dj_id=null), so
  // the listener still gets a broadcast in those slots, just hosted by the
  // station's own DJ instead of the deleted one.
  let promptText = `Delete DJ "${dj.name}"?`;
  if (affected.length) {
    const list = affected.map(s => {
      const label = s.name ? `${s.name}` : '(unnamed show)';
      const ranges = (s.shifts || []).map(_fmtShiftReadable).join('; ') || 'no shifts';
      return `  • ${label} — ${ranges}`;
    }).join('\n');
    promptText = `Delete DJ "${dj.name}"?\n\n` +
                 `${affected.length === 1 ? 'This show is' : `These ${affected.length} shows are`} hosted by them — they'll be reassigned to the Default DJ:\n${list}`;
  }
  if (!confirm(promptText)) return;

  // Reassign shows to Default DJ, then drop the DJ row.
  affected.forEach(s => { s.dj_id = null; });
  const idx = djs.findIndex(d => d.id === dj.id);
  if (idx !== -1) djs.splice(idx, 1);

  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    _refreshDjColourCache(config);  // djs[] shrank → indices shift → colour map stale
    _setRosterSubView('list');
    await _renderRosterList();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

// ── Station settings sidebar ────────────────────────────────────────────────
// Mirrors the scheduler takeover: sidebar expands, we render a grouped form
// for the most-tweaked AppConfig fields. The form deliberately omits fields
// already covered elsewhere (dj_roster — schedule editor) and the structured
// voice pools (alerts.news.voices, alerts.ads.voices — those need their own
// editor and are still json-only for now). All other top-level config values
// pass through unchanged on save.

// Read the form values back onto a (deep-cloned) copy of the live config and
// PUT it. Kept as a free function so unit tests can exercise it without going
// through the DOM-bound _saveSettings.
function _applySettingsForm(config, formEl) {
  const get   = (id) => formEl.querySelector(`#${id}`);
  const text  = (id) => get(id).value.trim();
  const opt   = (id) => text(id) || null;            // empty → null
  const num   = (id) => { const v = Number(get(id).value); return Number.isFinite(v) ? v : null; };
  const numOr = (id, fallback) => { const v = num(id); return v === null ? fallback : v; };
  const check = (id) => get(id).checked;
  const csv   = (id) => text(id).split(',').map(s => s.trim()).filter(Boolean);

  const station = config.station = config.station || {};
  station.name           = text('s-name') || station.name;
  station.spoken_name    = opt('s-spoken-name');
  station.tagline        = text('s-tagline') || station.tagline;
  station.format         = text('s-format') || station.format;
  station.description    = opt('s-description');
  station.era            = opt('s-era');
  station.genre_focus    = csv('s-genre-focus');

  const alerts = config.alerts = config.alerts || {};
  alerts.weather_location  = text('s-weather-location') || alerts.weather_location;
  alerts.weather_latitude  = get('s-weather-lat').value === '' ? null : numOr('s-weather-lat', null);
  alerts.weather_longitude = get('s-weather-lon').value === '' ? null : numOr('s-weather-lon', null);

  alerts.weather = alerts.weather || {};
  alerts.weather.enabled        = check('s-weather-enabled');
  alerts.weather.every_n_breaks = numOr('s-weather-cadence', 0);

  alerts.news = alerts.news || {};
  alerts.news.enabled        = check('s-news-enabled');
  alerts.news.rss_url        = text('s-news-rss') || alerts.news.rss_url;
  alerts.news.headline_count = numOr('s-news-count', 3);
  alerts.news.every_n_breaks = numOr('s-news-cadence', 0);

  alerts.ads = alerts.ads || {};
  alerts.ads.enabled        = check('s-ads-enabled');
  alerts.ads.every_n_breaks = numOr('s-ads-cadence', 0);
  alerts.ads.pool_size      = numOr('s-ads-pool', 100);
  alerts.ads.risque_chance  = numOr('s-ads-risque', 0.1);

  alerts.station_id = alerts.station_id || {};
  alerts.station_id.enabled      = check('s-sid-enabled');
  alerts.station_id.phrase_count = numOr('s-sid-count', 40);

  config.openai_text_temperature = numOr('s-temperature', 1.2);
  return config;
}

function _renderSettingsForm(config) {
  const form = document.getElementById('settingsForm');
  if (!form) return;
  const s = config.station   || {};
  const a = config.alerts    || {};
  const w = a.weather        || {};
  const n = a.news           || {};
  const ad = a.ads           || {};
  const sid = a.station_id   || {};

  // Helpers to keep the template scannable.
  const textField = (id, label, value, hint = '', { type = 'text', step, min, max } = {}) => `
    <div class="settings-field">
      <label for="${id}">${label}</label>
      <input type="${type}" id="${id}" value="${_escapeAttr(value ?? '')}"
             ${step != null ? `step="${step}"` : ''} ${min != null ? `min="${min}"` : ''} ${max != null ? `max="${max}"` : ''}
             autocomplete="off" data-lpignore="true" data-1p-ignore="true" />
      ${hint ? `<div class="field-hint">${hint}</div>` : ''}
    </div>`;
  const numField = (id, label, value, hint = '', { step = 1, min, max } = {}) =>
    textField(id, label, value, hint, { type: 'number', step, min, max });
  const checkField = (id, label, checked, hint = '') => `
    <div class="settings-field">
      <div class="settings-field-checkbox">
        <input type="checkbox" id="${id}" ${checked ? 'checked' : ''} />
        <label for="${id}">${label}</label>
      </div>
      ${hint ? `<div class="field-hint">${hint}</div>` : ''}
    </div>`;

  form.innerHTML = `
    <details class="settings-section" open>
      <summary>Station identity</summary>
      <div class="settings-section-body">
        ${textField('s-name', 'Name', s.name, 'Shown on screen and in DJ banter.')}
        ${textField('s-spoken-name', 'Spoken name', s.spoken_name, 'Phonetic form for TTS. Leave blank to use the name as-is. Example: "Radio Dunk, one oh seven point two F M".')}
        ${textField('s-tagline', 'Tagline', s.tagline)}
        ${textField('s-format', 'Format', s.format, 'e.g. "Eclectic", "Indie rock", "Late-night chill".')}
        <div class="settings-field">
          <label for="s-description">Description</label>
          <textarea id="s-description" autocomplete="off" data-lpignore="true" data-1p-ignore="true">${_escapeText(s.description || '')}</textarea>
        </div>
        ${textField('s-era', 'Era', s.era, 'Optional. e.g. "90s", "2010s indie".')}
        ${textField('s-genre-focus', 'Genre focus', (s.genre_focus || []).join(', '), 'Comma-separated.')}
      </div>
    </details>

    <details class="settings-section">
      <summary>Weather</summary>
      <div class="settings-section-body">
        ${checkField('s-weather-enabled', 'Enable weather reports', w.enabled !== false)}
        ${textField('s-weather-location', 'Location name', a.weather_location, 'Spoken in the DJ&rsquo;s weather mention.')}
        <div class="settings-row">
          ${numField('s-weather-lat', 'Latitude', a.weather_latitude, '', { step: 0.0001 })}
          ${numField('s-weather-lon', 'Longitude', a.weather_longitude, '', { step: 0.0001 })}
        </div>
        ${numField('s-weather-cadence', 'Every N breaks', w.every_n_breaks ?? 4, '0 = never. 1 = every break. 4 = roughly every 12–20 min.', { min: 0 })}
      </div>
    </details>

    <details class="settings-section">
      <summary>News</summary>
      <div class="settings-section-body">
        ${checkField('s-news-enabled', 'Enable news bulletins', n.enabled !== false)}
        ${textField('s-news-rss', 'RSS feed URL', n.rss_url)}
        ${numField('s-news-count', 'Headlines per bulletin', n.headline_count ?? 3, '', { min: 1, max: 10 })}
        ${numField('s-news-cadence', 'Every N breaks', n.every_n_breaks ?? 5, '0 = never.', { min: 0 })}
      </div>
    </details>

    <details class="settings-section">
      <summary>Ad breaks</summary>
      <div class="settings-section-body">
        ${checkField('s-ads-enabled', 'Enable ad breaks', ad.enabled === true)}
        ${numField('s-ads-cadence', 'Every N breaks', ad.every_n_breaks ?? 6, '0 = never. 1 = every break.', { min: 0 })}
        ${numField('s-ads-pool', 'Clip pool size', ad.pool_size ?? 100, 'Once this many unique ad clips are cached, reuse them randomly.', { min: 1 })}
        ${numField('s-ads-risque', 'Risqué chance', ad.risque_chance ?? 0.1, '0.0–1.0. Probability that the LLM is told to lean suggestive on this ad.', { step: 0.05, min: 0, max: 1 })}
      </div>
    </details>

    <details class="settings-section">
      <summary>Station IDs</summary>
      <div class="settings-section-body">
        ${checkField('s-sid-enabled', 'Enable station ID stingers', sid.enabled !== false)}
        ${numField('s-sid-count', 'Phrase pool size', sid.phrase_count ?? 40, 'How many varied "This is RadioDunc…" stingers to generate per station name. Rounded up to a multiple of 5.', { min: 5, max: 100 })}
      </div>
    </details>

    <details class="settings-section">
      <summary>AI generation</summary>
      <div class="settings-section-body">
        ${numField('s-temperature', 'Text temperature', config.openai_text_temperature ?? 1.2, '0.0–2.0. Higher = more variety in DJ banter and stingers. News bulletins override this lower internally.', { step: 0.1, min: 0, max: 2 })}
      </div>
    </details>

    <div class="settings-actions">
      <button type="button" id="s-save" class="primary">Save changes</button>
      <span class="status" id="s-status"></span>
    </div>
  `;

  form.querySelector('#s-save').addEventListener('click', _saveSettings);
}

let _settingsLiveConfig = null;  // last-fetched config, mutated on save

async function _openSettings() {
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  _settingsLiveConfig = config;
  _renderSettingsForm(config);
  _setSettingsMode(true);
}

function _setSettingsMode(on) {
  const wrap = document.getElementById('wrap');
  if (!wrap) return;
  wrap.dataset.mode = on ? 'settings' : 'default';
}

async function _saveSettings() {
  if (!_settingsLiveConfig) return;
  const form = document.getElementById('settingsForm');
  const status = document.getElementById('s-status');
  // Deep-clone so a save failure doesn't leave the in-memory config half-mutated.
  const next = JSON.parse(JSON.stringify(_settingsLiveConfig));
  _applySettingsForm(next, form);
  status.textContent = 'Saving…';
  try {
    const saved = await api('/config', { method: 'PUT', body: JSON.stringify(next) });
    _settingsLiveConfig = saved;
    status.textContent = 'Saved.';
    // Refresh the top-of-page name/tagline if the station identity changed.
    document.getElementById('stationName').textContent = saved.station?.name || 'RadioDunc';
    document.getElementById('stationMeta').textContent = saved.station?.tagline || '';
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

// ── Server state refresh ──────────────────────────────────────────────────────
let refreshInFlight = null;
async function refreshServerState() {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = api('/player/status')
    .then(s => { serverState = s; renderAll(); })
    .catch(() => {})
    .finally(() => { refreshInFlight = null; });
  return refreshInFlight;
}

// ── Event bindings ────────────────────────────────────────────────────────────
document.getElementById('scanBtn').addEventListener('click', async () => {
  const status     = document.getElementById('scanStatus');
  const folderPath = document.getElementById('libraryPath').value;
  status.textContent = 'Scanning…';
  try {
    const result = await api('/library/scan', {
      method: 'POST',
      body: JSON.stringify({ folder_path: folderPath }),
    });
    status.textContent = `Scanned ${result.scanned} files, added ${result.inserted}, updated ${result.updated}.`;
    refreshLibraryStatus();
  } catch (err) {
    status.textContent = `Scan failed: ${err.message}`;
  }
});

document.getElementById('playBtn').addEventListener('click', async () => {
  const status = document.getElementById('scanStatus');
  status.textContent = '';
  try {
    if (serverState?.is_playing && !ctx) {
      // Server has an active queue but the page was refreshed — resume current track.
      await resumeAfterRefresh();
    } else if (serverState?.is_playing && !paused) {
      await pausePlayback();
    } else if (serverState?.is_playing && paused) {
      await resumePlayback();
    } else {
      await startPlayback();
    }
  } catch (err) {
    status.textContent = `Play failed: ${err.message}`;
  }
});

document.getElementById('nextBtn').addEventListener('click', () => {
  if (!ctx || transitioning) return;
  const btn = document.getElementById('nextBtn');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  // Clear pause state — triggerTransition calls ctx.resume() and rebuilds timers.
  paused = false;
  _autoTriggerRemaining = null;
  triggerTransition('user').finally(() => {
    btn.disabled = false;
    btn.textContent = 'Next';
  });
});

document.getElementById('stopBtn').addEventListener('click', () => stopPlayback());

document.getElementById('volume').addEventListener('input', async (e) => {
  const vol = Number(e.target.value);
  // Update gain immediately — no round-trip latency.
  if (masterGain) masterGain.gain.value = vol / 100;
  localStorage.setItem('volume', vol);
  // Persist to server (best effort; we don't block on it).
  try {
    const resp = await api('/player/state', { method: 'PUT', body: JSON.stringify({ volume: vol }) });
    serverState = resp;
  } catch (_) {}
});

document.addEventListener('visibilitychange', () => {
  _logPlayback('visibilitychange', { visibilityState: document.visibilityState });
  if (document.visibilityState === 'visible' && serverState?.is_playing) refreshServerState();
});

// ── Track request search ──────────────────────────────────────────────────────
let _searchTimer = null;

function _renderSearchResults(tracks) {
  const list = document.getElementById('searchResults');
  list.innerHTML = '';
  tracks.forEach(track => {
    const label = `${track.artist || 'Unknown'} — ${track.title || 'Untitled'}`;
    const li = document.createElement('li');
    li.style.cssText = 'display:flex; align-items:center; justify-content:space-between; padding:4px 0; border-bottom:1px solid #eee;';
    const span = document.createElement('span');
    span.textContent = label;
    const btn = document.createElement('button');
    btn.textContent = 'Add next';
    btn.style.cssText = 'margin-left:8px; padding:2px 8px; font-size:0.85em;';
    btn.onclick = async () => {
      try {
        await api('/player/queue/inject', { method: 'POST', body: JSON.stringify({ track_id: track.id }) });
        document.getElementById('trackSearch').value = '';
        document.getElementById('searchResults').innerHTML = '';
        serverState = await api('/player/status');
        renderAll();
      } catch (err) {
        alert(`Could not add track: ${err.message}`);
      }
    };
    li.appendChild(span);
    li.appendChild(btn);
    list.appendChild(li);
  });
}

document.getElementById('trackSearch').addEventListener('input', (e) => {
  clearTimeout(_searchTimer);
  const q = e.target.value.trim();
  if (!q) { document.getElementById('searchResults').innerHTML = ''; return; }
  _searchTimer = setTimeout(async () => {
    try {
      const results = await api(`/library/search?q=${encodeURIComponent(q)}`);
      _renderSearchResults(results);
    } catch (_) {}
  }, 300);
});

// ── Library status ────────────────────────────────────────────────────────────
async function refreshLibraryStatus() {
  try {
    const data = await api('/library/status');
    document.getElementById('libTrackCount').textContent = `${data.track_count} tracks`;
    document.getElementById('libLastScan').textContent = data.last_scan_at
      ? `Last scan: ${new Date(data.last_scan_at).toLocaleString()}`
      : '';
  } catch (_) {}
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  // Pre-set slider from localStorage so it doesn't jump when serverState arrives.
  const savedVol = localStorage.getItem('volume');
  if (savedVol !== null) document.getElementById('volume').value = savedVol;

  const config = await api('/config');
  document.getElementById('libraryPath').value = config.music_folder || '';
  _refreshDjColourCache(config);
  serverState = await api('/player/status');
  renderAll();
  // Light polling — client drives playback now, so we don't need frequent syncs.
  setInterval(refreshServerState, 10_000);
  refreshLibraryStatus();

  // Scheduler sidebar takeover: open/close + view-switching wiring.
  document.getElementById('openSchedulerBtn')?.addEventListener('click', () => _setSchedulerMode(true));
  document.getElementById('closeSchedulerBtn')?.addEventListener('click', () => _setSchedulerMode(false));
  document.getElementById('backToGridBtn')?.addEventListener('click', () => _setSchedulerSubView('grid'));
  document.getElementById('addShowBtn')?.addEventListener('click', () => _openShowEditor('__new__'));

  // Settings sidebar takeover.
  document.getElementById('openSettingsBtn')?.addEventListener('click', _openSettings);
  document.getElementById('closeSettingsBtn')?.addEventListener('click', () => _setSettingsMode(false));

  // DJ Roster sidebar takeover.
  document.getElementById('openRosterBtn')?.addEventListener('click', _openRoster);
  document.getElementById('closeRosterBtn')?.addEventListener('click', () => _setRosterMode(false));
  document.getElementById('backToRosterBtn')?.addEventListener('click', () => _setRosterSubView('list'));
  document.getElementById('addDjBtn')?.addEventListener('click', () => _openDJEditor('__new__'));

  // Esc closes whichever sidebar takeover is active.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const mode = document.getElementById('wrap')?.dataset.mode;
    if (mode === 'scheduler') {
      if (document.querySelector('.sidebar-scheduler')?.dataset.subView === 'edit') {
        _setSchedulerSubView('grid');
      } else {
        _setSchedulerMode(false);
      }
    } else if (mode === 'settings') {
      _setSettingsMode(false);
    } else if (mode === 'roster') {
      if (document.querySelector('.sidebar-roster')?.dataset.subView === 'edit') {
        _setRosterSubView('list');
      } else {
        _setRosterMode(false);
      }
    }
  });

  _scheduleAutoRefresh();
}

init();

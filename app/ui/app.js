'use strict';

// ── Timing constants ─────────────────────────────────────────────────────────
// Adjust these to taste; all audio scheduling uses AudioContext time (sample-accurate).
const FADE_OUT_S     = 9.0;  // current track fades 1→0 over this many seconds
const FADE_IN_S      = 1.2;  // next track fades 0→1 over this many seconds
const DJ_GAIN        = 1.8;  // DJ clip peak gain (>1 boosts it above the fading music)
const DJ_EDGE_S      = 0.2;  // DJ clip's own tiny in/out fade
const AUTO_PREROLL_S = 10;   // start transition this many seconds before track end

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
  let text;
  if (mode === 'ad')        text = '📻 Ad break';
  else if (mode === 'news') text = '📰 News';
  else if (mode === 'dj')   text = '🎙️ On air';
  else text = label || serverState?.now_playing_label || el.textContent || '-';
  animateLabel(el, text);
}

async function animateLabel(el, newText) {
  if (el.textContent === newText) return;

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

  el.textContent = newText;

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
  masterGain.connect(ctx.destination);

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
// fadeIn/fadeOut override DJ_EDGE_S per segment (use a tiny value for short clips
// whose first/last consonant would otherwise be eaten by the default fade).
// Returns the end time so callers can chain segments back-to-back.
function scheduleSegment(buf, startAt, label, { fadeIn = DJ_EDGE_S, fadeOut = DJ_EDGE_S } = {}) {
  // Clamp so fade-in and fade-out can never overlap on a very short clip.
  const inS  = Math.min(fadeIn,  buf.duration / 2);
  const outS = Math.min(fadeOut, buf.duration / 2);

  const src  = ctx.createBufferSource();
  src.buffer = buf;
  const g    = ctx.createGain();
  src.connect(g);
  g.connect(masterGain);
  g.gain.setValueAtTime(0, startAt);
  g.gain.linearRampToValueAtTime(DJ_GAIN, startAt + inS);
  g.gain.setValueAtTime(DJ_GAIN, startAt + buf.duration - outS);
  g.gain.linearRampToValueAtTime(0, startAt + buf.duration);
  src.start(startAt);
  console.log(`[audio] ${label}: start=${startAt.toFixed(3)} dur=${buf.duration.toFixed(2)} in=${inS.toFixed(2)}`);
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
  autoTriggerTimer = setTimeout(() => triggerTransition('auto'), delaySec * 1000);
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
    scheduleSegment(buf, start, 'Skip stinger', { fadeIn: STINGER_FADE_S, fadeOut: STINGER_FADE_S });
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
  if (transitioning) {
    console.log('[audio] transition suppressed (already in progress)');
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

    if (djBuf)   { djEnd = scheduleSegment(djBuf, djStart, 'DJ clip');     cursor = djEnd; }
    if (newsBuf) { newsStart = cursor + 0.1; cursor = scheduleSegment(newsBuf, newsStart, 'News clip'); }
    if (adBuf)   { adStart   = cursor + 0.1; cursor = scheduleSegment(adBuf,   adStart,   'Ad clip'); }
    if (sidBuf)  { sidStart  = cursor + 0.1; cursor = scheduleSegment(sidBuf,  sidStart,  'Station ID', { fadeIn: STINGER_FADE_S, fadeOut: STINGER_FADE_S }); }
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
  paused = false;
  _autoTriggerRemaining = null;
  initAudio();
  await ctx.resume();
  serverState = await api('/player/status');
  await _playCurrentTrackFromServer();
}

// ── Stop playback ─────────────────────────────────────────────────────────────
async function stopPlayback() {
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

function _appendPersonaBlock(grid, persona, color, col, rowStart, rowEndExclusive, meta = {}) {
  const block = document.createElement('div');
  block.className = 'grid-persona-block';
  block.style.gridColumn = String(col);
  block.style.gridRow = `${rowStart} / ${rowEndExclusive}`;
  block.style.backgroundColor = color;
  block.title = `${persona.name} — ${persona.style}`;
  // Show the name only in the first block of a stack; for very short shifts,
  // a 1-letter monogram avoids overflow.
  const span = (rowEndExclusive - rowStart) >= 2 ? persona.name : persona.name.slice(0, 1);
  block.textContent = span;
  // Tag with indices so drag/click handlers can find their backing data.
  if (meta.personaIdx != null) block.dataset.personaIdx = String(meta.personaIdx);
  if (meta.shiftIdx   != null) block.dataset.shiftIdx   = String(meta.shiftIdx);
  if (meta.isWrap)              block.dataset.isWrap     = '1';
  if (meta.wrapHalf)            block.dataset.wrapHalf   = meta.wrapHalf;  // 'today' | 'tomorrow'
  // Add resize handles on non-wrap shifts. Wrap shifts (start > end render as
  // two blocks across midnight) are still editable via the form for v1.
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
  const roster = station.dj_roster || [];

  // ── Legend
  legend.innerHTML = '';
  const baseChip = document.createElement('span');
  baseChip.className = 'legend-item';
  baseChip.innerHTML = `<span class="legend-swatch" style="background:#334155; border:1px dashed #64748b;"></span>` +
                       `${station.dj_name || 'Base DJ'} (default)`;
  legend.appendChild(baseChip);
  roster.forEach((persona, i) => {
    const chip = document.createElement('span');
    chip.className = 'legend-item';
    chip.innerHTML = `<span class="legend-swatch" style="background:${_personaColor(i)};"></span>${persona.name}`;
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

  // ── Persona blocks (iterate in roster order so we can show overlap warnings later)
  roster.forEach((persona, personaIdx) => {
    const color = _personaColor(personaIdx);
    const shifts = persona.shifts || [];
    shifts.forEach((shift, shiftIdx) => {
      const dayIdx = DAYS_FULL.indexOf(shift.day);
      if (dayIdx === -1) return;
      const col = dayIdx + 2;
      const start = Number(shift.start_hour);
      const end = Number(shift.end_hour);
      if (start <= end) {
        _appendPersonaBlock(grid, persona, color, col, start + 2, end + 3, { personaIdx, shiftIdx });
      } else {
        // Wraps past midnight: render two blocks (today + tomorrow)
        _appendPersonaBlock(grid, persona, color, col, start + 2, 26,
                            { personaIdx, shiftIdx, isWrap: true, wrapHalf: 'today' });
        const tomorrowCol = ((dayIdx + 1) % 7) + 2;
        _appendPersonaBlock(grid, persona, color, tomorrowCol, 2, end + 3,
                            { personaIdx, shiftIdx, isWrap: true, wrapHalf: 'tomorrow' });
      }
    });
  });

  // ── NOW indicator: glowing outline on the current hour cell
  const now = new Date();
  const nowCol = _jsDayToGridIndex(now.getDay()) + 2;
  const nowRow = now.getHours() + 2;
  _appendCell(grid, '', 'grid-now-indicator', nowCol, nowRow);
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
    renderSchedule().then(_attachBlockClickHandlers);
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
  const idx = Number(e.currentTarget.dataset.personaIdx);
  if (!Number.isNaN(idx)) _openPersonaEditor(idx);
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
  const personaIdx = Number(block.dataset.personaIdx);
  const shiftIdx = Number(block.dataset.shiftIdx);
  const persona = config.station?.dj_roster?.[personaIdx];
  const shift = persona?.shifts?.[shiftIdx];
  if (!shift) return;

  _dragState = {
    config,
    persona,
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
  _suppressNextClick = true;

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
    _attachBlockClickHandlers();
  } catch (err) {
    console.warn('[schedule] save after resize failed:', err);
    await renderSchedule();
    _attachBlockClickHandlers();
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
  const personaIdx = Number(block.dataset.personaIdx);
  const shiftIdx = Number(block.dataset.shiftIdx);
  const persona = config.station?.dj_roster?.[personaIdx];
  const shift = persona?.shifts?.[shiftIdx];
  if (!shift) return;

  const start = Number(shift.start_hour);
  const end = Number(shift.end_hour);
  // Skip wrap-around defensively (the mousedown guard should already have
  // bailed for these, but belt-and-braces).
  if (start > end) return;

  _moveState = {
    config,
    persona,
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
  _suppressNextClick = true;

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
    _attachBlockClickHandlers();
  } catch (err) {
    console.warn('[schedule] save after move failed:', err);
    await renderSchedule();
    _attachBlockClickHandlers();
  }
}

// ── Persona editor form ─────────────────────────────────────────────────────
// schedulerEditing: null = no form open; -1 = new persona; >=0 = index in roster
let schedulerEditing = null;
let schedulerWorkingPersona = null;  // mutable form state, written through on Save

async function _openPersonaEditor(personaIdx) {
  let config;
  try { config = await api('/config'); } catch (_) { return; }
  const roster = config.station?.dj_roster || [];

  if (personaIdx === -1) {
    schedulerWorkingPersona = {
      name: '',
      style: '',
      voice: null,
      voice_instructions: null,
      shifts: [],
    };
  } else {
    // Deep clone so cancel returns the original untouched.
    schedulerWorkingPersona = JSON.parse(JSON.stringify(roster[personaIdx]));
    // Old-shape configs that haven't been resaved may not have shifts yet.
    schedulerWorkingPersona.shifts = schedulerWorkingPersona.shifts || [];
  }
  schedulerEditing = personaIdx;
  _renderPersonaForm();
  _setSchedulerSubView('edit');
}

function _renderPersonaForm() {
  const form = document.getElementById('personaForm');
  if (!form || !schedulerWorkingPersona) return;

  const p = schedulerWorkingPersona;
  const isNew = schedulerEditing === -1;
  const previewSample = p.name
    ? `Hi, you're listening to ${p.name} on RadioDunc.`
    : `Hi, you're listening to RadioDunc.`;

  form.innerHTML = `
    <div>
      <label for="pf-name">Name</label>
      <input type="text" id="pf-name" value="${_escapeAttr(p.name)}" required />
    </div>
    <div>
      <label for="pf-style">Personality / Style</label>
      <textarea id="pf-style" required>${_escapeText(p.style)}</textarea>
    </div>
    <div>
      <label for="pf-voice">Voice</label>
      <div class="voice-row">
        <select id="pf-voice">
          <option value="">(use station default)</option>
          ${OPENAI_VOICES.map(v => `<option value="${v}"${p.voice === v ? ' selected' : ''}>${v}</option>`).join('')}
        </select>
        <button type="button" class="preview-btn" id="pf-preview-btn">▶ Preview</button>
      </div>
    </div>
    <div>
      <label for="pf-voice-instructions">Voice instructions <span class="muted" style="text-transform:none; font-weight:400;">— how they should sound (pacing, tone, accent…)</span></label>
      <textarea id="pf-voice-instructions">${_escapeText(p.voice_instructions || '')}</textarea>
    </div>
    <div>
      <label>Shifts</label>
      <div id="pf-shifts" class="shifts-list"></div>
      <button type="button" id="pf-add-shift" class="add-shift-btn">+ Add shift</button>
    </div>
    <div class="preview-status" id="pf-preview-status"></div>
    <div class="persona-form-actions">
      <div class="left-group">
        <button type="submit" class="primary">${isNew ? 'Create persona' : 'Save changes'}</button>
        <button type="button" id="pf-cancel">Cancel</button>
      </div>
      ${isNew ? '' : '<button type="button" class="delete-btn" id="pf-delete">Delete</button>'}
    </div>
  `;

  _renderShifts();

  form.querySelector('#pf-cancel').addEventListener('click', () => _setSchedulerSubView('grid'));
  form.querySelector('#pf-add-shift').addEventListener('click', () => {
    p.shifts.push({ day: 'monday', start_hour: 9, end_hour: 17 });
    _renderShifts();
  });
  form.querySelector('#pf-preview-btn').addEventListener('click', () => _previewVoice(previewSample));
  if (!isNew) {
    form.querySelector('#pf-delete').addEventListener('click', _deletePersona);
  }
  form.addEventListener('submit', _savePersona);
}

function _renderShifts() {
  const container = document.getElementById('pf-shifts');
  if (!container) return;
  container.innerHTML = '';

  schedulerWorkingPersona.shifts.forEach((shift, i) => {
    const row = document.createElement('div');
    row.className = 'shift-row';
    row.innerHTML = `
      <select data-shift-i="${i}" data-field="day">
        ${DAYS_FULL.map(d => `<option value="${d}"${shift.day === d ? ' selected' : ''}>${d.charAt(0).toUpperCase() + d.slice(1)}</option>`).join('')}
      </select>
      <input type="number" data-shift-i="${i}" data-field="start_hour" min="0" max="23" value="${shift.start_hour}" />
      <input type="number" data-shift-i="${i}" data-field="end_hour" min="0" max="23" value="${shift.end_hour}" />
      <button type="button" class="remove-shift" data-shift-i="${i}">✕</button>
    `;
    container.appendChild(row);
  });

  // Wire up change handlers
  container.querySelectorAll('[data-shift-i]').forEach(el => {
    if (el.classList.contains('remove-shift')) {
      el.addEventListener('click', () => {
        schedulerWorkingPersona.shifts.splice(Number(el.dataset.shiftI), 1);
        _renderShifts();
      });
    } else {
      el.addEventListener('change', () => {
        const i = Number(el.dataset.shiftI);
        const field = el.dataset.field;
        const value = field === 'day' ? el.value : Number(el.value);
        schedulerWorkingPersona.shifts[i][field] = value;
      });
    }
  });
}

function _readFormIntoWorkingPersona() {
  const p = schedulerWorkingPersona;
  p.name = document.getElementById('pf-name').value.trim();
  p.style = document.getElementById('pf-style').value.trim();
  const v = document.getElementById('pf-voice').value;
  p.voice = v || null;
  const vi = document.getElementById('pf-voice-instructions').value.trim();
  p.voice_instructions = vi || null;
}

async function _previewVoice(sampleText) {
  _readFormIntoWorkingPersona();
  const btn = document.getElementById('pf-preview-btn');
  const status = document.getElementById('pf-preview-status');
  btn.disabled = true;
  status.textContent = 'Synthesizing preview…';
  try {
    const resp = await api('/tts/preview', {
      method: 'POST',
      body: JSON.stringify({
        text: sampleText,
        voice: schedulerWorkingPersona.voice,
        voice_instructions: schedulerWorkingPersona.voice_instructions,
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

async function _savePersona(event) {
  event.preventDefault();
  _readFormIntoWorkingPersona();
  const status = document.getElementById('pf-preview-status');

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  config.station = config.station || {};
  config.station.dj_roster = config.station.dj_roster || [];

  if (schedulerEditing === -1) {
    config.station.dj_roster.push(schedulerWorkingPersona);
  } else {
    config.station.dj_roster[schedulerEditing] = schedulerWorkingPersona;
  }

  status.textContent = 'Saving…';
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    status.textContent = '';
    _setSchedulerSubView('grid');
    await renderSchedule();
    _attachBlockClickHandlers();
  } catch (err) {
    status.textContent = `Save failed: ${err.message}`;
  }
}

async function _deletePersona() {
  if (schedulerEditing === -1) return;
  if (!confirm(`Delete persona "${schedulerWorkingPersona.name}"? This cannot be undone.`)) return;

  let config;
  try { config = await api('/config'); } catch (_) { return; }
  config.station.dj_roster.splice(schedulerEditing, 1);
  try {
    await api('/config', { method: 'PUT', body: JSON.stringify(config) });
    _setSchedulerSubView('grid');
    await renderSchedule();
    _attachBlockClickHandlers();
  } catch (err) {
    alert(`Delete failed: ${err.message}`);
  }
}

function _escapeAttr(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function _escapeText(s) { return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

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
  serverState = await api('/player/status');
  renderAll();
  // Light polling — client drives playback now, so we don't need frequent syncs.
  setInterval(refreshServerState, 10_000);
  refreshLibraryStatus();

  // Scheduler sidebar takeover: open/close + view-switching wiring.
  document.getElementById('openSchedulerBtn')?.addEventListener('click', () => _setSchedulerMode(true));
  document.getElementById('closeSchedulerBtn')?.addEventListener('click', () => _setSchedulerMode(false));
  document.getElementById('backToGridBtn')?.addEventListener('click', () => _setSchedulerSubView('grid'));
  document.getElementById('addPersonaBtn')?.addEventListener('click', () => _openPersonaEditor(-1));

  // Esc closes the scheduler back to the default sidebar view.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (document.getElementById('wrap')?.dataset.mode !== 'scheduler') return;
    if (document.querySelector('.sidebar-scheduler')?.dataset.subView === 'edit') {
      _setSchedulerSubView('grid');
    } else {
      _setSchedulerMode(false);
    }
  });

  _scheduleAutoRefresh();
}

init();

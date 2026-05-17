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
let adBadgeTimer     = null;  // setTimeout ID for hiding the ad badge

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
  console.log('[audio] transition start:', reason);

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

    // Optimistically update the now-playing label so the UI doesn't lag the audio.
    if (next.current_track_label) {
      document.getElementById('nowPlaying').textContent = next.current_track_label;
    }

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

    // Optional ad clip plays right after the DJ clip.
    let adBuf = null;
    if (next.ad_clip_url) {
      try {
        adBuf = await fetchAndDecode(next.ad_clip_url);
      } catch (err) {
        console.warn('[audio] ad clip unavailable, skipping:', err);
      }
    }

    // 4. Place the DJ clip (and optional ad) on the AudioContext timeline.
    //    djStart adapts: if we're on time, it overlaps the fade-out tail;
    //    if we're late, it fires immediately.
    const djStart = ctx.currentTime + 0.05;
    let djEnd = djStart; // advances as each clip is scheduled

    if (djBuf) {
      const djSrc  = ctx.createBufferSource();
      djSrc.buffer = djBuf;
      const djGain = ctx.createGain();
      djSrc.connect(djGain);
      djGain.connect(masterGain);

      djGain.gain.setValueAtTime(0, djStart);
      djGain.gain.linearRampToValueAtTime(DJ_GAIN, djStart + DJ_EDGE_S);
      djGain.gain.setValueAtTime(DJ_GAIN, djStart + djBuf.duration - DJ_EDGE_S);
      djGain.gain.linearRampToValueAtTime(0, djStart + djBuf.duration);

      djSrc.start(djStart);
      djEnd = djStart + djBuf.duration;
      console.log(`[audio] DJ clip: start=${djStart.toFixed(3)} dur=${djBuf.duration.toFixed(2)}`);
    }

    if (adBuf) {
      const adStart = djEnd + 0.1; // tiny gap between DJ and ad
      const adSrc  = ctx.createBufferSource();
      adSrc.buffer = adBuf;
      const adGain = ctx.createGain();
      adSrc.connect(adGain);
      adGain.connect(masterGain);

      adGain.gain.setValueAtTime(0, adStart);
      adGain.gain.linearRampToValueAtTime(DJ_GAIN, adStart + DJ_EDGE_S);
      adGain.gain.setValueAtTime(DJ_GAIN, adStart + adBuf.duration - DJ_EDGE_S);
      adGain.gain.linearRampToValueAtTime(0, adStart + adBuf.duration);

      adSrc.start(adStart);
      djEnd = adStart + adBuf.duration; // next track waits until after the ad
      console.log(`[audio] AD clip: start=${adStart.toFixed(3)} dur=${adBuf.duration.toFixed(2)}`);

      // Swap label to an Ad badge for the ad's duration, then restore.
      // Cancel any stale badge timer from a previous transition first.
      clearTimeout(adBadgeTimer);
      const trackLabel = next.current_track_label || document.getElementById('nowPlaying').textContent;
      const adShowMs   = Math.max(0, (adStart - ctx.currentTime) * 1000);
      const adHideMs   = Math.max(0, (djEnd   - ctx.currentTime) * 1000);
      setTimeout(() => { document.getElementById('nowPlaying').textContent = '📻 Ad break'; }, adShowMs);
      adBadgeTimer = setTimeout(() => {
        document.getElementById('nowPlaying').textContent = trackLabel;
        adBadgeTimer = null;
      }, adHideMs);
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
      whenDuration(curSlot().el, () => scheduleAutoTrigger(curSlot().el.duration));
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
async function startPlayback() {
  initAudio();
  await ctx.resume();

  const resp = await api('/player/play', {
    method: 'POST',
    body: JSON.stringify({ queue_size: 30 }),
  });
  serverState = resp.state;
  if (!serverState.current_track?.id) throw new Error('Server returned no current track');

  // Reset slots for a clean start
  for (const key of ['A', 'B']) {
    slots[key].gainNode.gain.cancelScheduledValues(ctx.currentTime);
    slots[key].gainNode.gain.setValueAtTime(0, ctx.currentTime);
    slots[key].el.pause();
    slots[key].el.src = '';
  }
  activeSlot = 'A';

  const cur = curSlot();
  await loadWithRetry(cur.el, `/media/track/${serverState.current_track?.id}`);
  await cur.el.play();

  // Fade in gently
  cur.gainNode.gain.setValueAtTime(0, ctx.currentTime);
  cur.gainNode.gain.linearRampToValueAtTime(1.0, ctx.currentTime + 0.5);

  scheduleAutoTrigger(cur.el.duration); // metadata already loaded by loadWithRetry

  renderAll();
}

// ── Stop playback ─────────────────────────────────────────────────────────────
async function stopPlayback() {
  clearAutoTrigger();
  transitioning = false;
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

// ── Render ────────────────────────────────────────────────────────────────────
function renderPlayer() {
  const station = serverState?.station;
  document.getElementById('stationName').textContent = station?.name || 'RadioDunc';
  document.getElementById('stationMeta').textContent = station?.tagline || 'Loading station…';
  document.getElementById('volume').value = savedVolume();
  if (masterGain) masterGain.gain.value = musicVolume();

  const label = serverState?.now_playing_label || '-';
  document.getElementById('nowPlaying').textContent = label;
  document.getElementById('playerFlags').textContent =
    serverState?.is_playing
      ? (transitioning ? 'Transitioning…' : 'Playing')
      : 'Stopped';
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
    li.style.cssText = 'display:flex; align-items:center; gap:8px; padding:4px 0; cursor:grab; border-top:2px solid transparent; transition:border-color 0.1s, opacity 0.1s;';

    const handle = document.createElement('span');
    handle.textContent = '⠿';
    handle.style.cssText = 'color:#475569; font-size:1.1em; user-select:none;';

    const span = document.createElement('span');
    span.textContent = item.label;
    span.style.flex = '1';

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
}

function renderAll() { renderPlayer(); renderQueue(); }

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
  } catch (err) {
    status.textContent = `Scan failed: ${err.message}`;
  }
});

document.getElementById('playBtn').addEventListener('click', async () => {
  const status = document.getElementById('scanStatus');
  status.textContent = '';
  try {
    await startPlayback();
  } catch (err) {
    status.textContent = `Play failed: ${err.message}`;
  }
});

document.getElementById('nextBtn').addEventListener('click', () => {
  if (!ctx || transitioning) return;
  const btn = document.getElementById('nextBtn');
  btn.disabled = true;
  btn.textContent = 'Loading…';
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
}

init();

'use strict';

// ── Timing constants ─────────────────────────────────────────────────────────
// Adjust these to taste; all audio scheduling uses AudioContext time (sample-accurate).
const FADE_OUT_S     = 2.5;  // current track fades 1→0 over this many seconds
const DJ_OVERLAP_S   = 0.5;  // DJ clip starts this far before the fade-out ends
const FADE_IN_S      = 1.2;  // next track fades 0→1 over this many seconds
const DJ_EDGE_S      = 0.2;  // DJ clip's own tiny in/out fade
const AUTO_PREROLL_S = 10;   // start transition this many seconds before track end

// ── App state ────────────────────────────────────────────────────────────────
let stations    = [];
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

// ── Helpers ──────────────────────────────────────────────────────────────────
function musicVolume()    { return Math.max(0, Math.min(1, (serverState?.volume ?? 80) / 100)); }
function stationLabel(id) { return stations.find(s => s.id === id)?.name || `#${id}`; }

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
  return resp.json();
}

// ── AudioContext bootstrap ────────────────────────────────────────────────────
// Call from a user-gesture handler so the context starts in "running" state.
function initAudio() {
  if (ctx) return;
  ctx        = new AudioContext();
  masterGain = ctx.createGain();
  masterGain.gain.value = musicVolume();
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
  if (!trackDurationSec) return;
  const elapsed  = curSlot().el.currentTime || 0;
  const delaySec = Math.max(0, trackDurationSec - elapsed - AUTO_PREROLL_S);
  console.log(`[audio] auto-trigger in ${delaySec.toFixed(1)}s`);
  autoTriggerTimer = setTimeout(() => triggerTransition('auto'), delaySec * 1000);
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
      next = await api('/player/next', { method: 'POST' });
    } catch (err) {
      console.error('[audio] /player/next failed — restoring gain:', err);
      const now = ctx.currentTime;
      curGain.cancelScheduledValues(now);
      curGain.setValueAtTime(curGain.value, now);
      curGain.linearRampToValueAtTime(1.0, now + 0.3);
      return;
    }

    // 3. Decode DJ clip and start loading next track (parallel work).
    //    The gainNode on the alt slot is already at 0 from the previous transition
    //    or from init; just make sure.
    const alt = altSlot();
    alt.gainNode.gain.cancelScheduledValues(ctx.currentTime);
    alt.gainNode.gain.setValueAtTime(0, ctx.currentTime);
    alt.el.src = next.current_track_url;
    alt.el.load();

    let djBuf = null;
    try {
      djBuf = await fetchAndDecode(next.dj_clip_url);
    } catch (err) {
      console.warn('[audio] DJ clip unavailable, crossfading without it:', err);
    }

    // 4. Place the DJ clip on the AudioContext timeline.
    //    djStart adapts: if we're on time, it overlaps the fade-out tail;
    //    if we're late, it fires immediately.
    const djStart = Math.max(ctx.currentTime + 0.05, t + FADE_OUT_S - DJ_OVERLAP_S);
    let djEnd = djStart; // advances to djStart + clip duration if we have a clip

    if (djBuf) {
      const djSrc  = ctx.createBufferSource();
      djSrc.buffer = djBuf;
      const djGain = ctx.createGain();
      djSrc.connect(djGain);
      djGain.connect(masterGain);

      // Short in/out fades on the DJ clip itself to avoid clicks.
      djGain.gain.setValueAtTime(0, djStart);
      djGain.gain.linearRampToValueAtTime(1.0, djStart + DJ_EDGE_S);
      djGain.gain.setValueAtTime(1.0, djStart + djBuf.duration - DJ_EDGE_S);
      djGain.gain.linearRampToValueAtTime(0, djStart + djBuf.duration);

      // AudioBufferSourceNode.start(when) is sample-accurate.
      djSrc.start(djStart);
      djEnd = djStart + djBuf.duration;
      console.log(`[audio] DJ clip: start=${djStart.toFixed(3)} dur=${djBuf.duration.toFixed(2)}`);
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
    //    next.next_track_metadata.duration_seconds is the *new current* track's duration
    //    (the one at current_track_url), returned by the server for exactly this purpose.
    const triggerSetupMs = Math.max(0, (trackGainStart + 0.3 - ctx.currentTime) * 1000);
    if (next.next_track_metadata?.duration_seconds) {
      setTimeout(() => scheduleAutoTrigger(next.next_track_metadata.duration_seconds), triggerSetupMs);
    } else {
      // Fall back to the audio element's own loadedmetadata
      curSlot().el.addEventListener('loadedmetadata', () => {
        scheduleAutoTrigger(curSlot().el.duration);
      }, { once: true });
    }

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

  // Ensure a station is selected
  if (!serverState?.station_id) {
    const id =
      serverState?.recent_station_ids?.[0] ??
      serverState?.favorites?.[0] ??
      stations?.[0]?.id;
    if (!id) throw new Error('No station available. Scan library and generate stations first.');
    serverState = await api('/player/state', {
      method: 'PUT',
      body: JSON.stringify({ station_id: id }),
    });
  }

  const resp = await api('/player/play', {
    method: 'POST',
    body: JSON.stringify({ station_id: serverState.station_id, queue_size: 12 }),
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
  cur.el.src = `/media/track/${serverState.current_track?.id}`;
  cur.el.load();
  await cur.el.play();

  // Fade in gently
  cur.gainNode.gain.setValueAtTime(0, ctx.currentTime);
  cur.gainNode.gain.linearRampToValueAtTime(1.0, ctx.currentTime + 0.5);

  cur.el.addEventListener('loadedmetadata', () => {
    scheduleAutoTrigger(cur.el.duration);
  }, { once: true });

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
function renderStations() {
  const q         = document.getElementById('search').value.toLowerCase();
  const container = document.getElementById('stations');
  container.innerHTML = '';
  stations.filter(s => s.name.toLowerCase().includes(q)).forEach(s => {
    const card = document.createElement('div');
    card.className = `card station${serverState?.station_id === s.id ? ' active' : ''}`;
    card.innerHTML = `<strong>${s.name}</strong><div class="muted">${s.tagline || s.format || 'Local station'}</div>`;

    const row    = document.createElement('div');
    row.className = 'row';
    const favBtn = document.createElement('button');
    favBtn.textContent = serverState?.favorites?.includes(s.id) ? '★ Favorited' : '☆ Favorite';
    favBtn.onclick = async (e) => {
      e.stopPropagation();
      const want = !serverState?.favorites?.includes(s.id);
      await api(`/stations/${s.id}/favorite`, { method: 'PUT', body: JSON.stringify({ favorite: want }) });
      await refreshServerState();
    };
    row.appendChild(favBtn);
    card.appendChild(row);

    card.onclick = async () => {
      serverState = await api('/player/state', { method: 'PUT', body: JSON.stringify({ station_id: s.id }) });
      renderAll();
    };
    container.appendChild(card);
  });
}

function renderPlayer() {
  const cur = stations.find(s => s.id === serverState?.station_id);
  document.getElementById('stationName').textContent = cur?.name || 'No station selected';
  document.getElementById('stationMeta').textContent = cur
    ? (cur.tagline || cur.format || 'Live radio')
    : 'Pick a station, then press Play.';
  document.getElementById('volume').value = serverState?.volume ?? 80;
  if (masterGain) masterGain.gain.value = musicVolume();

  const label = serverState?.now_playing_label || '-';
  document.getElementById('nowPlaying').textContent = label;
  document.getElementById('playerFlags').textContent =
    serverState?.is_playing
      ? (transitioning ? 'Transitioning…' : 'Playing')
      : 'Stopped';
  document.getElementById('upNext').textContent =
    serverState?.is_playing ? 'Web Audio — direct file playback' : '-';
  document.getElementById('favorites').textContent =
    (serverState?.favorites || []).map(stationLabel).join(', ') || '-';
  document.getElementById('recent').textContent =
    (serverState?.recent_station_ids || []).map(stationLabel).join(', ') || '-';
}

function renderAll() { renderStations(); renderPlayer(); }

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
document.getElementById('search').addEventListener('input', renderStations);

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

document.getElementById('refreshStationsBtn').addEventListener('click', async () => {
  const status = document.getElementById('scanStatus');
  status.textContent = 'Generating stations…';
  try {
    const result = await api('/stations/generate', { method: 'POST', body: JSON.stringify({}) });
    stations     = await api('/stations');
    await refreshServerState();
    status.textContent = `Generated ${result?.generated ?? stations.length} stations.`;
  } catch (err) {
    status.textContent = `Refresh failed: ${err.message}`;
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
  triggerTransition('user');
});

document.getElementById('stopBtn').addEventListener('click', () => stopPlayback());

document.getElementById('volume').addEventListener('input', async (e) => {
  const vol = Number(e.target.value);
  // Update gain immediately — no round-trip latency.
  if (masterGain) masterGain.gain.value = vol / 100;
  // Persist to server (best effort; we don't block on it).
  try {
    const resp = await api('/player/state', { method: 'PUT', body: JSON.stringify({ volume: vol }) });
    serverState = resp;
  } catch (_) {}
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && serverState?.is_playing) refreshServerState();
});

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  const config = await api('/config');
  document.getElementById('libraryPath').value = config.music_folder || '';
  stations    = await api('/stations');
  serverState = await api('/player/status');
  renderAll();
  // Light polling — client drives playback now, so we don't need frequent syncs.
  setInterval(refreshServerState, 10_000);
}

init();

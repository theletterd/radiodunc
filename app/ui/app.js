let stations = [];
let state = null;
const audioEl = document.getElementById('playerAudio');
const overlayEl = document.getElementById('overlayAudio');
let loadedQueuePosition = null;
let transitionInFlight = false;
let transitionEpoch = 0;
const FADE_DURATION_MS = 8000;
const DJ_DUCK_FADE_DURATION_MS = 500;
const DJ_DUCK_MULTIPLIER = 0.5;
const DJ_FIXED_VOLUME = 1;
const TRACK_TO_DJ_PREROLL_SECONDS = 20;
let djPreparedForTrackQueuePosition = null;
let djOverlayFinishingHandledForQueuePosition = null;
let trackTransitionTriggeredForQueuePosition = null;
let overlapTrackQueuePosition = null;
let overlapTrackEnded = false;
let overlapDjEnded = false;
let overlapAdvancedToSecondTrack = false;
let playbackRetryTimer = null;
const PLAYBACK_RETRY_DELAY_MS = 750;

function musicTargetVolume() {
  return Math.max(0, Math.min(1, (state?.volume ?? 80) / 100));
}

async function api(path, options = {}) {
  console.log('[ui][api] request', { path, method: options.method || 'GET' });
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!response.ok) {
    console.error('[ui][api] response error', { path, status: response.status });
    throw new Error(await response.text());
  }
  console.log('[ui][api] response ok', { path, status: response.status });
  return response.json();
}

function stationName(id) {
  return stations.find((s) => s.id === id)?.name || `#${id}`;
}

function renderStations() {
  const q = document.getElementById('search').value.toLowerCase();
  const el = document.getElementById('stations');
  el.innerHTML = '';
  stations.filter((s) => s.name.toLowerCase().includes(q)).forEach((s) => {
    const card = document.createElement('div');
    card.className = `card station${state?.station_id === s.id ? ' active' : ''}`;
    card.innerHTML = `<strong>${s.name}</strong><div class="muted">${s.tagline || s.format || 'Local station'}</div>`;

    const row = document.createElement('div');
    row.className = 'row';
    const favBtn = document.createElement('button');
    favBtn.textContent = state?.favorites?.includes(s.id) ? '★ Favorited' : '☆ Favorite';
    favBtn.onclick = async (event) => {
      console.log('[ui][button] favorite clicked', { stationId: s.id });
      event.stopPropagation();
      const target = !state?.favorites?.includes(s.id);
      console.log('[ui][button] favorite target', { stationId: s.id, favorite: target });
      await api(`/stations/${s.id}/favorite`, { method: 'PUT', body: JSON.stringify({ favorite: target }) });
      await refreshState();
    };
    row.appendChild(favBtn);
    card.appendChild(row);

    card.onclick = async () => {
      console.log('[ui][station] card clicked', { stationId: s.id });
      state = await api('/player/state', { method: 'PUT', body: JSON.stringify({ station_id: s.id }) });
      console.log('[ui][station] card select complete', { stationId: s.id });
      renderAll();
    };
    el.appendChild(card);
  });
}

function renderPlayer() {
  const current = stations.find((s) => s.id === state?.station_id);
  document.getElementById('stationName').textContent = current?.name || 'No station selected';
  document.getElementById('stationMeta').textContent = current ? (current.tagline || current.format || 'Live radio') : 'Pick a station, then press Play Station.';
  const sliderValue = state?.volume ?? 80;
  document.getElementById('volume').value = sliderValue;
  if (!transitionInFlight) {
    audioEl.volume = musicTargetVolume();
  }

  const label = state?.now_playing_label || '-';
  document.getElementById('nowPlaying').textContent = label;
  document.getElementById('playerFlags').textContent = `State: ${state?.is_playing ? 'Playing' : 'Stopped'} · Type: ${state?.now_playing_type || '-'} · Queue position: ${state?.queue_position ?? '-'} `;
  document.getElementById('upNext').textContent = `Queue depth: ${state?.queue_depth ?? 0}`;
  document.getElementById('favorites').textContent = (state?.favorites || []).map(stationName).join(', ') || '-';
  document.getElementById('recent').textContent = (state?.recent_station_ids || []).map(stationName).join(', ') || '-';
}

function renderAll() {
  renderStations();
  renderPlayer();
}


async function fadeToVolume(targetVolume, durationMs = FADE_DURATION_MS, epoch = transitionEpoch) {
  const clamped = Math.max(0, Math.min(1, targetVolume));
  const start = audioEl.volume;
  const delta = clamped - start;
  if (Math.abs(delta) < 0.01 || durationMs <= 0) {
    audioEl.volume = clamped;
    return;
  }

  const startAt = performance.now();
  await new Promise((resolve) => {
    function step(now) {
      if (epoch !== transitionEpoch) {
        resolve();
        return;
      }
      const progress = Math.min(1, (now - startAt) / durationMs);
      audioEl.volume = Math.max(0, Math.min(1, start + (delta * progress)));
      if (progress < 1) {
        requestAnimationFrame(step);
      } else {
        resolve();
      }
    }
    requestAnimationFrame(step);
  });
}


function clearPlaybackRetry() {
  if (playbackRetryTimer !== null) {
    clearTimeout(playbackRetryTimer);
    playbackRetryTimer = null;
  }
}

function schedulePlaybackRetry() {
  if (playbackRetryTimer !== null || !state?.is_playing) return;
  playbackRetryTimer = window.setTimeout(async () => {
    playbackRetryTimer = null;
    await syncAudioToState();
  }, PLAYBACK_RETRY_DELAY_MS);
}

async function syncAudioToState() {
  console.log('[ui][audio] sync start', {
    isPlaying: state?.is_playing,
    queuePosition: state?.queue_position,
    nowPlayingType: state?.now_playing_type,
    loadedQueuePosition,
  });
  if (!state?.is_playing) {
    console.log('[ui][audio] sync stopping playback because state is not playing');
    audioEl.pause();
    overlayEl.pause();
    audioEl.removeAttribute('src');
    overlayEl.removeAttribute('src');
    audioEl.load();
    overlayEl.load();
    loadedQueuePosition = null;
    clearPlaybackRetry();
    return;
  }

  if (!state?.now_playing_type) {
    return;
  }

  if (loadedQueuePosition !== state.queue_position) {
    console.log('[ui][audio] loading queue position', { from: loadedQueuePosition, to: state.queue_position });
    audioEl.src = `/player/current-media?pos=${state.queue_position}&ts=${Date.now()}`;
    loadedQueuePosition = state.queue_position;
    djPreparedForTrackQueuePosition = null;
    djOverlayFinishingHandledForQueuePosition = null;
  }
  try {
    clearPlaybackRetry();
    await audioEl.play();
    console.log('[ui][audio] play() resolved');
    const targetVolume = musicTargetVolume();
    if (audioEl.volume < targetVolume - 0.01) {
      await fadeToVolume(targetVolume, FADE_DURATION_MS);
    }
  } catch (_err) {
    console.warn('[ui][audio] play() failed, scheduling retry');
    schedulePlaybackRetry();
  }
}


async function fadeThenAdvance(actionFn) {
  if (transitionInFlight) return;
  const epoch = ++transitionEpoch;
  transitionInFlight = true;
  const targetVolume = musicTargetVolume();
  try {
    if (state?.is_playing && !audioEl.paused) {
      await fadeToVolume(0, FADE_DURATION_MS, epoch);
    }
    const response = await actionFn();
    state = response.state;
    renderAll();
    audioEl.volume = 0;
    await syncAudioToState();
    await fadeToVolume(targetVolume, FADE_DURATION_MS, epoch);
    overlapTrackQueuePosition = null;
    overlapTrackEnded = false;
    overlapDjEnded = false;
    overlapAdvancedToSecondTrack = false;
  } finally {
    if (epoch === transitionEpoch) {
      transitionInFlight = false;
    }
  }
}

async function runDjOverlapTransition() {
  if (!state?.is_playing || state?.now_playing_type !== 'track') {
    await fadeThenAdvance(() => api('/player/next', { method: 'POST' }));
    return;
  }

  if (transitionInFlight || djPreparedForTrackQueuePosition === state.queue_position) return;
  const epoch = ++transitionEpoch;
  transitionInFlight = true;
  const targetVolume = musicTargetVolume();
  const duckedMusicVolume = targetVolume * DJ_DUCK_MULTIPLIER;
  try {
    const trackQueuePosition = state.queue_position;
    overlapTrackQueuePosition = trackQueuePosition;
    overlapTrackEnded = false;
    overlapDjEnded = false;
    overlapAdvancedToSecondTrack = false;
    const djResponse = await api('/player/next', { method: 'POST' });
    if (djResponse?.state?.now_playing_type !== 'dj') {
      state = djResponse.state;
      renderAll();
      audioEl.volume = 0;
      await syncAudioToState();
      await fadeToVolume(targetVolume, FADE_DURATION_MS, epoch);
      return;
    }

    state = djResponse.state;
    renderAll();
    overlayEl.src = `/player/current-media?pos=${state.queue_position}&ts=${Date.now()}`;
    overlayEl.volume = DJ_FIXED_VOLUME;
    djOverlayFinishingHandledForQueuePosition = null;
    try {
      await overlayEl.play();
    } catch (_err) { }

    if (!audioEl.paused) {
      await fadeToVolume(duckedMusicVolume, DJ_DUCK_FADE_DURATION_MS, epoch);
    }

    djPreparedForTrackQueuePosition = trackQueuePosition;
  } finally {
    if (epoch === transitionEpoch) {
      transitionInFlight = false;
    }
  }
}


async function maybeAdvanceAfterOverlapProgress() {
  if (overlapTrackQueuePosition === null) return;
  if (!overlapAdvancedToSecondTrack && overlapTrackEnded) {
    overlapAdvancedToSecondTrack = true;
    await transitionFromDjToNextTrack({ keepDjOverlayPlaying: !overlapDjEnded });
    return;
  }
  if (overlapAdvancedToSecondTrack && overlapDjEnded) {
    overlayEl.pause();
    overlayEl.removeAttribute('src');
    overlayEl.load();
    overlapTrackQueuePosition = null;
    overlapTrackEnded = false;
    overlapDjEnded = false;
    overlapAdvancedToSecondTrack = false;
  }
}

async function transitionFromDjToNextTrack({ keepDjOverlayPlaying = false } = {}) {
  if (transitionInFlight) return;
  const epoch = ++transitionEpoch;
  transitionInFlight = true;
  const targetVolume = musicTargetVolume();
  try {
    const nextTrackResponse = await api('/player/next', { method: 'POST' });
    state = nextTrackResponse.state;
    renderAll();
    if (!keepDjOverlayPlaying) {
      overlayEl.pause();
      overlayEl.removeAttribute('src');
      overlayEl.load();
    }
    audioEl.volume = 0;
    await syncAudioToState();
    await fadeToVolume(targetVolume, FADE_DURATION_MS, epoch);
    overlapTrackQueuePosition = null;
    overlapTrackEnded = false;
    overlapDjEnded = false;
    overlapAdvancedToSecondTrack = false;
  } finally {
    if (epoch === transitionEpoch) {
      transitionInFlight = false;
    }
  }
}

async function stopPlayback() {
  transitionEpoch += 1;
  transitionInFlight = false;
  try {
    const response = await api('/player/stop', { method: 'POST' });
    state = response.state;
  } catch (_err) {
    if (state) {
      state.is_playing = false;
    }
  }
  audioEl.pause();
  overlayEl.pause();
  audioEl.removeAttribute('src');
  overlayEl.removeAttribute('src');
  audioEl.load();
  overlayEl.load();
  loadedQueuePosition = null;
  djPreparedForTrackQueuePosition = null;
  djOverlayFinishingHandledForQueuePosition = null;
  trackTransitionTriggeredForQueuePosition = null;
  overlapTrackQueuePosition = null;
  overlapTrackEnded = false;
  overlapDjEnded = false;
  overlapAdvancedToSecondTrack = false;
  clearPlaybackRetry();
  renderAll();
}

async function refreshState() {
  state = await api('/player/status');
  renderAll();
  await syncAudioToState();
}

async function loadConfigDefaults() {
  const config = await api('/config');
  document.getElementById('libraryPath').value = config.music_folder || '';
}

async function loadStations() {
  stations = await api('/stations');
}

document.getElementById('search').addEventListener('input', renderStations);
document.getElementById('scanBtn').addEventListener('click', async () => {
  console.log('[ui][button] scan clicked');
  const status = document.getElementById('scanStatus');
  const folderPath = document.getElementById('libraryPath').value;
  console.log('[ui][button] scan params', { folderPath });
  status.textContent = 'Scanning...';
  try {
    const result = await api('/library/scan', { method: 'POST', body: JSON.stringify({ folder_path: folderPath }) });
    status.textContent = `Scanned ${result.scanned} files, added ${result.inserted}, updated ${result.updated}.`;
  } catch (error) {
    console.error('[ui][button] scan failed', { error: error.message });
    status.textContent = `Scan failed: ${error.message}`;
  }
});

document.getElementById('refreshStationsBtn').addEventListener('click', async () => {
  console.log('[ui][button] refresh stations clicked');
  const status = document.getElementById('scanStatus');
  status.textContent = 'Generating stations...';
  try {
    const result = await api('/stations/generate', { method: 'POST', body: JSON.stringify({}) });
    await loadStations();
    await refreshState();
    console.log('[ui][button] refresh stations complete', {
      generated: result?.generated,
      stationCount: stations.length,
    });
    status.textContent = `Generated ${result?.generated ?? stations.length} stations.`;
  } catch (error) {
    console.error('[ui][button] refresh stations failed', { error: error.message });
    status.textContent = `Refresh failed: ${error.message}`;
  }
});

document.getElementById('playBtn').addEventListener('click', async () => {
  console.log('[ui][button] play clicked', { stationId: state?.station_id });
  if (!state?.station_id) {
    console.warn('[ui][button] play ignored: no station selected');
    return;
  }
  const response = await api('/player/play', { method: 'POST', body: JSON.stringify({ station_id: state.station_id, queue_size: 10 }) });
  state = response.state;
  console.log('[ui][button] play response', {
    isPlaying: state?.is_playing,
    queuePosition: state?.queue_position,
    nowPlayingType: state?.now_playing_type,
  });
  renderAll();
  await syncAudioToState();
});

document.getElementById('nextBtn').addEventListener('click', async () => {
  console.log('[ui][button] next clicked', {
    transitionInFlight,
    queuePosition: state?.queue_position,
    nowPlayingType: state?.now_playing_type,
  });
  await runDjOverlapTransition();
  console.log('[ui][button] next flow complete');
});

document.getElementById('stopBtn').addEventListener('click', async () => {
  console.log('[ui][button] stop clicked');
  await stopPlayback();
  console.log('[ui][button] stop flow complete');
});

audioEl.addEventListener('timeupdate', async () => {
  if (transitionInFlight) return;
  if (!state?.is_playing || state?.now_playing_type !== 'track') return;
  if (!Number.isFinite(audioEl.duration) || audioEl.duration <= 0) return;

  const remaining = audioEl.duration - audioEl.currentTime;
  if (trackTransitionTriggeredForQueuePosition === state.queue_position) return;
  if (remaining <= TRACK_TO_DJ_PREROLL_SECONDS) {
    trackTransitionTriggeredForQueuePosition = state.queue_position;
    await runDjOverlapTransition();
  }
});

audioEl.addEventListener('ended', async () => {
  if (transitionInFlight) return;
  if (overlapTrackQueuePosition !== null) {
    overlapTrackEnded = true;
    await maybeAdvanceAfterOverlapProgress();
    return;
  }
  if (trackTransitionTriggeredForQueuePosition === state?.queue_position) return;
  trackTransitionTriggeredForQueuePosition = state?.queue_position ?? null;
  await runDjOverlapTransition();
});

overlayEl.addEventListener('ended', async () => {
  if (transitionInFlight) return;
  if (overlapTrackQueuePosition !== null) {
    overlapDjEnded = true;
    djOverlayFinishingHandledForQueuePosition = state?.queue_position ?? null;
    await maybeAdvanceAfterOverlapProgress();
    return;
  }
  await transitionFromDjToNextTrack();
});

document.getElementById('volume').addEventListener('input', async (event) => {
  console.log('[ui][control] volume input', { value: Number(event.target.value) });
  state = await api('/player/state', { method: 'PUT', body: JSON.stringify({ volume: Number(event.target.value) }) });
  console.log('[ui][control] volume update response', { volume: state?.volume });
  renderAll();
  await syncAudioToState();
});

document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible' && state?.is_playing && audioEl.paused) {
    await syncAudioToState();
  }
});

window.addEventListener('focus', async () => {
  if (state?.is_playing && audioEl.paused) {
    await syncAudioToState();
  }
});

async function init() {
  console.log('[ui][init] start');
  await loadConfigDefaults();
  await loadStations();
  state = await api('/player/status');
  console.log('[ui][init] initial state loaded', {
    stationId: state?.station_id,
    isPlaying: state?.is_playing,
    queuePosition: state?.queue_position,
    stationCount: stations.length,
  });
  renderAll();
  await syncAudioToState();
  setInterval(refreshState, 5000);
  console.log('[ui][init] complete; refresh interval set to 5000ms');
}

init();

// Tests for the player rendering functions in app/ui/app.js:
// renderPlayer, renderQueue, renderAll, setOnAirMode, and related helpers.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

function stubFetch(handlers) {
  globalThis.fetch = vi.fn(async (url, opts = {}) => {
    const method = opts.method || 'GET';
    const path = String(url).split('?')[0];
    const key = `${method} ${path}`;
    const handler = handlers[key];
    if (!handler) {
      return globalThis.__fakeJsonResponse({}, 204);
    }
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    const responseBody = await handler(body);
    return globalThis.__fakeJsonResponse(responseBody);
  });
}

function baseState(overrides = {}) {
  return {
    is_playing: false,
    volume: 80,
    now_playing_label: 'Test Track — Artist',
    current_track: null,
    station: { name: 'KVW 96.7', tagline: 'Always on' },
    ...overrides,
  };
}

// ── renderPlayer ─────────────────────────────────────────────────────────────

describe('renderPlayer', () => {
  beforeEach(() => {
    loadAppJs();
    // Reset AudioContext and internal state accessors before each test
    globalThis.__setCtx(null);
    globalThis.__setPaused(false);
    globalThis.__setOnAirModeVar('track');
  });

  it('shows station name and tagline', () => {
    globalThis.__setServerState(baseState());
    globalThis.renderPlayer();

    expect(document.getElementById('stationName').textContent).toBe('KVW 96.7');
    expect(document.getElementById('stationMeta').textContent).toBe('Always on');
  });

  it('shows "Stopped" in playerFlags when not playing', () => {
    globalThis.__setServerState(baseState({ is_playing: false }));
    globalThis.renderPlayer();

    expect(document.getElementById('playerFlags').textContent).toBe('Stopped');
  });

  it('shows "On air" with live-dot when actively playing', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx({});  // ctx truthy → audio is live

    globalThis.renderPlayer();

    const flags = document.getElementById('playerFlags');
    expect(flags.innerHTML).toContain('On air');
    expect(flags.querySelector('span.live-dot')).not.toBeNull();
  });

  it('shows "Paused" when paused is true', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx({});
    globalThis.__setPaused(true);

    globalThis.renderPlayer();

    expect(document.getElementById('playerFlags').textContent).toBe('Paused');
  });

  it('shows "Ready — click Play to resume" when server says playing but no AudioContext', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx(null);  // no AudioContext

    globalThis.renderPlayer();

    expect(document.getElementById('playerFlags').textContent).toBe('Ready — click Play to resume');
  });

  it('Play button reads "Pause" when audio is live', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx({});
    globalThis.__setPaused(false);

    globalThis.renderPlayer();

    expect(document.getElementById('playBtn').textContent).toBe('Pause');
  });

  it('Play button reads "Play" when stopped', () => {
    globalThis.__setServerState(baseState({ is_playing: false }));
    globalThis.__setCtx(null);

    globalThis.renderPlayer();

    expect(document.getElementById('playBtn').textContent).toBe('Play');
  });

  it('Play button reads "Play" when paused', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx({});
    globalThis.__setPaused(true);

    globalThis.renderPlayer();

    expect(document.getElementById('playBtn').textContent).toBe('Play');
  });

  it('Play button reads "Play" when no AudioContext (ready-to-resume state)', () => {
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx(null);

    globalThis.renderPlayer();

    expect(document.getElementById('playBtn').textContent).toBe('Play');
  });

  it('nowPlayingCard has "active" class only when audio is live', () => {
    const card = document.getElementById('nowPlayingCard');

    // Live: is_playing + ctx + not paused
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx({});
    globalThis.__setPaused(false);
    globalThis.renderPlayer();
    expect(card.classList.contains('active')).toBe(true);

    // Paused → not active
    globalThis.__setPaused(true);
    globalThis.renderPlayer();
    expect(card.classList.contains('active')).toBe(false);

    // Stopped → not active
    globalThis.__setServerState(baseState({ is_playing: false }));
    globalThis.__setPaused(false);
    globalThis.renderPlayer();
    expect(card.classList.contains('active')).toBe(false);

    // No ctx → not active
    globalThis.__setServerState(baseState({ is_playing: true }));
    globalThis.__setCtx(null);
    globalThis.renderPlayer();
    expect(card.classList.contains('active')).toBe(false);
  });

  it('does NOT overwrite #nowPlaying text when onAirMode is "dj"', () => {
    globalThis.__setServerState(baseState({
      is_playing: true,
      now_playing_label: 'Some Track',
    }));
    globalThis.__setCtx({});
    globalThis.__setOnAirModeVar('dj');

    const nowPlayingEl = document.getElementById('nowPlaying');
    nowPlayingEl.textContent = '🎙️ On air';

    globalThis.renderPlayer();

    // The badge text should be preserved — renderPlayer must not stomp it
    // when onAirMode !== 'track'.
    expect(nowPlayingEl.textContent).toBe('🎙️ On air');
  });
});

// ── renderQueue ───────────────────────────────────────────────────────────────

describe('renderQueue', () => {
  beforeEach(() => {
    loadAppJs();
    globalThis.__setCtx(null);
    globalThis.__setPaused(false);
    globalThis.__setOnAirModeVar('track');
  });

  it('clears the queue list when not playing', async () => {
    globalThis.__setServerState(baseState({ is_playing: false }));

    // Pre-populate the list to confirm it gets cleared
    const list = document.getElementById('queueList');
    list.innerHTML = '<li>stale item</li>';

    await globalThis.renderQueue();

    expect(list.innerHTML).toBe('');
  });

  it('paints upcoming items when is_playing is true', async () => {
    globalThis.__setServerState(baseState({ is_playing: true }));

    stubFetch({
      'GET /player/queue': () => ({
        items: [
          { position: 1, label: 'Song One — Band A' },
          { position: 2, label: 'Song Two — Band B' },
        ],
      }),
    });

    await globalThis.renderQueue();
    await flush();

    const items = document.querySelectorAll('#queueList li');
    expect(items.length).toBe(2);

    // Each item should have its label text
    expect(items[0].textContent).toContain('Song One — Band A');
    expect(items[1].textContent).toContain('Song Two — Band B');

    // Each item should have a drag handle span
    expect(items[0].querySelector('span.handle')).not.toBeNull();
    expect(items[1].querySelector('span.handle')).not.toBeNull();

    // Each item should have a remove button
    const btns = items[0].querySelectorAll('button');
    expect(btns.length).toBeGreaterThan(0);
    expect(btns[0].textContent).toBe('✕');
  });
});

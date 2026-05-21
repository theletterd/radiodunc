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


// ── setOnAirMode + badge avatar ─────────────────────────────────────────────

describe('setOnAirMode badge avatar', () => {
  beforeEach(() => {
    // Stub fetch BEFORE loadAppJs so init()'s /config call hits a sensible
    // response instead of the default `{}` (which has no .music_folder and
    // trips an unhandled rejection that vitest surfaces as a noisy "1 error"
    // line). The other test files get away with no stub because they don't
    // await past the synchronous tick where init() blows up; ours await
    // flush() for the animation, so the rejection has time to land.
    stubFetch({
      'GET /config': () => ({ music_folder: '/x', station: {}, alerts: {} }),
      'GET /player/status': () => baseState(),
      'GET /player/queue': () => ({ items: [] }),
      'GET /library/status': () => ({ track_count: 0 }),
    });
    loadAppJs();
    globalThis.__setPaused(false);
    globalThis.__setOnAirModeVar('track');
  });

  it('dj mode with both dj_name and active_dj_id renders an inline avatar', async () => {
    globalThis.__setServerState(baseState({
      station: {
        name: 'Test FM', tagline: 't', dj_name: 'Ms. Jessica Danger',
        active_dj_id: 'dj-jess-123',
      },
    }));

    globalThis.setOnAirMode('dj');
    // The roll-out animation flips innerHTML at the midpoint; give it a
    // couple of microtask ticks to settle.
    await flush();
    await flush();

    const el = document.getElementById('nowPlaying');
    expect(el.dataset.mode).toBe('dj');
    const avatar = el.querySelector('.badge-avatar.dj-avatar.dj-avatar-xs');
    expect(avatar).toBeTruthy();
    const img = avatar.querySelector('img');
    expect(img.getAttribute('src')).toContain('/media/dj-icon/dj-jess-123');
    // onerror self-removes so the placeholder background stays visible if
    // the DJ has no avatar generated yet.
    expect(img.getAttribute('onerror')).toContain('this.remove()');
    // Text content includes the DJ's name (textContent strips the markup).
    expect(el.textContent).toContain('Ms. Jessica Danger');
    expect(el.textContent).toContain('On air with');
  });

  it('dj mode without active_dj_id falls back to plain text (no avatar)', async () => {
    // Default-DJ slot: dj_name is set (the station's own DJ takes over) but
    // active_dj_id is null because no Show's DJ is hosting. We render the
    // plain text badge — no avatar to slot in.
    globalThis.__setServerState(baseState({
      station: { name: 'Test FM', tagline: 't', dj_name: 'Default Dan', active_dj_id: null },
    }));

    globalThis.setOnAirMode('dj');
    await flush();
    await flush();

    const el = document.getElementById('nowPlaying');
    expect(el.querySelector('.badge-avatar')).toBeNull();
    expect(el.textContent).toContain('On air with Default Dan');
  });

  it('dj mode with no dj_name at all says plain "On air"', async () => {
    globalThis.__setServerState(baseState({
      station: { name: 'Test FM', tagline: 't' },  // no dj_name, no active_dj_id
    }));

    globalThis.setOnAirMode('dj');
    await flush();
    await flush();

    const el = document.getElementById('nowPlaying');
    expect(el.querySelector('.badge-avatar')).toBeNull();
    expect(el.textContent).toContain('On air');
    expect(el.textContent).not.toContain('with');
  });

  it('non-dj modes do not render an avatar', async () => {
    globalThis.__setServerState(baseState({
      station: { name: 'Test FM', tagline: 't', dj_name: 'Sam', active_dj_id: 'dj-sam' },
    }));

    globalThis.setOnAirMode('ad');
    await flush(); await flush();
    let el = document.getElementById('nowPlaying');
    expect(el.querySelector('.badge-avatar')).toBeNull();
    expect(el.textContent).toContain('Ad break');

    globalThis.setOnAirMode('news');
    await flush(); await flush();
    el = document.getElementById('nowPlaying');
    expect(el.querySelector('.badge-avatar')).toBeNull();
    expect(el.textContent).toContain('News');
  });

  it('escapes the dj name so HTML injection in dj_name is harmless', async () => {
    // dj_name flows from the config (user-editable). The badge interpolates
    // it into innerHTML when an avatar is present, so we have to escape it
    // — otherwise a DJ literally named `<script>alert(1)</script>` would
    // execute.
    globalThis.__setServerState(baseState({
      station: {
        name: 'Test FM', tagline: 't',
        dj_name: '<img src=x onerror=alert(1)>',
        active_dj_id: 'dj-attack',
      },
    }));

    globalThis.setOnAirMode('dj');
    await flush(); await flush();

    const el = document.getElementById('nowPlaying');
    // The escaped form lands in textContent as the literal characters.
    // Critically: no extra <img> element from the injection attempt.
    const imgs = el.querySelectorAll('img');
    // Only the legitimate avatar img — not the injected one.
    expect(imgs.length).toBe(1);
    expect(imgs[0].getAttribute('src')).toContain('/media/dj-icon/dj-attack');
  });
});

// Global stubs that the UI code (app.js) expects from the browser but
// happy-dom doesn't ship out of the box. Each one is a minimal fake with
// just enough surface to keep app.js from crashing during init/load.
//
// Individual tests can override these with vi.spyOn(...) or vi.fn() to
// assert behaviour — these stubs only exist so the module can be loaded
// at all.

import { vi } from 'vitest';

// ── AudioContext ────────────────────────────────────────────────────────────
// Only constructed when the user first clicks Play, but we stub it anyway
// so unit tests for playback paths can opt in.
class FakeAudioParam {
  value = 1;
  setValueAtTime = vi.fn();
  cancelScheduledValues = vi.fn();
  linearRampToValueAtTime = vi.fn();
}
class FakeGainNode {
  gain = new FakeAudioParam();
  connect = vi.fn();
}
class FakeBufferSource {
  buffer = null;
  connect = vi.fn();
  start = vi.fn();
}
globalThis.AudioContext = class FakeAudioContext {
  constructor() {
    this.currentTime = 0;
    this.destination = {};
  }
  createGain() { return new FakeGainNode(); }
  createMediaElementSource() { return { connect: vi.fn() }; }
  createBufferSource() { return new FakeBufferSource(); }
  suspend() { return Promise.resolve(); }
  resume() { return Promise.resolve(); }
  decodeAudioData() { return Promise.resolve({ duration: 2 }); }
};

// ── HTMLMediaElement ────────────────────────────────────────────────────────
// happy-dom provides Audio but methods like play() / load() are missing.
if (typeof globalThis.Audio !== 'undefined') {
  globalThis.Audio.prototype.play = function () { return Promise.resolve(); };
  globalThis.Audio.prototype.pause = function () {};
  globalThis.Audio.prototype.load = function () {};
}

// ── Element.animate() (Web Animations API) ──────────────────────────────────
// Used by animateLabel to slide text in/out. happy-dom doesn't implement it.
if (typeof globalThis.Element !== 'undefined' && !globalThis.Element.prototype.animate) {
  globalThis.Element.prototype.animate = function () {
    return { finished: Promise.resolve(), cancel: () => {} };
  };
}

// ── fetch() default ────────────────────────────────────────────────────────
// Returns sensible empty-shape payloads for the endpoints init() touches at
// module load time. Tests that exercise specific endpoints replace this with
// their own vi.fn().
function fakeJsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

globalThis.__fakeJsonResponse = fakeJsonResponse;

globalThis.fetch = vi.fn(async (url) => {
  const path = String(url).split('?')[0];
  if (path.endsWith('/config')) {
    return fakeJsonResponse({
      music_folder: '/test/music',
      station: { name: 'Test FM', dj_name: 'Test DJ', dj_roster: [] },
      alerts: {},
    });
  }
  if (path.endsWith('/player/status')) {
    return fakeJsonResponse({
      is_playing: false,
      volume: 80,
      station: { name: 'Test FM' },
    });
  }
  if (path.endsWith('/library/status')) {
    return fakeJsonResponse({ track_count: 0, last_scan_at: null });
  }
  return fakeJsonResponse(null, 204);
});

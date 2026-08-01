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
// Drives the spectrum analyser. getByteFrequencyData fills the caller's array
// from `fakeSpectrum` so tests can pin what the bars are reading; the default
// is silence. frequencyBinCount is fftSize/2, matching the real node.
class FakeAnalyserNode {
  fftSize = 2048;
  smoothingTimeConstant = 0.8;
  fakeSpectrum = null;
  connect = vi.fn();
  get frequencyBinCount() { return this.fftSize / 2; }
  getByteFrequencyData(target) {
    for (let i = 0; i < target.length; i++) {
      target[i] = this.fakeSpectrum ? (this.fakeSpectrum[i] ?? 0) : 0;
    }
  }
}
globalThis.AudioContext = class FakeAudioContext {
  constructor() {
    this.currentTime = 0;
    this.sampleRate = 48000;
    this.destination = {};
  }
  createGain() { return new FakeGainNode(); }
  createAnalyser() { return new FakeAnalyserNode(); }
  createMediaElementSource() { return { connect: vi.fn() }; }
  createBufferSource() { return new FakeBufferSource(); }
  createDynamicsCompressor() {
    return {
      threshold: { value: 0 },
      knee: { value: 0 },
      ratio: { value: 1 },
      attack: { value: 0 },
      release: { value: 0 },
      connect: vi.fn(),
    };
  }
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

// ── Canvas 2D context ───────────────────────────────────────────────────────
// happy-dom has no canvas rendering, so getContext('2d') returns null and the
// analyser's draw would bail before doing anything. Stub the handful of 2D
// calls _drawAnalyserFrame makes; the drawing itself isn't asserted on (the
// interesting logic is computeBands, which is pure), but the draw path has to
// run without throwing so the loop/lifecycle tests are meaningful.
if (typeof globalThis.HTMLCanvasElement !== 'undefined') {
  globalThis.HTMLCanvasElement.prototype.getContext = function () {
    return {
      clearRect: vi.fn(),
      fillRect: vi.fn(),
      createLinearGradient: () => ({ addColorStop: vi.fn() }),
      set fillStyle(_v) {},
      get fillStyle() { return '#000'; },
    };
  };
  // clientWidth/clientHeight are 0 in happy-dom (no layout). The draw bails on
  // a zero-sized box, so give the canvas a plausible laid-out size.
  Object.defineProperty(globalThis.HTMLCanvasElement.prototype, 'clientWidth', {
    configurable: true, get() { return 480; },
  });
  Object.defineProperty(globalThis.HTMLCanvasElement.prototype, 'clientHeight', {
    configurable: true, get() { return 56; },
  });
}

// ── requestAnimationFrame ───────────────────────────────────────────────────
// Overrides happy-dom's timer-backed implementation on purpose. The analyser
// loop re-arms itself every frame, so anything that fires callbacks on its own
// either recurses forever or leaks a live loop between test files. This
// version only queues; tests pump frames explicitly via __rafCallbacks.
// app.js's analyser loop is the only rAF user in the codebase.
{
  let _rafId = 0;
  globalThis.__rafCallbacks = new Map();
  globalThis.requestAnimationFrame = (cb) => {
    const id = ++_rafId;
    globalThis.__rafCallbacks.set(id, cb);
    return id;
  };
  globalThis.cancelAnimationFrame = (id) => {
    globalThis.__rafCallbacks.delete(id);
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
    // For clients that fetch the response as bytes (e.g. preview audio bytes
    // piped through AudioContext.decodeAudioData). The real bytes don't matter
    // because decodeAudioData is itself stubbed.
    arrayBuffer: async () => new ArrayBuffer(0),
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

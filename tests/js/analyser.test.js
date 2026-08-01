import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs } from './_loadApp.js';

// ── computeBands ─────────────────────────────────────────────────────────────
// The pure half of the analyser: linear FFT bins in, log-spaced display bands
// out. Everything visually interesting about the display depends on this
// mapping being right, and it needs no AudioContext to test.

const SAMPLE_RATE = 48000;
const BIN_COUNT = 1024;           // fftSize 2048 → 1024 bins
const BIN_WIDTH = (SAMPLE_RATE / 2) / BIN_COUNT;  // 23.4375 Hz

/** Build a spectrum with a single loud bin at the bin covering `hz`. */
function spectrumWithToneAt(hz, level = 255) {
  const data = new Uint8Array(BIN_COUNT);
  data[Math.floor(hz / BIN_WIDTH)] = level;
  return data;
}

describe('computeBands', () => {
  beforeEach(() => { loadAppJs(); });

  it('returns exactly bandCount values', () => {
    const bands = globalThis.computeBands(new Uint8Array(BIN_COUNT), SAMPLE_RATE, 24);
    expect(bands).toHaveLength(24);
  });

  it('returns all zeros for a silent spectrum', () => {
    const bands = globalThis.computeBands(new Uint8Array(BIN_COUNT), SAMPLE_RATE, 24);
    expect(bands.every(v => v === 0)).toBe(true);
  });

  it('normalises byte levels to 0..1', () => {
    // Every bin at full scale → every band should read 1.0.
    const data = new Uint8Array(BIN_COUNT).fill(255);
    const bands = globalThis.computeBands(data, SAMPLE_RATE, 24);
    expect(bands.every(v => v > 0.99 && v <= 1)).toBe(true);
  });

  it('puts a low tone in a low band and a high tone in a high band', () => {
    const bass = globalThis.computeBands(spectrumWithToneAt(60), SAMPLE_RATE, 24);
    const treble = globalThis.computeBands(spectrumWithToneAt(12000), SAMPLE_RATE, 24);

    const loudest = (bands) => bands.indexOf(Math.max(...bands));
    expect(loudest(bass)).toBeLessThan(4);
    expect(loudest(treble)).toBeGreaterThan(19);
  });

  it('spaces bands logarithmically, not linearly', () => {
    // The whole point of the log mapping: 440 Hz and 880 Hz are one octave
    // apart and must land in *different* bands, even though they're only 440 Hz
    // apart — a linear mapping over 40..16000 Hz would put both in the first
    // couple of bands and waste most of the display.
    const loudest = (hz) => {
      const bands = globalThis.computeBands(spectrumWithToneAt(hz), SAMPLE_RATE, 24);
      return bands.indexOf(Math.max(...bands));
    };
    const a4 = loudest(440);
    const a5 = loudest(880);
    expect(a5).toBeGreaterThan(a4);

    // And the same octave ratio should span a similar number of bands wherever
    // it sits — that's what "logarithmic" buys us.
    const a2 = loudest(110);
    const a3 = loudest(220);
    expect(Math.abs((a5 - a4) - (a3 - a2))).toBeLessThanOrEqual(1);
  });

  it('gives the lowest bands at least one bin instead of dividing by zero', () => {
    // Bands below ~23 Hz wide are narrower than a single 23.4 Hz bin. Those
    // must still produce a finite number rather than NaN from a 0-width range.
    const bands = globalThis.computeBands(new Uint8Array(BIN_COUNT).fill(128), SAMPLE_RATE, 64);
    expect(bands.every(v => Number.isFinite(v))).toBe(true);
    expect(bands.every(v => v > 0)).toBe(true);
  });

  it('degrades to zeros on empty or nonsense input rather than throwing', () => {
    expect(globalThis.computeBands(null, SAMPLE_RATE, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(0), SAMPLE_RATE, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(BIN_COUNT), 0, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(BIN_COUNT), SAMPLE_RATE, 0)).toEqual([]);
  });
});

// ── Draw loop lifecycle ──────────────────────────────────────────────────────
// Same failure class as the stale autoTrigger timers fixed in #166: a queued
// animation frame that lands after stop must not resurrect the loop.

describe('analyser draw loop', () => {
  beforeEach(() => {
    loadAppJs();   // body fixture comes from the real index.html, canvas included
    globalThis.__rafCallbacks?.clear();
    globalThis.initAudio();
  });

  afterEach(() => {
    globalThis.stopAnalyser({ clear: true });
    globalThis.__rafCallbacks?.clear();
  });

  /** Run exactly one queued animation frame. */
  function pumpFrame() {
    const [id, cb] = [...globalThis.__rafCallbacks.entries()][0] ?? [];
    if (cb) {
      globalThis.__rafCallbacks.delete(id);
      cb();
    }
    return Boolean(cb);
  }

  it('initAudio wires an analyser and sizes its bin buffer', () => {
    const a = globalThis.__getAnalyser();
    expect(a).not.toBeNull();
    expect(a.fftSize).toBe(2048);
    expect(a.frequencyBinCount).toBe(1024);
  });

  it('startAnalyser marks the canvas live and queues a frame', () => {
    globalThis.startAnalyser();
    expect(document.getElementById('analyser').classList.contains('live')).toBe(true);
    expect(globalThis.__getAnalyserRaf()).not.toBeNull();
  });

  it('keeps re-arming itself frame after frame while running', () => {
    globalThis.startAnalyser();
    expect(pumpFrame()).toBe(true);
    // The frame it just ran should have queued the next one.
    expect(globalThis.__rafCallbacks.size).toBe(1);
    expect(pumpFrame()).toBe(true);
    expect(globalThis.__rafCallbacks.size).toBe(1);
  });

  it('stopAnalyser cancels the pending frame', () => {
    globalThis.startAnalyser();
    globalThis.stopAnalyser();
    expect(globalThis.__getAnalyserRaf()).toBeNull();
    expect(globalThis.__rafCallbacks.size).toBe(0);
  });

  it('a frame queued before stop does not resurrect the loop (generation guard)', () => {
    // The race: rAF fires the callback from the browser's frame queue, so a
    // callback can already be in flight when stopPlayback runs. Without the
    // generation token it would redraw and re-arm, leaving a live draw loop
    // after playback stopped.
    globalThis.startAnalyser();
    const [, inFlight] = [...globalThis.__rafCallbacks.entries()][0];
    globalThis.__rafCallbacks.clear();   // simulate: already dequeued, about to run

    globalThis.stopAnalyser();
    inFlight();                          // the stale frame lands *after* stop

    expect(globalThis.__rafCallbacks.size).toBe(0);
    expect(globalThis.__getAnalyserRaf()).toBeNull();
  });

  it('restarting supersedes the previous loop instead of running two', () => {
    globalThis.startAnalyser();
    const [, firstLoop] = [...globalThis.__rafCallbacks.entries()][0];
    globalThis.__rafCallbacks.clear();

    globalThis.startAnalyser();          // e.g. resume after pause
    expect(globalThis.__rafCallbacks.size).toBe(1);

    // The first loop's in-flight frame is now stale and must not add a second.
    firstLoop();
    expect(globalThis.__rafCallbacks.size).toBe(1);
  });

  it('stop with clear:true hides the canvas; plain stop leaves it visible', () => {
    const canvas = document.getElementById('analyser');

    globalThis.startAnalyser();
    globalThis.stopAnalyser();           // pause: frozen, still on screen
    expect(canvas.classList.contains('live')).toBe(true);

    globalThis.startAnalyser();
    globalThis.stopAnalyser({ clear: true });  // stop: gone
    expect(canvas.classList.contains('live')).toBe(false);
  });

  it('peak caps decay toward zero once the signal drops out', () => {
    const analyser = globalThis.__getAnalyser();
    // A loud frame to push the caps up…
    analyser.fakeSpectrum = new Uint8Array(1024).fill(255);
    globalThis._drawAnalyserFrame();
    const loud = [...globalThis.__getAnalyserPeaks()];
    expect(Math.max(...loud)).toBeGreaterThan(0.9);

    // …then silence. Caps should fall, but gradually — not snap to zero.
    analyser.fakeSpectrum = new Uint8Array(1024);
    globalThis._drawAnalyserFrame();
    const afterOne = [...globalThis.__getAnalyserPeaks()];
    expect(Math.max(...afterOne)).toBeLessThan(Math.max(...loud));
    expect(Math.max(...afterOne)).toBeGreaterThan(0.8);

    for (let i = 0; i < 200; i++) globalThis._drawAnalyserFrame();
    expect(Math.max(...globalThis.__getAnalyserPeaks())).toBe(0);
  });

  it('draws without throwing when the canvas is missing from the DOM', () => {
    // Remove only the canvas — wiping the whole body would also delete the
    // elements init()'s in-flight fetch chain writes into, and the resulting
    // unhandled rejection would surface as a confusing failure elsewhere.
    document.getElementById('analyser').remove();
    expect(() => globalThis._drawAnalyserFrame()).not.toThrow();
  });
});

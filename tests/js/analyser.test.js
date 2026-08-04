import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs } from './_loadApp.js';

// ── computeBands ─────────────────────────────────────────────────────────────
// The pure half of the analyser: linear FFT bins in, log-spaced display bands
// out. Everything visually interesting about the display depends on this
// mapping being right, and it needs no AudioContext to test.

const SAMPLE_RATE = 48000;
const BIN_COUNT = 2048;           // fftSize 4096 → 2048 bins
const BIN_WIDTH = (SAMPLE_RATE / 2) / BIN_COUNT;  // 11.72 Hz

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
    // At a high band count the lowest bands are narrower than a single bin.
    // Those must still produce a finite number rather than NaN from a
    // zero-width bin range.
    const bands = globalThis.computeBands(new Uint8Array(BIN_COUNT).fill(128), SAMPLE_RATE, 64);
    expect(bands.every(v => Number.isFinite(v))).toBe(true);
    expect(bands.every(v => v > 0)).toBe(true);
  });

  it('takes the peak of a band, not the mean', () => {
    // This is the fix for the display reading "flat" at the top end. The
    // highest band spans ~350 bins; averaging buries a single tonal peak
    // under all the quiet bins around it, so the treble bars sat low and
    // barely moved no matter what the music did. Peak-of-band tracks the
    // loudest content instead — which is also what a hardware bargraph's
    // band-pass-plus-peak-detector approximates.
    const data = new Uint8Array(BIN_COUNT);
    data[1195] = 255;   // 14 kHz — one loud bin inside the 353-bin top band

    const bands = globalThis.computeBands(data, SAMPLE_RATE, 20);

    // Peak-of-band lights it fully. The mean over those 353 bins would have
    // been 0.0028 — under one pixel of a 90px display, i.e. invisible.
    expect(bands[19]).toBe(1);
  });

  it('degrades to zeros on empty or nonsense input rather than throwing', () => {
    expect(globalThis.computeBands(null, SAMPLE_RATE, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(0), SAMPLE_RATE, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(BIN_COUNT), 0, 8).every(v => v === 0)).toBe(true);
    expect(globalThis.computeBands(new Uint8Array(BIN_COUNT), SAMPLE_RATE, 0)).toEqual([]);
  });
});

// ── Level zones ──────────────────────────────────────────────────────────────
// Classic bargraphs escalate colour toward the top of the scale. The station's
// on-air colour holds the low zone so the display still reads pink/orange/blue
// at normal levels; amber and red mark the hot end.

describe('analyserZoneColour', () => {
  beforeEach(() => { loadAppJs(); });

  const AMBER = '#fbbf24';
  const RED   = '#ef4444';
  const PINK  = '#f472b6';

  it('keeps the on-air colour through the low zone', () => {
    expect(globalThis.analyserZoneColour(0, PINK)).toBe(PINK);
    expect(globalThis.analyserZoneColour(0.3, PINK)).toBe(PINK);
    expect(globalThis.analyserZoneColour(0.61, PINK)).toBe(PINK);
  });

  it('escalates to amber in the mid zone', () => {
    expect(globalThis.analyserZoneColour(0.62, PINK)).toBe(AMBER);
    expect(globalThis.analyserZoneColour(0.8, PINK)).toBe(AMBER);
  });

  it('escalates to red in the hot zone', () => {
    expect(globalThis.analyserZoneColour(0.86, PINK)).toBe(RED);
    expect(globalThis.analyserZoneColour(1, PINK)).toBe(RED);
  });

  it('escalates the same way regardless of the mode colour', () => {
    // The hot end means "loud", not "which segment is on air" — so ads and
    // news hit the same amber/red, only their low zone differs.
    for (const low of ['#f472b6', '#fb923c', '#60a5fa']) {
      expect(globalThis.analyserZoneColour(0.2, low)).toBe(low);
      expect(globalThis.analyserZoneColour(0.7, low)).toBe(AMBER);
      expect(globalThis.analyserZoneColour(0.95, low)).toBe(RED);
    }
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

  it('initAudio wires an analyser and configures it for the display', () => {
    const a = globalThis.__getAnalyser();
    expect(a).not.toBeNull();
    // fftSize is coupled to ANALYSER_BANDS: 4096 is only sufficient because
    // there are 20 bands. Pinned here so raising the band count without
    // revisiting the FFT size trips a test rather than quietly reintroducing
    // the correlated-low-end problem.
    expect(a.fftSize).toBe(4096);
    expect(a.frequencyBinCount).toBe(2048);
    // Raised off the -100 dB default so quiet bands go dark instead of
    // sitting permanently part-lit on the noise floor.
    expect(a.minDecibels).toBe(-80);
    expect(a.smoothingTimeConstant).toBeLessThan(0.75);
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

  it('peak markers jump straight to a new high', () => {
    const analyser = globalThis.__getAnalyser();
    analyser.fakeSpectrum = new Uint8Array(BIN_COUNT).fill(255);
    globalThis._drawAnalyserFrame();
    expect(Math.max(...globalThis.__getAnalyserPeaks())).toBeGreaterThan(0.9);
  });

  /** Push the markers to full scale, then let the signal drop out. */
  function peakThenSilence() {
    const analyser = globalThis.__getAnalyser();
    analyser.fakeSpectrum = new Uint8Array(BIN_COUNT).fill(255);
    globalThis._drawAnalyserFrame(0);
    const high = Math.max(...globalThis.__getAnalyserPeaks());
    analyser.fakeSpectrum = new Uint8Array(BIN_COUNT);
    return high;
  }

  /** Advance `ms` of wall time in `steps` frames. */
  function advance(ms, steps = 10) {
    for (let i = 0; i < steps; i++) globalThis._drawAnalyserFrame(ms / steps);
  }

  it('peak markers hold at the high-water mark before sinking', () => {
    // The hold is what makes the marker read as a deliberate indicator rather
    // than the bar lagging: it parks at the transient's height for the better
    // part of a second, so you actually see where the signal got to.
    const high = peakThenSilence();
    advance(500);
    // Still parked at the high-water mark, even though the bars are at zero.
    expect(Math.max(...globalThis.__getAnalyserPeaks())).toBe(high);
  });

  it('peak markers sink to zero once the hold expires', () => {
    const high = peakThenSilence();

    advance(1000);   // past the hold, partway down
    const sinking = Math.max(...globalThis.__getAnalyserPeaks());
    expect(sinking).toBeLessThan(high);
    expect(sinking).toBeGreaterThan(0);

    advance(3000);
    expect(Math.max(...globalThis.__getAnalyserPeaks())).toBe(0);
  });

  it('decays on wall time, not frame count', () => {
    // Regression for the 120 Hz bug: decay used to be a fixed drop per frame,
    // so on a ProMotion display (twice the frames per second) the marker fell
    // in half the time. The same elapsed time must produce the same fall
    // regardless of how many frames it was split across.
    const high = peakThenSilence();
    advance(1400, 7);                       // 7 long frames
    const fewFrames = Math.max(...globalThis.__getAnalyserPeaks());

    globalThis.stopAnalyser({ clear: true });   // reset markers
    const high2 = peakThenSilence();
    advance(1400, 84);                      // same 1400 ms, 12x the frames
    const manyFrames = Math.max(...globalThis.__getAnalyserPeaks());

    expect(high2).toBe(high);
    expect(manyFrames).toBeCloseTo(fewFrames, 5);
  });

  it('draws without throwing when the canvas is missing from the DOM', () => {
    // Remove only the canvas — wiping the whole body would also delete the
    // elements init()'s in-flight fetch chain writes into, and the resulting
    // unhandled rejection would surface as a confusing failure elsewhere.
    document.getElementById('analyser').remove();
    expect(() => globalThis._drawAnalyserFrame()).not.toThrow();
  });
});

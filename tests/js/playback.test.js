// Tests for playback timing logic in app/ui/app.js.
//
// This is the area with the most historical UX bugs:
//   - pause-after-refresh (ctx null guard)
//   - mode-timer state (audioTime preserved through pause/resume)
//   - auto-trigger rescheduling
//   - badge text correctness
//
// All fake-timer tests install vi.useFakeTimers() in beforeEach and restore
// real timers in afterEach so state doesn't leak between tests.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeFakeCtx(currentTime = 0) {
  return {
    currentTime,
    suspend: vi.fn(() => Promise.resolve()),
    resume:  vi.fn(() => Promise.resolve()),
  };
}

// Set up a fake active audio slot so curSlot() returns something usable.
function stubActiveSlot({ currentTime = 0, duration = 300 } = {}) {
  globalThis.__setActiveSlot('A');
  globalThis.__setSlots({
    A: {
      el: {
        currentTime,
        duration,
        pause: vi.fn(),
        src: '',
      },
      gainNode: {
        gain: {
          value: 1,
          cancelScheduledValues: vi.fn(),
          setValueAtTime: vi.fn(),
          linearRampToValueAtTime: vi.fn(),
        },
      },
    },
  });
}

// ── Mode timers: scheduleMode / clearModeTimers / setOnAirMode ───────────────

describe('scheduleMode', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('creates one entry in _modeTimers with audioTime, mode, id, label', () => {
    globalThis.scheduleMode('dj', 1000);
    const timers = globalThis.__getModeTimers();
    expect(timers).toHaveLength(1);
    const entry = timers[0];
    expect(entry.mode).toBe('dj');
    expect(typeof entry.id).not.toBe('undefined');
    expect(entry.id).not.toBeNull();
    expect(typeof entry.audioTime).toBe('number');
    // label defaults to null
    expect(entry.label).toBeNull();
  });

  it('accumulates multiple entries', () => {
    globalThis.scheduleMode('dj',   1000);
    globalThis.scheduleMode('ad',   2000);
    globalThis.scheduleMode('news', 3000);
    expect(globalThis.__getModeTimers()).toHaveLength(3);
  });
});

describe('clearModeTimers', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('empties _modeTimers after scheduling several modes', () => {
    globalThis.scheduleMode('dj',   500);
    globalThis.scheduleMode('ad',   1000);
    globalThis.scheduleMode('news', 1500);
    expect(globalThis.__getModeTimers()).toHaveLength(3);

    globalThis.clearModeTimers();
    expect(globalThis.__getModeTimers()).toHaveLength(0);
  });
});

describe('scheduleMode firing', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('timer firing flips onAirMode and updates #nowPlaying data-mode', async () => {
    expect(globalThis.__getOnAirMode()).toBe('track');

    globalThis.scheduleMode('dj', 100);
    expect(globalThis.__getModeTimers()).toHaveLength(1);

    vi.advanceTimersByTime(100);
    // animateLabel is async (awaits finished promise); the mode + data-mode
    // are set synchronously inside setOnAirMode before the animation.
    expect(globalThis.__getOnAirMode()).toBe('dj');
    expect(document.getElementById('nowPlaying').dataset.mode).toBe('dj');

    // The timer should have removed itself from the list.
    expect(globalThis.__getModeTimers()).toHaveLength(0);
  });
});

// ── setOnAirMode badge text ──────────────────────────────────────────────────

describe('setOnAirMode', () => {
  beforeEach(() => {
    loadAppJs();
  });

  it("setOnAirMode('dj') sets data-mode='dj' and queues '🎙️ On air' text", () => {
    globalThis.setOnAirMode('dj');
    const el = document.getElementById('nowPlaying');
    expect(el.dataset.mode).toBe('dj');
    // animateLabel is async — it sets textContent after the out-animation.
    // In happy-dom the animation finishes immediately so textContent updates
    // on the microtask after this call. We rely on the data-mode being set
    // synchronously and check onAirMode.
    expect(globalThis.__getOnAirMode()).toBe('dj');
  });

  it("setOnAirMode('ad') sets data-mode='ad' and onAirMode='ad'", () => {
    globalThis.setOnAirMode('ad');
    expect(globalThis.__getOnAirMode()).toBe('ad');
    expect(document.getElementById('nowPlaying').dataset.mode).toBe('ad');
  });

  it("setOnAirMode('news') sets data-mode='news' and onAirMode='news'", () => {
    globalThis.setOnAirMode('news');
    expect(globalThis.__getOnAirMode()).toBe('news');
    expect(document.getElementById('nowPlaying').dataset.mode).toBe('news');
  });

  it("setOnAirMode('track', label) uses the label arg for onAirMode='track'", () => {
    globalThis.setOnAirMode('track', 'Some Artist - Song');
    expect(globalThis.__getOnAirMode()).toBe('track');
    expect(document.getElementById('nowPlaying').dataset.mode).toBe('track');
  });
});

// ── clearAutoTrigger / scheduleAutoTrigger ────────────────────────────────────

describe('clearAutoTrigger', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('nulls autoTriggerTimer after scheduling', () => {
    stubActiveSlot({ currentTime: 0, duration: 300 });
    globalThis.scheduleAutoTrigger(300);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();

    globalThis.clearAutoTrigger();
    expect(globalThis.__getAutoTriggerTimer()).toBeNull();
  });
});

describe('scheduleAutoTrigger', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('sets a non-null autoTriggerTimer for a 300s track', () => {
    stubActiveSlot({ currentTime: 0, duration: 300 });
    globalThis.scheduleAutoTrigger(300);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();
  });

  it('fires triggerTransition after (duration - elapsed - AUTO_PREROLL_S) seconds', () => {
    // AUTO_PREROLL_S = 10; duration=300, elapsed=0 → delay = 290s
    // Provide a fake ctx so triggerTransition's sync path doesn't blow up on
    // ctx.currentTime accesses inside altSlot handling.
    const fakeCtx = makeFakeCtx();
    globalThis.__setCtx(fakeCtx);
    stubActiveSlot({ currentTime: 0, duration: 300 });
    // triggerTransition now defensively bails if serverState says not playing
    // (lid-wake bug guard). Set is_playing so this test's trigger fires normally.
    globalThis.__setServerState({ is_playing: true });

    // Stub /player/next to return 400 (end-of-queue) so triggerTransition
    // calls stopPlayback() and exits cleanly without dereferencing a null
    // response body.
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url, opts = {}) => {
      if (String(url).includes('/player/next')) {
        return {
          ok: false,
          status: 400,
          headers: { get: () => 'application/json' },
          json: async () => null,
          text: async () => '400',
        };
      }
      return origFetch(url, opts);
    });

    // triggerTransition is not exported; we detect firing by checking that
    // autoTriggerTimer gets cleared (clearAutoTrigger is called at the top
    // of triggerTransition).
    globalThis.scheduleAutoTrigger(300);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();

    // Advance just under the delay — timer should NOT have fired.
    vi.advanceTimersByTime(289_000);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();

    // Advance past the trigger point.
    vi.advanceTimersByTime(2_000);
    // triggerTransition calls clearAutoTrigger() as its first action,
    // which nulls the timer.
    expect(globalThis.__getAutoTriggerTimer()).toBeNull();
  });

  it('autoTrigger firing while serverState.is_playing=false is suppressed (lid-wake guard)', () => {
    // Repro for the lid-wake bug class: a stale timer survives stopPlayback
    // and fires later. Even if the timer fires, triggerTransition should bail
    // out because the server says we're stopped.
    globalThis.__setCtx(makeFakeCtx());
    stubActiveSlot({ currentTime: 0, duration: 300 });
    globalThis.__setServerState({ is_playing: true });

    globalThis.scheduleAutoTrigger(300);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();

    // User hits Stop — serverState flips to not playing, but a stale timer
    // is still in flight (in this synthetic case we just override state
    // without calling stopPlayback's timer cleanup).
    globalThis.__setServerState({ is_playing: false });

    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.advanceTimersByTime(291_000);  // fire the timer

    // The defensive guard inside triggerTransition warned and returned early,
    // so the timer ID was NEVER cleared (clearAutoTrigger is the first line
    // AFTER the guard). The bare setTimeout callback fired but did nothing
    // playback-shaped.
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('triggerTransition blocked'),
      expect.objectContaining({ reason: 'auto' }),
    );
    warn.mockRestore();
  });
});

// ── pausePlayback ─────────────────────────────────────────────────────────────

describe('pausePlayback', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('is a no-op (no throw, paused stays false) when ctx is null', async () => {
    globalThis.__setCtx(null);
    expect(globalThis.__getPaused()).toBe(false);
    await globalThis.pausePlayback();
    expect(globalThis.__getPaused()).toBe(false);
  });

  it('calls ctx.suspend() and sets paused=true when ctx exists', async () => {
    const fakeCtx = makeFakeCtx();
    globalThis.__setCtx(fakeCtx);

    await globalThis.pausePlayback();

    expect(fakeCtx.suspend).toHaveBeenCalledOnce();
    expect(globalThis.__getPaused()).toBe(true);
  });

  it('cancels autoTriggerTimer and sets _autoTriggerRemaining=true', async () => {
    const fakeCtx = makeFakeCtx();
    globalThis.__setCtx(fakeCtx);
    stubActiveSlot({ currentTime: 0, duration: 300 });

    globalThis.scheduleAutoTrigger(300);
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();

    await globalThis.pausePlayback();

    expect(globalThis.__getAutoTriggerTimer()).toBeNull();
    expect(globalThis.__getAutoTriggerRemaining()).toBe(true);
  });

  it('keeps _modeTimers entries with their audioTime after pausing', async () => {
    const fakeCtx = makeFakeCtx(5); // currentTime = 5
    globalThis.__setCtx(fakeCtx);

    // Schedule a mode 2s from now → audioTime = 5 + 2 = 7
    globalThis.scheduleMode('dj', 2000);
    const before = globalThis.__getModeTimers();
    expect(before).toHaveLength(1);
    const audioTimeBefore = before[0].audioTime;

    await globalThis.pausePlayback();

    // Entry must still exist with the original audioTime.
    const after = globalThis.__getModeTimers();
    expect(after).toHaveLength(1);
    expect(after[0].audioTime).toBe(audioTimeBefore);
  });
});

// ── resumePlayback ────────────────────────────────────────────────────────────

describe('resumePlayback', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reschedules mode timers using AudioContext currentTime on resume', async () => {
    const fakeCtx = makeFakeCtx(5);
    globalThis.__setCtx(fakeCtx);

    // Schedule a mode 2s from now → audioTime = 7.
    globalThis.scheduleMode('dj', 2000);

    await globalThis.pausePlayback();
    expect(globalThis.__getModeTimers()).toHaveLength(1);

    // Simulate time passing: AudioContext currentTime is now 6 (1s elapsed).
    fakeCtx.currentTime = 6;

    await globalThis.resumePlayback();

    // The mode timer should have been re-issued with a new id.
    const timers = globalThis.__getModeTimers();
    expect(timers).toHaveLength(1);
    // audioTime should be preserved.
    expect(timers[0].audioTime).toBe(7);
    // The timer should fire after the remaining ~1s delay.
    expect(globalThis.__getOnAirMode()).toBe('track'); // not yet
    vi.advanceTimersByTime(1100);
    expect(globalThis.__getOnAirMode()).toBe('dj');
  });

  it('reschedules autoTrigger from el.currentTime on resume', async () => {
    const fakeCtx = makeFakeCtx();
    globalThis.__setCtx(fakeCtx);
    stubActiveSlot({ currentTime: 0, duration: 300 });

    globalThis.scheduleAutoTrigger(300);
    await globalThis.pausePlayback();

    expect(globalThis.__getAutoTriggerRemaining()).toBe(true);
    expect(globalThis.__getAutoTriggerTimer()).toBeNull();

    await globalThis.resumePlayback();

    expect(globalThis.__getAutoTriggerRemaining()).toBeNull();
    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();
  });
});

// ── stopPlayback ──────────────────────────────────────────────────────────────

describe('stopPlayback', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    loadAppJs();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('clears all timers and resets paused/_autoTriggerRemaining', async () => {
    const fakeCtx = makeFakeCtx();
    globalThis.__setCtx(fakeCtx);
    stubActiveSlot({ currentTime: 0, duration: 300 });

    // Set up timers in all four slots.
    globalThis.scheduleAutoTrigger(300);
    globalThis.schedulePrefetch(300);
    globalThis.scheduleMode('dj', 1000);
    // Stinger timer is internal to triggerTransition; set paused manually.
    globalThis.__setPaused(true);

    expect(globalThis.__getAutoTriggerTimer()).not.toBeNull();
    expect(globalThis.__getPrefetchTimer()).not.toBeNull();
    expect(globalThis.__getModeTimers()).toHaveLength(1);

    await globalThis.stopPlayback();

    expect(globalThis.__getAutoTriggerTimer()).toBeNull();
    expect(globalThis.__getPrefetchTimer()).toBeNull();
    expect(globalThis.__getModeTimers()).toHaveLength(0);
    expect(globalThis.__getPaused()).toBe(false);
    expect(globalThis.__getAutoTriggerRemaining()).toBeNull();
  });
});

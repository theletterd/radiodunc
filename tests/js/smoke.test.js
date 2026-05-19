// Smoke test: prove the vitest + happy-dom + setup-file pipeline works.
// If this file passes, the test runner is wired up correctly and we can
// start adding real assertions in follow-up test files.

import { describe, it, expect, vi } from 'vitest';

describe('test infrastructure', () => {
  it('vitest runs and assertions work', () => {
    expect(2 + 2).toBe(4);
  });

  it('happy-dom provides the DOM', () => {
    expect(typeof document).toBe('object');
    const el = document.createElement('div');
    el.textContent = 'hi';
    expect(el.textContent).toBe('hi');
  });

  it('setup.js stubbed AudioContext', () => {
    const ctx = new AudioContext();
    expect(ctx.currentTime).toBe(0);
    const gain = ctx.createGain();
    expect(typeof gain.gain.setValueAtTime).toBe('function');
  });

  it('setup.js stubbed Element.animate', () => {
    const el = document.createElement('div');
    const anim = el.animate();
    expect(anim.finished).toBeInstanceOf(Promise);
    expect(typeof anim.cancel).toBe('function');
  });

  it('setup.js stubbed fetch with default config/status payloads', async () => {
    const r1 = await fetch('/config');
    const body = await r1.json();
    expect(body.station.name).toBe('Test FM');

    const r2 = await fetch('/player/status');
    const status = await r2.json();
    expect(status.is_playing).toBe(false);
  });

  it('per-test fetch overrides work via vi.fn', async () => {
    globalThis.fetch = vi.fn(async () => __fakeJsonResponse({ custom: true }));
    const r = await fetch('/anything');
    const body = await r.json();
    expect(body.custom).toBe(true);
  });
});

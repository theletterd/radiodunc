import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs } from './_loadApp.js';

// ── fmtClock ─────────────────────────────────────────────────────────────────

describe('fmtClock', () => {
  beforeEach(() => { loadAppJs(); });

  it('formats under a minute with a leading zero on seconds', () => {
    expect(globalThis.fmtClock(0)).toBe('0:00');
    expect(globalThis.fmtClock(7)).toBe('0:07');
    expect(globalThis.fmtClock(59)).toBe('0:59');
  });

  it('formats minutes and seconds', () => {
    expect(globalThis.fmtClock(60)).toBe('1:00');
    expect(globalThis.fmtClock(175.4)).toBe('2:55');
  });

  it('switches to h:mm:ss past an hour', () => {
    // The library has podcast episodes in it, so this isn't hypothetical.
    expect(globalThis.fmtClock(3600)).toBe('1:00:00');
    expect(globalThis.fmtClock(3725)).toBe('1:02:05');
  });

  it('returns 0:00 for NaN, negative, or missing input', () => {
    // duration is NaN until the audio element loads metadata.
    expect(globalThis.fmtClock(NaN)).toBe('0:00');
    expect(globalThis.fmtClock(undefined)).toBe('0:00');
    expect(globalThis.fmtClock(-5)).toBe('0:00');
    expect(globalThis.fmtClock(Infinity)).toBe('0:00');
  });
});

// ── renderProgress ───────────────────────────────────────────────────────────

describe('renderProgress', () => {
  beforeEach(() => { loadAppJs(); });

  function stubSlot({ currentTime, duration }) {
    globalThis.__setActiveSlot('A');
    globalThis.__setSlots({ A: { el: { currentTime, duration }, gainNode: {} } });
  }

  const fill = () => document.getElementById('progressFill');
  const elapsed = () => document.getElementById('progressElapsed').textContent;
  const total = () => document.getElementById('progressTotal').textContent;

  it('scales the bar to the fraction played and writes both clocks', () => {
    stubSlot({ currentTime: 60, duration: 240 });
    globalThis.renderProgress();

    expect(fill().style.transform).toBe('scaleX(0.25)');
    expect(elapsed()).toBe('1:00');
    expect(total()).toBe('4:00');
  });

  it('shows an empty bar when there is no audio element at all', () => {
    // Before the first Play, and after Stop clears the slots.
    globalThis.__setSlots({ A: { el: null, gainNode: {} } });
    globalThis.renderProgress();

    expect(fill().style.transform).toBe('scaleX(0)');
    expect(elapsed()).toBe('0:00');
    expect(total()).toBe('0:00');
  });

  it('shows an empty bar while duration is still NaN', () => {
    // An <audio> reports NaN duration until metadata loads, which is the state
    // during the first moments of a transition.
    stubSlot({ currentTime: 0, duration: NaN });
    globalThis.renderProgress();

    expect(fill().style.transform).toBe('scaleX(0)');
    expect(total()).toBe('0:00');
  });

  it('clamps to the full bar if currentTime overshoots duration', () => {
    // Browsers can report currentTime a hair past duration at the very end.
    stubSlot({ currentTime: 201, duration: 200 });
    globalThis.renderProgress();

    expect(fill().style.transform).toBe('scaleX(1)');
    expect(elapsed()).toBe('3:20');
  });

  it('only touches the clock text when the displayed value changes', () => {
    // Called every animation frame, so the DOM write has to be conditional or
    // it thrashes 60-120x a second for a value that ticks once a second.
    stubSlot({ currentTime: 10.1, duration: 200 });
    globalThis.renderProgress();

    const el = document.getElementById('progressElapsed');
    const spy = vi.spyOn(el, 'textContent', 'set');

    stubSlot({ currentTime: 10.4, duration: 200 });   // same whole second
    globalThis.renderProgress();
    expect(spy).not.toHaveBeenCalled();

    stubSlot({ currentTime: 11.2, duration: 200 });   // ticks over
    globalThis.renderProgress();
    expect(spy).toHaveBeenCalledWith('0:11');
    spy.mockRestore();
  });
});

// ── Up Next hover card ───────────────────────────────────────────────────────

describe('trackCardHtml', () => {
  beforeEach(() => { loadAppJs(); });

  const full = {
    label: 'The Beatles - Penny Lane',
    file_path: '/Volumes/music/beatles/penny.mp3',
    title: 'Penny Lane', artist: 'The Beatles', album: 'Magical Mystery Tour',
    year: '1967', genre: 'Rock', duration_seconds: 175.4, bitrate: 320000,
  };

  it('renders every populated field', () => {
    const html = globalThis.trackCardHtml(full);
    expect(html).toContain('Penny Lane');
    expect(html).toContain('Magical Mystery Tour');
    expect(html).toContain('1967');
    expect(html).toContain('Rock');
  });

  it('formats duration as a clock and bitrate as kbps', () => {
    const html = globalThis.trackCardHtml(full);
    expect(html).toContain('2:55');
    expect(html).toContain('320 kbps');
  });

  it('omits rows the scanner found nothing for', () => {
    // The whole point of the card: sparsely-tagged rips are common, and empty
    // "Album: —" rows would be noise.
    const html = globalThis.trackCardHtml({
      file_path: '/m/unknown.mp3', title: null, artist: 'A',
      album: null, year: null, genre: '', duration_seconds: 100, bitrate: null,
    });
    expect(html).toContain('Artist');
    expect(html).not.toContain('Album');
    expect(html).not.toContain('Year');
    expect(html).not.toContain('Genre');
    expect(html).not.toContain('Bitrate');
  });

  it('always shows the file path, since it is the one field always present', () => {
    const html = globalThis.trackCardHtml({ file_path: '/m/mystery.mp3' });
    expect(html).toContain('/m/mystery.mp3');
    expect(html).toContain('track-card-path');
  });

  it('says so explicitly when there is no metadata at all', () => {
    const html = globalThis.trackCardHtml({ file_path: '/m/bare.mp3' });
    expect(html).toContain('No metadata for this file');
  });

  it('handles a track missing from the library (all fields null)', () => {
    // Queue items survive a track being deleted from the library.
    const html = globalThis.trackCardHtml({ label: 'Since Deleted', file_path: null });
    expect(html).toContain('No metadata for this file');
    expect(html).not.toContain('track-card-path');
  });

  it('escapes metadata rather than injecting it as markup', () => {
    const html = globalThis.trackCardHtml({
      title: '<img src=x onerror=alert(1)>',
      file_path: '/m/<script>.mp3',
    });
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;img');
  });
});

describe('showTrackCard / hideTrackCard', () => {
  beforeEach(() => { loadAppJs(); });

  function anchor({ right = 400, left = 100, top = 200 } = {}) {
    const li = document.createElement('li');
    document.body.appendChild(li);
    li.getBoundingClientRect = () => ({ right, left, top, bottom: top + 30, width: right - left, height: 30 });
    return li;
  }

  it('positions to the right of the row when there is room', () => {
    const card = document.getElementById('trackCard');
    card.getBoundingClientRect = () => ({ width: 300, height: 150 });
    globalThis.showTrackCard({ file_path: '/m/a.mp3' }, anchor({ right: 400 }));

    expect(card.hidden).toBe(false);
    expect(card.classList.contains('visible')).toBe(true);
    expect(parseInt(card.style.left, 10)).toBe(412);   // row.right + 12 gap
  });

  it('flips to the left of the row when it would overflow the viewport', () => {
    const card = document.getElementById('trackCard');
    card.getBoundingClientRect = () => ({ width: 300, height: 150 });
    // Row sits near the right edge; card would run off the screen.
    globalThis.showTrackCard({ file_path: '/m/a.mp3' }, anchor({ left: 600, right: 900 }));

    expect(parseInt(card.style.left, 10)).toBeLessThan(600);
  });

  it('lifts a card near the bottom so it stays on screen', () => {
    const card = document.getElementById('trackCard');
    card.getBoundingClientRect = () => ({ width: 300, height: 400 });
    globalThis.showTrackCard({ file_path: '/m/a.mp3' }, anchor({ top: window.innerHeight - 40 }));

    const top = parseInt(card.style.top, 10);
    expect(top + 400).toBeLessThanOrEqual(window.innerHeight);
  });

  it('hideTrackCard hides it again', () => {
    const card = document.getElementById('trackCard');
    card.getBoundingClientRect = () => ({ width: 300, height: 150 });
    globalThis.showTrackCard({ file_path: '/m/a.mp3' }, anchor());
    globalThis.hideTrackCard();

    expect(card.hidden).toBe(true);
    expect(card.classList.contains('visible')).toBe(false);
  });
});

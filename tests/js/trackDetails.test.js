import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

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

// ── Up Next right-click menu ─────────────────────────────────────────────────

describe('showQueueMenu / hideQueueMenu', () => {
  beforeEach(() => { loadAppJs(); });

  const menu = () => document.getElementById('queueMenu');

  it('renders one button per entry and fires its handler on click', () => {
    const picked = [];
    globalThis.showQueueMenu([
      { label: 'Move to top', onSelect: () => picked.push('top') },
    ], 100, 100);

    const items = menu().querySelectorAll('.context-menu-item');
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toBe('Move to top');

    items[0].click();
    expect(picked).toEqual(['top']);
  });

  it('closes itself when an entry is chosen', () => {
    globalThis.showQueueMenu([{ label: 'Go', onSelect: () => {} }], 50, 50);
    menu().querySelector('.context-menu-item').click();
    expect(menu().hidden).toBe(true);
  });

  it('renders a disabled entry that does nothing when clicked', () => {
    const picked = [];
    globalThis.showQueueMenu([
      { label: 'Move to top', disabled: true, onSelect: () => picked.push('top') },
    ], 50, 50);

    const btn = menu().querySelector('.context-menu-item');
    expect(btn.disabled).toBe(true);
    btn.click();
    expect(picked).toEqual([]);
  });

  it('clamps into the viewport when opened near an edge', () => {
    menu().getBoundingClientRect = () => ({ width: 200, height: 120 });
    globalThis.showQueueMenu(
      [{ label: 'Go', onSelect: () => {} }],
      window.innerWidth - 5, window.innerHeight - 5,
    );

    expect(parseInt(menu().style.left, 10) + 200).toBeLessThanOrEqual(window.innerWidth);
    expect(parseInt(menu().style.top, 10) + 120).toBeLessThanOrEqual(window.innerHeight);
  });

  it('opens at the cursor when there is room', () => {
    menu().getBoundingClientRect = () => ({ width: 200, height: 120 });
    globalThis.showQueueMenu([{ label: 'Go', onSelect: () => {} }], 120, 90);

    expect(menu().style.left).toBe('120px');
    expect(menu().style.top).toBe('90px');
  });

  it('replaces the previous entries rather than stacking them', () => {
    globalThis.showQueueMenu([{ label: 'First', onSelect: () => {} }], 10, 10);
    globalThis.showQueueMenu([{ label: 'Second', onSelect: () => {} }], 10, 10);

    const items = menu().querySelectorAll('.context-menu-item');
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toBe('Second');
  });

  it('hideQueueMenu hides it', () => {
    globalThis.showQueueMenu([{ label: 'Go', onSelect: () => {} }], 10, 10);
    globalThis.hideQueueMenu();
    expect(menu().hidden).toBe(true);
    expect(menu().classList.contains('visible')).toBe(false);
  });
});

describe('queue row context menu', () => {
  // Drives the real renderQueue path so the wiring is covered, not just the
  // menu primitives.
  const QUEUE = {
    queue_position: 2,
    queue_depth: 6,
    items: [
      { position: 3, track_id: 30, label: 'Next Up',  file_path: '/m/a.mp3' },
      { position: 4, track_id: 40, label: 'After It', file_path: '/m/b.mp3' },
    ],
  };

  let calls;

  beforeEach(async () => {
    calls = [];
    globalThis.fetch = vi.fn(async (url, opts = {}) => {
      const u = String(url);
      if (u.includes('/player/queue/reorder')) {
        calls.push(JSON.parse(opts.body));
        return { ok: true, status: 204, headers: { get: () => null }, text: async () => '' };
      }
      const body = u.includes('/player/queue') ? QUEUE : {};
      return {
        ok: true, status: 200,
        headers: { get: () => 'application/json' },
        json: async () => body,
        text: async () => JSON.stringify(body),
      };
    });
    loadAppJs();
    // Let init()'s async chain settle before seeding state — it assigns
    // serverState from /player/status and would otherwise clobber ours
    // partway through the test.
    await flush();
    globalThis.__setServerState({ is_playing: true });
    await globalThis.renderQueue();
  });

  function rightClick(index) {
    const row = document.querySelectorAll('#queueList li')[index];
    const ev = new MouseEvent('contextmenu', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'clientX', { value: 100 });
    Object.defineProperty(ev, 'clientY', { value: 100 });
    row.dispatchEvent(ev);
    return ev;
  }

  it('opens the menu and suppresses the browser default', () => {
    const ev = rightClick(1);
    expect(ev.defaultPrevented).toBe(true);
    expect(document.getElementById('queueMenu').hidden).toBe(false);
  });

  it('moves the row to the first slot after the current track', () => {
    rightClick(1);   // "After It" at position 4
    document.querySelector('#queueMenu .context-menu-item').click();

    // queue_position is 2, so the first reorderable slot is 3 — not 0, which
    // the server would reject as already played.
    expect(calls).toEqual([{ from_position: 4, to_position: 3 }]);
  });

  it('disables the entry for a row already at the top', () => {
    rightClick(0);   // "Next Up" is already at position 3
    const btn = document.querySelector('#queueMenu .context-menu-item');
    expect(btn.disabled).toBe(true);
  });

  it('hides the hover card so the two never overlap', () => {
    const card = document.getElementById('trackCard');
    card.getBoundingClientRect = () => ({ width: 200, height: 100 });
    document.querySelectorAll('#queueList li')[1]
      .dispatchEvent(new MouseEvent('mouseenter'));
    expect(card.hidden).toBe(false);

    rightClick(1);
    expect(card.hidden).toBe(true);
  });

  it('closes on Escape', () => {
    rightClick(1);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    expect(document.getElementById('queueMenu').hidden).toBe(true);
  });

  it('closes when the queue re-renders, since positions may have shifted', async () => {
    rightClick(1);
    expect(document.getElementById('queueMenu').hidden).toBe(false);

    await globalThis.renderQueue();   // e.g. the 10s status poll landing

    expect(document.getElementById('queueMenu').hidden).toBe(true);
  });
});

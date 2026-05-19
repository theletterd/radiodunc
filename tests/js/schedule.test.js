// Tests for the DJ schedule editor in app/ui/app.js.
//
// The schedule editor is the largest piece of UI logic we've shipped
// (grid rendering, drag-to-resize, drag-to-move, persona form, voice
// preview), and the area where UX bugs are most likely to land.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Test setup helpers ──────────────────────────────────────────────────────

const PERSONA_PALETTE = [
  '#e879a0', '#fb923c', '#60a5fa', '#a78bfa',
  '#34d399', '#fbbf24', '#f87171', '#22d3ee',
];

function configWithRoster(roster) {
  return {
    music_folder: '/test/music',
    station: { name: 'Test FM', dj_name: 'Test DJ', dj_roster: roster },
    alerts: {},
  };
}

function stubFetch(handlers) {
  // handlers: { 'GET /config': () => body, 'PUT /config': (body) => body, ... }
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

// Pre-render the grid by switching to scheduler mode, then waiting for the
// /config fetch + render to settle.
async function openScheduler() {
  globalThis._setSchedulerMode(true);
  await flush();
}

// Stub element layout so drag math (which uses getBoundingClientRect) has
// known dimensions. Real CSS isn't applied in happy-dom — cells return 0.
const ROW_PX = 20;
const COL_PX = 100;
function stubGridLayout() {
  const real = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function () {
    if (this.classList?.contains('grid-hour-label')) {
      return { width: 40, height: ROW_PX, top: 0, left: 0, right: 40, bottom: ROW_PX, x: 0, y: 0 };
    }
    if (this.classList?.contains('grid-day-header')) {
      return { width: COL_PX, height: 24, top: 0, left: 0, right: COL_PX, bottom: 24, x: 0, y: 0 };
    }
    return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0 };
  };
  return () => { Element.prototype.getBoundingClientRect = real; };
}

// ── Pure helpers ────────────────────────────────────────────────────────────

describe('pure helpers', () => {
  beforeEach(() => loadAppJs());

  it('_personaColor cycles through the 8-colour palette by index', () => {
    expect(typeof globalThis._personaColor).toBe('function');
    expect(globalThis._personaColor(0)).toBe(PERSONA_PALETTE[0]);
    expect(globalThis._personaColor(7)).toBe(PERSONA_PALETTE[7]);
    expect(globalThis._personaColor(8)).toBe(PERSONA_PALETTE[0]);  // wraps
    expect(globalThis._personaColor(17)).toBe(PERSONA_PALETTE[1]);
  });

  it('_jsDayToGridIndex maps Sunday-first JS to Monday-first grid', () => {
    expect(globalThis._jsDayToGridIndex(0)).toBe(6);  // Sun → col 6
    expect(globalThis._jsDayToGridIndex(1)).toBe(0);  // Mon → col 0
    expect(globalThis._jsDayToGridIndex(6)).toBe(5);  // Sat → col 5
  });
});

// ── renderSchedule ──────────────────────────────────────────────────────────

describe('renderSchedule', () => {
  beforeEach(() => loadAppJs());

  it('paints one block per simple shift in roster order', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
        { name: 'Lou', style: 'low', shifts: [
          { day: 'monday', start_hour: 22, end_hour: 23 },
          { day: 'tuesday', start_hour: 22, end_hour: 23 },
        ]},
      ]),
    });

    await openScheduler();
    const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');

    // 1 Morgan block + 2 Lou blocks. happy-dom keeps the hex value as-set
    // (it doesn't normalise to rgb() the way a browser would).
    expect(blocks.length).toBe(3);
    expect(blocks[0].style.backgroundColor).toBe(PERSONA_PALETTE[0]);  // Morgan (roster idx 0)
    expect(blocks[1].style.backgroundColor).toBe(PERSONA_PALETTE[1]);  // Lou (roster idx 1)
    expect(blocks[1].dataset.personaIdx).toBe('1');
    expect(blocks[1].dataset.shiftIdx).toBe('0');
  });

  it('places blocks on the right grid-row/column for the shift', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Solo', style: 'x', shifts: [{ day: 'wednesday', start_hour: 14, end_hour: 17 }] },
      ]),
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    // wednesday is the 3rd day (index 2), so column 2 + 2 = 4
    expect(block.style.gridColumn).toBe('4');
    // start_hour 14 → row 14+2 = 16; end_hour 17 (inclusive) → row 17+3 = 20
    expect(block.style.gridRow).toBe('16 / 20');
  });

  it('splits a wrap-around shift into two blocks across midnight', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Overnight', style: 'late', shifts: [{ day: 'friday', start_hour: 22, end_hour: 3 }] },
      ]),
    });

    await openScheduler();
    const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');

    expect(blocks.length).toBe(2);
    // today half: friday is DAYS_FULL[4], col = 4+2 = 6, rows 22+2 .. 26
    expect(blocks[0].style.gridColumn).toBe('6');
    expect(blocks[0].style.gridRow).toBe('24 / 26');
    expect(blocks[0].dataset.isWrap).toBe('1');
    expect(blocks[0].dataset.wrapHalf).toBe('today');
    // tomorrow half: saturday is DAYS_FULL[5], col = 5+2 = 7, rows 2 .. 3+3=6
    expect(blocks[1].style.gridColumn).toBe('7');
    expect(blocks[1].style.gridRow).toBe('2 / 6');
    expect(blocks[1].dataset.wrapHalf).toBe('tomorrow');
  });

  it('wrap-around blocks have no resize handles (form-only edit)', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Owl', style: 'mellow', shifts: [{ day: 'monday', start_hour: 22, end_hour: 2 }] },
      ]),
    });

    await openScheduler();
    const wrapBlocks = document.querySelectorAll('#scheduleGrid .grid-persona-block[data-is-wrap]');
    expect(wrapBlocks.length).toBe(2);
    wrapBlocks.forEach(b => {
      expect(b.querySelector('.resize-handle')).toBeNull();
    });
  });

  it('non-wrap blocks have top + bottom resize handles', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Day', style: 'bright', shifts: [{ day: 'monday', start_hour: 9, end_hour: 17 }] },
      ]),
    });

    await openScheduler();
    const handles = document.querySelectorAll('#scheduleGrid .resize-handle');
    expect(handles.length).toBe(2);
    expect(document.querySelector('#scheduleGrid .resize-handle.top')).toBeTruthy();
    expect(document.querySelector('#scheduleGrid .resize-handle.bottom')).toBeTruthy();
  });

  it('places the NOW indicator on the current day-column / hour-row', async () => {
    // Pin a known Date so the assertion is deterministic. Tuesday 14:30 UTC.
    const realNow = Date.now;
    const realProto = Date.prototype.getDay;
    const realHours = Date.prototype.getHours;
    Date.prototype.getDay = function () { return 2; };  // Tuesday
    Date.prototype.getHours = function () { return 14; };

    try {
      stubFetch({ 'GET /config': () => configWithRoster([]) });
      await openScheduler();
      const now = document.querySelector('#scheduleGrid .grid-now-indicator');
      expect(now).toBeTruthy();
      // _jsDayToGridIndex(2) = 1, col = 1+2 = 3; row = 14+2 = 16
      expect(now.style.gridColumn).toBe('3');
      expect(now.style.gridRow).toBe('16');
    } finally {
      Date.prototype.getDay = realProto;
      Date.prototype.getHours = realHours;
      Date.now = realNow;
    }
  });
});

// ── Drag-to-resize end-to-end ───────────────────────────────────────────────

describe('drag-to-resize', () => {
  let restoreLayout;

  beforeEach(() => {
    loadAppJs();
    restoreLayout = stubGridLayout();
  });

  afterEach(() => {
    restoreLayout?.();
  });

  function dispatchMouseEvent(target, type, clientY = 0, clientX = 0) {
    const ev = new MouseEvent(type, { bubbles: true, cancelable: true, clientY, clientX });
    target.dispatchEvent(ev);
  }

  it('drags the bottom handle down 2 rows → end_hour increases by 2 and PUT fires', async () => {
    const initial = configWithRoster([
      { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
    ]);
    let savedConfig = null;
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const bottomHandle = document.querySelector('#scheduleGrid .resize-handle.bottom');
    expect(bottomHandle).toBeTruthy();

    // Drag down 2 row-heights → +2 hours
    dispatchMouseEvent(bottomHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 2);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.dj_roster[0].shifts[0].end_hour).toBe(11);
    expect(savedConfig.station.dj_roster[0].shifts[0].start_hour).toBe(7);  // unchanged
  });

  it('drags the top handle up 3 rows → start_hour decreases by 3', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 9, end_hour: 12 }] },
      ]),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const topHandle = document.querySelector('#scheduleGrid .resize-handle.top');

    dispatchMouseEvent(topHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', -ROW_PX * 3);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig.station.dj_roster[0].shifts[0].start_hour).toBe(6);
    expect(savedConfig.station.dj_roster[0].shifts[0].end_hour).toBe(12);
  });

  it('clamps end_hour to 23 when dragged past the bottom of the grid', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Late', style: 'x', shifts: [{ day: 'monday', start_hour: 20, end_hour: 22 }] },
      ]),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const bottomHandle = document.querySelector('#scheduleGrid .resize-handle.bottom');

    dispatchMouseEvent(bottomHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 10);  // try to drag way past 23
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig.station.dj_roster[0].shifts[0].end_hour).toBe(23);
  });

  it('clamps start_hour to 0 when dragged past the top', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Early', style: 'x', shifts: [{ day: 'monday', start_hour: 3, end_hour: 6 }] },
      ]),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const topHandle = document.querySelector('#scheduleGrid .resize-handle.top');

    dispatchMouseEvent(topHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', -ROW_PX * 10);  // way past 0
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig.station.dj_roster[0].shifts[0].start_hour).toBe(0);
  });

  it('no PUT when drag ends with zero net movement', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Static', style: 'x', shifts: [{ day: 'monday', start_hour: 9, end_hour: 12 }] },
      ]),
      'PUT /config': (body) => { putCalls++; return body; },
    });

    await openScheduler();
    const handle = document.querySelector('#scheduleGrid .resize-handle.bottom');

    dispatchMouseEvent(handle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 2);  // sub-row pixel jitter
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(putCalls).toBe(0);
  });
});

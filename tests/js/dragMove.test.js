// Tests for the drag-to-move feature on Show blocks in app/ui/app.js.
//
// Each block has a mousedown → _startMoveDrag path and a click → _onBlockClick
// path. Small movements (< MOVE_THRESHOLD_PX = 4) are treated as clicks;
// larger movements suppress the click and PUT /config with the updated Show's
// shift data.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

function configFromShows(entries) {
  const djs = [];
  const shows = [];
  entries.forEach((e, i) => {
    const djId = `dj-${i}`;
    djs.push({
      id: djId,
      name: e.name,
      personality: e.personality || 'x',
      voice: e.voice ?? null,
      voice_instructions: e.voice_instructions ?? null,
      prompt_template: null,
    });
    shows.push({
      id: `show-${i}`,
      name: e.showName ?? null,
      dj_id: djId,
      shifts: e.shifts,
    });
  });
  return {
    music_folder: '/test/music',
    station: { name: 'Test FM', dj_name: 'Test DJ', djs, shows, dj_roster: [] },
    alerts: {},
  };
}

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

async function openScheduler() {
  globalThis._setSchedulerMode(true);
  await flush();
}

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

function dispatchMouseEvent(target, type, clientY = 0, clientX = 0) {
  const ev = new MouseEvent(type, { bubbles: true, cancelable: true, clientY, clientX });
  target.dispatchEvent(ev);
}

// ── drag-to-move ─────────────────────────────────────────────────────────────

describe('drag-to-move', () => {
  let restoreLayout;

  beforeEach(() => {
    loadAppJs();
    restoreLayout = stubGridLayout();
  });

  afterEach(() => {
    restoreLayout?.();
  });

  it('click without movement opens the show editor', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
      ]),
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block).toBeTruthy();

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mouseup', 0, 0);
    await flush();
    dispatchMouseEvent(block, 'click', 0, 0);
    await flush();

    // Editor should have opened: sub-view switches to 'edit' and the Show's
    // DJ picker is populated.
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
    const djSelect = document.getElementById('sf-dj');
    expect(djSelect).toBeTruthy();
    expect(djSelect.value).toBe('dj-0');  // Morgan
  });

  it('drag below 4px threshold is still treated as a click', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 2, 2);
    dispatchMouseEvent(document, 'mouseup', 2, 2);
    await flush();
    dispatchMouseEvent(block, 'click', 2, 2);
    await flush();

    expect(putCalled).toBe(false);
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
  });

  it('drag past threshold suppresses click and saves with updated hours', async () => {
    let savedConfig = null;
    const initial = configFromShows([
      { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 3, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 3, 0);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.shows[0].shifts[0];
    expect(shift.start_hour).toBe(10);  // 7 + 3
    expect(shift.end_hour).toBe(12);    // 9 + 3 (duration 2 preserved)

    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).not.toBe('edit');
  });

  it('horizontal drag changes the day', async () => {
    let savedConfig = null;
    const initial = configFromShows([
      { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 9, end_hour: 11 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 0, 150);
    dispatchMouseEvent(document, 'mouseup', 0, 150);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.shows[0].shifts[0];
    expect(shift.day).toBe('tuesday');
    expect(shift.start_hour).toBe(9);
    expect(shift.end_hour).toBe(11);
  });

  it('day is clamped to sunday when dragged past the last column', async () => {
    let savedConfig = null;
    const initial = configFromShows([
      { name: 'Sam', shifts: [{ day: 'saturday', start_hour: 10, end_hour: 12 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 0, COL_PX * 5);
    dispatchMouseEvent(document, 'mouseup', 0, COL_PX * 5);
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.shows[0].shifts[0].day).toBe('sunday');
  });

  it('start hour clamped so end stays ≤ 23', async () => {
    let savedConfig = null;
    const initial = configFromShows([
      { name: 'Late', shifts: [{ day: 'monday', start_hour: 20, end_hour: 22 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 10, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 10, 0);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.shows[0].shifts[0];
    expect(shift.start_hour).toBe(21);
    expect(shift.end_hour).toBe(23);
  });

  it('wrap-around blocks do not enter move mode; click opens the editor', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Owl', shifts: [{ day: 'monday', start_hour: 22, end_hour: 2 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    const wrapBlock = document.querySelector('#scheduleGrid .grid-persona-block[data-is-wrap]');
    expect(wrapBlock).toBeTruthy();

    dispatchMouseEvent(wrapBlock, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 5, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 5, 0);
    await flush();
    dispatchMouseEvent(wrapBlock, 'click', 0, 0);
    await flush();

    expect(putCalled).toBe(false);
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
  });

  it('mousedown on resize handle does not start a move drag', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Day', shifts: [{ day: 'monday', start_hour: 9, end_hour: 17 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    const bottomHandle = block.querySelector('.resize-handle.bottom');
    expect(bottomHandle).toBeTruthy();

    const originalGridRow = block.style.gridRow;

    dispatchMouseEvent(bottomHandle, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 0, COL_PX * 3);
    dispatchMouseEvent(document, 'mouseup', 0, COL_PX * 3);
    await flush();

    expect(block.style.gridColumn).toBe('2');
    expect(block.style.gridRow).toBe(originalGridRow);
  });
});

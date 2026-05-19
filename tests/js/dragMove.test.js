// Tests for the drag-to-move feature in app/ui/app.js.
//
// Each block has a mousedown → _startMoveDrag path and a click → _onBlockClick
// path.  Small movements (< MOVE_THRESHOLD_PX = 4) are treated as clicks;
// larger movements suppress the click and PUT /config with updated shift data.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers (same pattern as schedule.test.js) ───────────────────────────────

function configWithRoster(roster) {
  return {
    music_folder: '/test/music',
    station: { name: 'Test FM', dj_name: 'Test DJ', dj_roster: roster },
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

  it('click without movement opens the persona editor', async () => {
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
      ]),
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block).toBeTruthy();

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mouseup', 0, 0);
    await flush();
    // Fire the click that the browser would naturally produce after mouseup.
    dispatchMouseEvent(block, 'click', 0, 0);
    await flush();

    // Editor should have opened: sub-view switches to 'edit' and form is populated.
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
    const nameInput = document.getElementById('pf-name');
    expect(nameInput?.value).toBe('Morgan');
  });

  it('drag below 4px threshold is still treated as a click', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    // Move within the 4px threshold (diagonal 2,2 — below threshold on both axes)
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
    const initial = configWithRoster([
      { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    // Drag down 3 row-heights (3 hours)
    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 3, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 3, 0);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.dj_roster[0].shifts[0];
    expect(shift.start_hour).toBe(10);  // 7 + 3
    expect(shift.end_hour).toBe(12);    // 9 + 3 (duration 2 preserved)

    // Editor must NOT have opened
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).not.toBe('edit');
  });

  it('horizontal drag changes the day', async () => {
    let savedConfig = null;
    const initial = configWithRoster([
      { name: 'Morgan', style: 'cheerful', shifts: [{ day: 'monday', start_hour: 9, end_hour: 11 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    // Drag right 1.5 col widths → rounds to 1 → monday (idx 0) + 1 = tuesday (idx 1)
    // _gridColWidthPx returns COL_PX + 2 (gap) = 102; Math.round(150/102) = 1
    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 0, 150);
    dispatchMouseEvent(document, 'mouseup', 0, 150);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.dj_roster[0].shifts[0];
    expect(shift.day).toBe('tuesday');
    expect(shift.start_hour).toBe(9);   // hours unchanged
    expect(shift.end_hour).toBe(11);
  });

  it('day is clamped to sunday when dragged past the last column', async () => {
    let savedConfig = null;
    const initial = configWithRoster([
      { name: 'Sam', style: 'x', shifts: [{ day: 'saturday', start_hour: 10, end_hour: 12 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    // Saturday is index 5; drag right 5 col widths would be idx 10, clamped to 6 (sunday)
    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 0, COL_PX * 5);
    dispatchMouseEvent(document, 'mouseup', 0, COL_PX * 5);
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.dj_roster[0].shifts[0].day).toBe('sunday');
  });

  it('start hour clamped so end stays ≤ 23', async () => {
    let savedConfig = null;
    const initial = configWithRoster([
      { name: 'Late', style: 'x', shifts: [{ day: 'monday', start_hour: 20, end_hour: 22 }] },
    ]);
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');

    // Duration is 2; maxStart = 23 - 2 = 21. Drag down 10 rows tries start=30 → clamped to 21.
    dispatchMouseEvent(block, 'mousedown', 0, 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 10, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 10, 0);
    await flush();

    expect(savedConfig).not.toBeNull();
    const shift = savedConfig.station.dj_roster[0].shifts[0];
    expect(shift.start_hour).toBe(21);
    expect(shift.end_hour).toBe(23);
  });

  it('wrap-around blocks do not enter move mode; click opens the editor', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Owl', style: 'mellow', shifts: [{ day: 'monday', start_hour: 22, end_hour: 2 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    // The 'today' wrap half
    const wrapBlock = document.querySelector('#scheduleGrid .grid-persona-block[data-is-wrap]');
    expect(wrapBlock).toBeTruthy();

    // Mousedown on wrap block → guard returns early, no _moveState set up
    dispatchMouseEvent(wrapBlock, 'mousedown', 0, 0);
    await flush();
    // A big move that would normally trigger a drag
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 5, 0);
    dispatchMouseEvent(document, 'mouseup', ROW_PX * 5, 0);
    await flush();
    // Click should still fire (not suppressed)
    dispatchMouseEvent(wrapBlock, 'click', 0, 0);
    await flush();

    expect(putCalled).toBe(false);
    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
  });

  it('mousedown on resize handle does not start a move drag', async () => {
    let putCalled = false;
    stubFetch({
      'GET /config': () => configWithRoster([
        { name: 'Day', style: 'bright', shifts: [{ day: 'monday', start_hour: 9, end_hour: 17 }] },
      ]),
      'PUT /config': (body) => { putCalled = true; return body; },
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    const bottomHandle = block.querySelector('.resize-handle.bottom');
    expect(bottomHandle).toBeTruthy();

    // Store the block's grid-row before any interaction
    const originalGridRow = block.style.gridRow;

    // Mousedown on resize handle — _onBlockMouseDown guard skips _startMoveDrag
    dispatchMouseEvent(bottomHandle, 'mousedown', 0, 0);
    await flush();

    // A horizontal move that would change the day if move-drag were active
    dispatchMouseEvent(document, 'mousemove', 0, COL_PX * 3);
    dispatchMouseEvent(document, 'mouseup', 0, COL_PX * 3);
    await flush();

    // grid-column must NOT have been altered by a move-drag (resize only affects grid-row)
    // The column should still match the original monday column (= 2 for index 0)
    expect(block.style.gridColumn).toBe('2');
    // And the grid-row should still be the original (no move-drag running)
    expect(block.style.gridRow).toBe(originalGridRow);
  });
});

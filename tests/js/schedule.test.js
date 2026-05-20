// Tests for the DJ schedule grid in app/ui/app.js.
//
// The grid renders one block per shift on every Show, with the colour pinned
// to the DJ identity (so the same DJ hosting two different shows shows up in
// the same colour). Default-DJ slots (show.dj_id=null) get a distinct dashed
// treatment. Tests also cover the legend (one chip per used DJ + a Default
// chip + an "unscheduled" chip per Show that has no shifts) and the drag-
// to-resize flow.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Test setup helpers ──────────────────────────────────────────────────────

const PERSONA_PALETTE = [
  '#e879a0', '#fb923c', '#60a5fa', '#a78bfa',
  '#34d399', '#fbbf24', '#f87171', '#22d3ee',
];

// Build a config from a list of {name, personality?, shifts, showName?, dj_id?}
// entries. Each entry becomes one DJ + one Show wired together. dj_id is
// derived deterministically so tests can predict the colour assignment.
function configFromShows(entries) {
  const djs = [];
  const shows = [];
  entries.forEach((e, i) => {
    if (e.dj_id === null) {
      // Default-DJ slot: just a Show with dj_id=null, no DJ row needed.
      shows.push({
        id: `show-${i}`,
        name: e.showName ?? null,
        dj_id: null,
        shifts: e.shifts,
      });
    } else {
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
    }
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

// ── Pure helpers ────────────────────────────────────────────────────────────

describe('pure helpers', () => {
  beforeEach(() => loadAppJs());

  it('_personaColor cycles through the 8-colour palette by index', () => {
    expect(typeof globalThis._personaColor).toBe('function');
    expect(globalThis._personaColor(0)).toBe(PERSONA_PALETTE[0]);
    expect(globalThis._personaColor(7)).toBe(PERSONA_PALETTE[7]);
    expect(globalThis._personaColor(8)).toBe(PERSONA_PALETTE[0]);
    expect(globalThis._personaColor(17)).toBe(PERSONA_PALETTE[1]);
  });

  it('_jsDayToGridIndex maps Sunday-first JS to Monday-first grid', () => {
    expect(globalThis._jsDayToGridIndex(0)).toBe(6);
    expect(globalThis._jsDayToGridIndex(1)).toBe(0);
    expect(globalThis._jsDayToGridIndex(6)).toBe(5);
  });

  it('_fmtHourBoundary names the obvious landmarks', () => {
    expect(globalThis._fmtHourBoundary(0)).toBe('midnight');
    expect(globalThis._fmtHourBoundary(12)).toBe('noon');
    expect(globalThis._fmtHourBoundary(7)).toBe('7am');
    expect(globalThis._fmtHourBoundary(13)).toBe('1pm');
    expect(globalThis._fmtHourBoundary(23)).toBe('11pm');
    expect(globalThis._fmtHourBoundary(24)).toBe('midnight');
  });

  it('_fmtShiftRange treats end as inclusive — 23 ends at midnight', () => {
    expect(globalThis._fmtShiftRange(23, 23)).toBe('11pm → midnight');
    expect(globalThis._fmtShiftRange(22, 23)).toBe('10pm → midnight');
    expect(globalThis._fmtShiftRange(7, 9)).toBe('7am → 10am');
    expect(globalThis._fmtShiftRange(11, 11)).toBe('11am → noon');
    expect(globalThis._fmtShiftRange(22, 2)).toBe('10pm → 3am');
  });
});

// ── renderSchedule ──────────────────────────────────────────────────────────

describe('renderSchedule', () => {
  beforeEach(() => loadAppJs());

  it('paints one block per simple shift in shows order', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
        { name: 'Lou', shifts: [
          { day: 'monday', start_hour: 22, end_hour: 23 },
          { day: 'tuesday', start_hour: 22, end_hour: 23 },
        ]},
      ]),
    });

    await openScheduler();
    const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');

    expect(blocks.length).toBe(3);
    expect(blocks[0].style.backgroundColor).toBe(PERSONA_PALETTE[0]);
    expect(blocks[1].style.backgroundColor).toBe(PERSONA_PALETTE[1]);
    expect(blocks[1].dataset.showId).toBe('show-1');
    expect(blocks[1].dataset.shiftIdx).toBe('0');
  });

  it('places blocks on the right grid-row/column for the shift', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Solo', shifts: [{ day: 'wednesday', start_hour: 14, end_hour: 17 }] },
      ]),
    });

    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block.style.gridColumn).toBe('4');
    expect(block.style.gridRow).toBe('16 / 20');
  });

  it('splits a wrap-around shift into two blocks across midnight', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Overnight', shifts: [{ day: 'friday', start_hour: 22, end_hour: 3 }] },
      ]),
    });

    await openScheduler();
    const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');

    expect(blocks.length).toBe(2);
    expect(blocks[0].style.gridColumn).toBe('6');
    expect(blocks[0].style.gridRow).toBe('24 / 26');
    expect(blocks[0].dataset.isWrap).toBe('1');
    expect(blocks[0].dataset.wrapHalf).toBe('today');
    expect(blocks[1].style.gridColumn).toBe('7');
    expect(blocks[1].style.gridRow).toBe('2 / 6');
    expect(blocks[1].dataset.wrapHalf).toBe('tomorrow');
  });

  it('wrap-around blocks have no resize handles (form-only edit)', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Owl', shifts: [{ day: 'monday', start_hour: 22, end_hour: 2 }] },
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
      'GET /config': () => configFromShows([
        { name: 'Day', shifts: [{ day: 'monday', start_hour: 9, end_hour: 17 }] },
      ]),
    });

    await openScheduler();
    const handles = document.querySelectorAll('#scheduleGrid .resize-handle');
    expect(handles.length).toBe(2);
    expect(document.querySelector('#scheduleGrid .resize-handle.top')).toBeTruthy();
    expect(document.querySelector('#scheduleGrid .resize-handle.bottom')).toBeTruthy();
  });

  it('re-attaches block click handlers on every render (regression)', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
      ]),
    });

    await openScheduler();
    await window.renderSchedule();

    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block).toBeTruthy();

    block.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await flush();

    const panel = document.querySelector('.sidebar-scheduler');
    expect(panel?.dataset.subView).toBe('edit');
  });

  it('places the NOW indicator on the current day-column / hour-row', async () => {
    const realProto = Date.prototype.getDay;
    const realHours = Date.prototype.getHours;
    Date.prototype.getDay = function () { return 2; };
    Date.prototype.getHours = function () { return 14; };

    try {
      stubFetch({ 'GET /config': () => configFromShows([]) });
      await openScheduler();
      const now = document.querySelector('#scheduleGrid .grid-now-indicator');
      expect(now).toBeTruthy();
      expect(now.style.gridColumn).toBe('3');
      expect(now.style.gridRow).toBe('16');
    } finally {
      Date.prototype.getDay = realProto;
      Date.prototype.getHours = realHours;
    }
  });
});

// ── Legend ───────────────────────────────────────────────────────────────────

describe('legend', () => {
  beforeEach(() => loadAppJs());

  it('shows one chip per used DJ (deduped when one DJ hosts multiple shows)', async () => {
    stubFetch({
      'GET /config': () => {
        const cfg = configFromShows([
          { name: 'Sam', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
        ]);
        cfg.station.shows.push({
          id: 'show-extra',
          name: 'Late Night Sam',
          dj_id: 'dj-0',
          shifts: [{ day: 'tuesday', start_hour: 20, end_hour: 22 }],
        });
        return cfg;
      },
    });
    await openScheduler();

    const chips = document.querySelectorAll('#scheduleLegend .legend-item');
    // Default chip + Sam chip = 2 items total.
    expect(chips.length).toBe(2);
    expect(chips[1].textContent).toContain('Sam');
  });

  it('chip tooltip lists named shows the DJ hosts', async () => {
    stubFetch({
      'GET /config': () => {
        const cfg = configFromShows([
          { name: 'Jess', showName: 'Drivetime', shifts: [{ day: 'monday', start_hour: 16, end_hour: 19 }] },
        ]);
        cfg.station.shows.push({
          id: 'show-extra',
          name: 'Late Night',
          dj_id: 'dj-0',
          shifts: [{ day: 'tuesday', start_hour: 22, end_hour: 23 }],
        });
        return cfg;
      },
    });
    await openScheduler();

    const chips = document.querySelectorAll('#scheduleLegend .legend-item');
    const jessChip = Array.from(chips).find(c => c.textContent.includes('Jess'));
    expect(jessChip?.title).toContain('Drivetime');
    expect(jessChip?.title).toContain('Late Night');
  });

  it('Default chip is always present; default-slot Show carries default-dj class', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { dj_id: null, shifts: [{ day: 'monday', start_hour: 6, end_hour: 8 }] },
      ]),
    });
    await openScheduler();
    const chips = document.querySelectorAll('#scheduleLegend .legend-item');
    expect(chips.length).toBe(1);
    expect(chips[0].textContent).toContain('default');
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block?.classList.contains('default-dj')).toBe(true);
  });

  it('shows an "unscheduled" chip per Show with no shifts', async () => {
    stubFetch({
      'GET /config': () => {
        const cfg = configFromShows([
          { name: 'Has Shifts', shifts: [{ day: 'monday', start_hour: 9, end_hour: 11 }] },
        ]);
        cfg.station.djs.push({ id: 'dj-orphan', name: 'Drifter', personality: 'lost', voice: null, voice_instructions: null, prompt_template: null });
        cfg.station.shows.push({ id: 'show-orphan', name: null, dj_id: 'dj-orphan', shifts: [] });
        return cfg;
      },
    });
    await openScheduler();

    const unscheduled = document.querySelector('#scheduleLegend .legend-unscheduled');
    expect(unscheduled).toBeTruthy();
    expect(unscheduled.textContent).toContain('Drifter');
    expect(unscheduled.textContent).toContain('no shifts');
  });
});

// ── Show block label rendering ──────────────────────────────────────────────

describe('block label', () => {
  beforeEach(() => loadAppJs());

  it('renders just the DJ name when the show has no name', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Jess', shifts: [{ day: 'monday', start_hour: 6, end_hour: 10 }] },
      ]),
    });
    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    const label = block.querySelector('.block-label');
    expect(label?.textContent).toBe('Jess');
    // No lead/tail split when there's no show name to call out.
    expect(label.querySelector('.block-label-lead')).toBeNull();
    expect(label.querySelector('.block-label-tail')).toBeNull();
  });

  it('renders "<show> with <dj>" when the show has a name', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Jess', showName: 'Drivetime', shifts: [{ day: 'monday', start_hour: 6, end_hour: 10 }] },
      ]),
    });
    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    const label = block.querySelector('.block-label');
    expect(label?.textContent).toBe('Drivetime with Jess');
    // Lead carries the show name; tail carries the " with <DJ>" suffix
    // (styled lighter via CSS so the show name reads as the primary line).
    expect(label.querySelector('.block-label-lead').textContent).toBe('Drivetime');
    expect(label.querySelector('.block-label-tail').textContent).toBe(' with Jess');
  });

  it('block label wraps instead of truncating with an ellipsis', async () => {
    // happy-dom doesn't apply CSS layout, so we can't measure wrapped lines —
    // but we can assert the JS doesn't apply nowrap inline AND that the label
    // gets the .block-label class that the CSS targets for wrap behaviour.
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Saturday Night Sam', showName: 'Peak Hours',
          shifts: [{ day: 'saturday', start_hour: 18, end_hour: 23 }] },
      ]),
    });
    await openScheduler();
    const label = document.querySelector('#scheduleGrid .grid-persona-block .block-label');
    expect(label).toBeTruthy();
    // No inline white-space: nowrap (which the old block-primary/caption used).
    expect(label.style.whiteSpace).toBe('');
    // Text is the full combined string, not truncated with an ellipsis.
    expect(label.textContent).toContain('Peak Hours');
    expect(label.textContent).toContain('Saturday Night Sam');
  });

  it('1-hour blocks fall back to a single-letter monogram (no overflow)', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Jess', shifts: [{ day: 'monday', start_hour: 6, end_hour: 6 }] },
      ]),
    });
    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block.textContent.trim()).toBe('J');
    expect(block.querySelector('.block-label')).toBeNull();
  });

  it('1-hour monogram prefers the show name initial when set', async () => {
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Jess', showName: 'Drivetime',
          shifts: [{ day: 'monday', start_hour: 6, end_hour: 6 }] },
      ]),
    });
    await openScheduler();
    const block = document.querySelector('#scheduleGrid .grid-persona-block');
    expect(block.textContent.trim()).toBe('D');
  });
});

// ── DJ colour stability ─────────────────────────────────────────────────────

describe('DJ colour stability', () => {
  beforeEach(() => loadAppJs());

  it('blocks for the same DJ across multiple shows render in the same colour', async () => {
    stubFetch({
      'GET /config': () => {
        const cfg = configFromShows([
          { name: 'Jess', shifts: [{ day: 'monday', start_hour: 6, end_hour: 10 }] },
        ]);
        cfg.station.shows.push({
          id: 'show-extra',
          name: 'Late Night',
          dj_id: 'dj-0',
          shifts: [{ day: 'wednesday', start_hour: 22, end_hour: 23 }],
        });
        return cfg;
      },
    });
    await openScheduler();
    const blocks = document.querySelectorAll('#scheduleGrid .grid-persona-block');
    expect(blocks.length).toBe(2);
    expect(blocks[0].style.backgroundColor).toBe(PERSONA_PALETTE[0]);
    expect(blocks[1].style.backgroundColor).toBe(PERSONA_PALETTE[0]);
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
    const initial = configFromShows([
      { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 7, end_hour: 9 }] },
    ]);
    let savedConfig = null;
    stubFetch({
      'GET /config': () => initial,
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const bottomHandle = document.querySelector('#scheduleGrid .resize-handle.bottom');
    expect(bottomHandle).toBeTruthy();

    dispatchMouseEvent(bottomHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 2);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.shows[0].shifts[0].end_hour).toBe(11);
    expect(savedConfig.station.shows[0].shifts[0].start_hour).toBe(7);
  });

  it('drags the top handle up 3 rows → start_hour decreases by 3', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Morgan', shifts: [{ day: 'monday', start_hour: 9, end_hour: 12 }] },
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

    expect(savedConfig.station.shows[0].shifts[0].start_hour).toBe(6);
    expect(savedConfig.station.shows[0].shifts[0].end_hour).toBe(12);
  });

  it('clamps end_hour to 23 when dragged past the bottom of the grid', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Late', shifts: [{ day: 'monday', start_hour: 20, end_hour: 22 }] },
      ]),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const bottomHandle = document.querySelector('#scheduleGrid .resize-handle.bottom');

    dispatchMouseEvent(bottomHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', ROW_PX * 10);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig.station.shows[0].shifts[0].end_hour).toBe(23);
  });

  it('clamps start_hour to 0 when dragged past the top', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Early', shifts: [{ day: 'monday', start_hour: 3, end_hour: 6 }] },
      ]),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openScheduler();
    const topHandle = document.querySelector('#scheduleGrid .resize-handle.top');

    dispatchMouseEvent(topHandle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', -ROW_PX * 10);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(savedConfig.station.shows[0].shifts[0].start_hour).toBe(0);
  });

  it('no PUT when drag ends with zero net movement', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => configFromShows([
        { name: 'Static', shifts: [{ day: 'monday', start_hour: 9, end_hour: 12 }] },
      ]),
      'PUT /config': (body) => { putCalls++; return body; },
    });

    await openScheduler();
    const handle = document.querySelector('#scheduleGrid .resize-handle.bottom');

    dispatchMouseEvent(handle, 'mousedown', 0);
    await flush();
    dispatchMouseEvent(document, 'mousemove', 2);
    dispatchMouseEvent(document, 'mouseup');
    await flush();

    expect(putCalls).toBe(0);
  });
});

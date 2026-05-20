// Tests for the Show editor form in app/ui/app.js — the dialog that opens
// when you click a Show block on the schedule grid, or hit "+ New show".
//
// Covers _openShowEditor, _renderShowForm, the shifts list, save/delete, and
// the inline "+ Create new DJ…" modal flow that lets users add a DJ without
// leaving the Show editor.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

function makeConfig({ djs = [], shows = [] } = {}) {
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
    if (!handler) return globalThis.__fakeJsonResponse({}, 204);
    const body = opts.body ? JSON.parse(opts.body) : undefined;
    const responseBody = await handler(body);
    return globalThis.__fakeJsonResponse(responseBody);
  });
}

const SAM = {
  id: 'dj-sam',
  name: 'Sam',
  personality: 'warm and welcoming',
  voice: 'coral',
  voice_instructions: 'speak slowly',
  prompt_template: null,
};

function showWithSam(extra = {}) {
  return {
    id: 'show-1',
    name: 'Drivetime',
    dj_id: 'dj-sam',
    shifts: [{ day: 'tuesday', start_hour: 8, end_hour: 12 }],
    ...extra,
  };
}

async function openEditor(showId) {
  globalThis._setSchedulerMode(true);
  await flush();
  await globalThis._openShowEditor(showId);
  await flush();
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('show editor form', () => {
  beforeEach(() => loadAppJs());

  it('opening editor for an existing show pre-fills name and DJ picker', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }) });

    await openEditor('show-1');

    expect(document.getElementById('sf-name').value).toBe('Drivetime');
    expect(document.getElementById('sf-dj').value).toBe('dj-sam');
  });

  it('opening editor for a new show (__new__) gives a blank form with Default DJ selected', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });

    await openEditor('__new__');

    expect(document.getElementById('sf-name').value).toBe('');
    expect(document.getElementById('sf-dj').value).toBe('');  // Default DJ slot

    const submitBtn = document.querySelector('#showForm #pf-save');
    expect(submitBtn.textContent).toContain('Create show');
    expect(document.getElementById('pf-delete')).toBeNull();
  });

  it('shows the empty-shifts warning when the show has no shifts', async () => {
    stubFetch({
      'GET /config': () => makeConfig({
        djs: [SAM],
        shows: [{ id: 'show-empty', name: null, dj_id: 'dj-sam', shifts: [] }],
      }),
    });

    await openEditor('show-empty');

    const warning = document.querySelector('#showForm .empty-shifts-warning');
    expect(warning).toBeTruthy();
    expect(warning.textContent).toContain("won't air");
  });

  it('renders one .shift-row per shift', async () => {
    const show = showWithSam({
      shifts: [
        { day: 'monday', start_hour: 6, end_hour: 8 },
        { day: 'wednesday', start_hour: 12, end_hour: 14 },
        { day: 'friday', start_hour: 20, end_hour: 22 },
      ],
    });
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows: [show] }) });

    await openEditor('show-1');

    const rows = document.querySelectorAll('#pf-shifts .shift-row');
    expect(rows.length).toBe(3);
  });

  it('add-shift appends a default monday 9–17 shift', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }) });

    await openEditor('show-1');
    const before = document.querySelectorAll('#pf-shifts .shift-row').length;
    document.getElementById('pf-add-shift').click();
    await flush();

    const rows = document.querySelectorAll('#pf-shifts .shift-row');
    expect(rows.length).toBe(before + 1);
    const newRow = rows[rows.length - 1];
    expect(newRow.querySelector('select[data-field="day"]').value).toBe('monday');
    expect(newRow.querySelector('input[data-field="start_hour"]').value).toBe('9');
    expect(newRow.querySelector('input[data-field="end_hour"]').value).toBe('17');
  });

  it('remove-shift removes that row and (when all gone) shows the empty-shifts warning', async () => {
    const show = showWithSam();  // one shift
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows: [show] }) });

    await openEditor('show-1');
    expect(document.querySelector('.empty-shifts-warning')).toBeNull();

    document.querySelector('#pf-shifts .remove-shift').click();
    await flush();

    expect(document.querySelectorAll('#pf-shifts .shift-row').length).toBe(0);
    expect(document.querySelector('.empty-shifts-warning')).toBeTruthy();
  });

  it('changing a day select updates the working show state', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }) });

    await openEditor('show-1');
    const daySelect = document.querySelector('#pf-shifts select[data-field="day"]');
    daySelect.value = 'thursday';
    daySelect.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    const ws = globalThis.__getSchedulerWorkingShow();
    expect(ws.shifts[0].day).toBe('thursday');
  });

  it('save fires PUT /config with updated show name and DJ', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor('show-1');
    const nameEl = document.getElementById('sf-name');
    nameEl.value = 'New Show Name';
    nameEl.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('pf-save').click();
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.shows[0].name).toBe('New Show Name');
    expect(savedConfig.station.shows[0].dj_id).toBe('dj-sam');
    expect(savedConfig.station.shows.length).toBe(1);
  });

  it('save with a blank name normalises to null (rather than an empty string)', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor('show-1');
    const nameEl = document.getElementById('sf-name');
    nameEl.value = '   ';
    nameEl.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('pf-save').click();
    await flush();

    expect(savedConfig.station.shows[0].name).toBeNull();
  });

  it('save for a new show appends it to shows[]', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor('__new__');
    const nameEl = document.getElementById('sf-name');
    nameEl.value = 'Brand New';
    nameEl.dispatchEvent(new Event('input', { bubbles: true }));
    // Assign to Sam via the picker.
    const djEl = document.getElementById('sf-dj');
    djEl.value = 'dj-sam';
    djEl.dispatchEvent(new Event('change', { bubbles: true }));

    document.getElementById('pf-save').click();
    await flush();

    expect(savedConfig.station.shows.length).toBe(2);
    const created = savedConfig.station.shows[1];
    expect(created.name).toBe('Brand New');
    expect(created.dj_id).toBe('dj-sam');
  });

  it('save for a new show with Default DJ leaves dj_id=null', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor('__new__');
    document.getElementById('pf-save').click();
    await flush();

    expect(savedConfig.station.shows.length).toBe(1);
    expect(savedConfig.station.shows[0].dj_id).toBeNull();
  });

  it('delete with confirm=true PUTs config without that show; the DJ stays', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    globalThis.confirm = vi.fn(() => true);

    await openEditor('show-1');
    document.getElementById('pf-delete').click();
    await flush();

    expect(globalThis.confirm).toHaveBeenCalledOnce();
    expect(savedConfig.station.shows.length).toBe(0);
    // DJ identity is untouched.
    expect(savedConfig.station.djs.length).toBe(1);
    expect(savedConfig.station.djs[0].id).toBe('dj-sam');
  });

  it('delete with confirm=false does not PUT', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [showWithSam()] }),
      'PUT /config': (body) => { putCalls++; return body; },
    });
    globalThis.confirm = vi.fn(() => false);

    await openEditor('show-1');
    document.getElementById('pf-delete').click();
    await flush();

    expect(putCalls).toBe(0);
  });

  it('DJ picker lists existing DJs alphabetically plus Default and "+ Create new DJ…"', async () => {
    const ALEX = { id: 'dj-alex', name: 'Alex', personality: 'cool', voice: null, voice_instructions: null, prompt_template: null };
    const ZED  = { id: 'dj-zed',  name: 'Zed',  personality: 'punny', voice: null, voice_instructions: null, prompt_template: null };
    stubFetch({ 'GET /config': () => makeConfig({ djs: [ZED, ALEX, SAM] }) });  // out of order on purpose

    await openEditor('__new__');
    const opts = Array.from(document.getElementById('sf-dj').options).map(o => o.textContent);
    // (Default DJ), then DJs alphabetically, then "+ Create new DJ…"
    expect(opts[0]).toContain('Default');
    expect(opts[opts.length - 1]).toContain('Create new DJ');
    const djNames = opts.slice(1, -1);
    expect(djNames).toEqual(['Alex', 'Sam', 'Zed']);
  });
});

// ── Inline "+ Create new DJ" modal ──────────────────────────────────────────

describe('inline DJ create modal', () => {
  beforeEach(() => loadAppJs());

  it('selecting "+ Create new DJ…" opens the modal without changing the picker', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('__new__');

    // Snapshot the picker's current value (Default DJ for a fresh show).
    const djEl = document.getElementById('sf-dj');
    const before = djEl.value;

    djEl.value = '__new__';
    djEl.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    // Modal is open.
    const modal = document.getElementById('djCreateModal');
    expect(modal.classList.contains('open')).toBe(true);
    expect(modal.querySelector('#dj-modal-name')).toBeTruthy();
    // Picker snapped back so cancelling leaves the Show as it was.
    expect(djEl.value).toBe(before);
  });

  it('cancel closes the modal without modifying djs[]', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (b) => { putCalls++; return b; },
    });
    await openEditor('__new__');

    document.getElementById('sf-dj').value = '__new__';
    document.getElementById('sf-dj').dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    document.getElementById('dj-modal-cancel').click();
    await flush();

    const modal = document.getElementById('djCreateModal');
    expect(modal.classList.contains('open')).toBe(false);
    expect(putCalls).toBe(0);
  });

  it('saving the modal creates a DJ, closes the modal, and pre-selects it in the picker', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => savedConfig || makeConfig({ djs: [SAM] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    await openEditor('__new__');

    document.getElementById('sf-dj').value = '__new__';
    document.getElementById('sf-dj').dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    document.getElementById('dj-modal-name').value = 'New Jess';
    document.getElementById('dj-modal-personality').value = 'edgy and sharp';
    document.getElementById('dj-modal-save').click();
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.djs.length).toBe(2);
    const created = savedConfig.station.djs.find(d => d.name === 'New Jess');
    expect(created).toBeTruthy();
    expect(typeof created.id).toBe('string');
    expect(created.id.length).toBeGreaterThan(0);

    // Modal closed; picker now pre-selects the new DJ.
    const modal = document.getElementById('djCreateModal');
    expect(modal.classList.contains('open')).toBe(false);
    expect(document.getElementById('sf-dj').value).toBe(created.id);
  });

  it('saving with blank name/personality shows an error and does not PUT', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (b) => { putCalls++; return b; },
    });
    await openEditor('__new__');

    document.getElementById('sf-dj').value = '__new__';
    document.getElementById('sf-dj').dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    // Leave fields empty.
    document.getElementById('dj-modal-save').click();
    await flush();

    expect(putCalls).toBe(0);
    expect(document.getElementById('dj-modal-status').textContent).toContain('required');
    // Modal stays open so the user can fix the input.
    expect(document.getElementById('djCreateModal').classList.contains('open')).toBe(true);
  });
});

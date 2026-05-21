// Tests for the DJ Roster sidebar takeover in app/ui/app.js.
//
// The Roster view lists every DJ in station.djs[], showing a "used in N
// shows" counter and a soft warning when N=0. Clicking a row (or "+ New DJ")
// opens the DJ editor — the same fields the persona editor used to have,
// minus the shifts section (scheduling lives on the Show now). Delete
// reassigns hosted Shows to the Default DJ slot rather than dropping them.

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

const JESS = {
  id: 'dj-jess',
  name: 'Jess',
  personality: 'edgy and sharp',
  voice: null,
  voice_instructions: null,
  prompt_template: null,
};

const ORPHAN = {
  id: 'dj-orphan',
  name: 'Drifter',
  personality: 'lost in the static',
  voice: null,
  voice_instructions: null,
  prompt_template: null,
};

function samShow(extra = {}) {
  return {
    id: 'show-sam',
    name: 'Drivetime',
    dj_id: 'dj-sam',
    shifts: [{ day: 'tuesday', start_hour: 8, end_hour: 12 }],
    ...extra,
  };
}

async function openRoster() {
  globalThis._setRosterMode(true);
  await flush();
}

async function openEditor(djId) {
  await openRoster();
  await globalThis._openDJEditor(djId);
  await flush();
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('roster list', () => {
  beforeEach(() => loadAppJs());

  it('shows one row per DJ, alphabetically sorted', async () => {
    // Out of order on purpose: Jess before Sam, then alphabetical sort.
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM, JESS] }) });
    await openRoster();

    const rows = document.querySelectorAll('#rosterList .roster-row');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('Jess');
    expect(rows[1].textContent).toContain('Sam');
  });

  it('"used in N show(s)" counter reflects shows[]', async () => {
    stubFetch({
      'GET /config': () => makeConfig({
        djs: [SAM, JESS],
        shows: [samShow(), samShow({ id: 'show-sam-2', name: 'Late Night' })],
        // Jess hosts no shows.
      }),
    });
    await openRoster();

    const rows = document.querySelectorAll('#rosterList .roster-row');
    const samRow = Array.from(rows).find(r => r.textContent.includes('Sam'));
    const jessRow = Array.from(rows).find(r => r.textContent.includes('Jess'));
    expect(samRow.textContent).toContain('Used in 2 shows');
    expect(jessRow.textContent).toContain('Used in 0 shows');
  });

  it('orphan badge ("⚠ not in any show") only appears for DJs with no shows', async () => {
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM, ORPHAN], shows: [samShow()] }),
    });
    await openRoster();

    const rows = document.querySelectorAll('#rosterList .roster-row');
    const samRow = Array.from(rows).find(r => r.textContent.includes('Sam'));
    const orphanRow = Array.from(rows).find(r => r.textContent.includes('Drifter'));
    expect(samRow.querySelector('.orphan')).toBeNull();
    expect(orphanRow.querySelector('.orphan')).toBeTruthy();
    expect(orphanRow.querySelector('.orphan').textContent).toContain('not in any show');
  });

  it('voice line shows the voice name or "(default)" when unset', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM, JESS] }) });
    await openRoster();

    const rows = document.querySelectorAll('#rosterList .roster-row');
    const samRow = Array.from(rows).find(r => r.textContent.includes('Sam'));
    const jessRow = Array.from(rows).find(r => r.textContent.includes('Jess'));
    expect(samRow.textContent).toContain('Voice: coral');
    expect(jessRow.textContent).toContain('Voice: (default)');
  });

  it('empty roster shows a friendly nudge rather than an empty list', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [] }) });
    await openRoster();
    expect(document.querySelectorAll('#rosterList .roster-row').length).toBe(0);
    expect(document.querySelector('#rosterList p')?.textContent).toContain('No DJs yet');
  });

  it('clicking a row opens the editor for that DJ', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openRoster();

    document.querySelector('#rosterList .roster-row').click();
    await flush();

    const panel = document.querySelector('.sidebar-roster');
    expect(panel?.dataset.subView).toBe('edit');
    expect(document.getElementById('de-name').value).toBe('Sam');
  });
});

// ── DJ editor form ──────────────────────────────────────────────────────────

describe('DJ editor', () => {
  beforeEach(() => loadAppJs());

  it('opening editor for an existing DJ pre-fills every field', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('dj-sam');

    expect(document.getElementById('de-name').value).toBe('Sam');
    expect(document.getElementById('de-personality').value).toBe('warm and welcoming');
    const voiceSelect = document.getElementById('de-voice');
    expect(voiceSelect.querySelector('option[selected]')?.value).toBe('coral');
    expect(document.getElementById('de-vi').value).toBe('speak slowly');
  });

  it('opening editor for a new DJ (__new__) gives a blank form with a "Create DJ" button', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('__new__');

    expect(document.getElementById('de-name').value).toBe('');
    expect(document.getElementById('de-personality').value).toBe('');
    expect(document.getElementById('de-voice').value).toBe('');
    expect(document.getElementById('de-vi').value).toBe('');
    expect(document.getElementById('de-save').textContent).toContain('Create DJ');
    // No delete button on a new DJ; no "used in" footer either.
    expect(document.getElementById('de-delete')).toBeNull();
    expect(document.querySelector('.dj-editor-uses')).toBeNull();
  });

  it('saving an existing DJ PUTs config with the updated fields', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    await openEditor('dj-sam');

    document.getElementById('de-name').value = 'Sam Updated';
    document.getElementById('de-personality').value = 'breezier now';
    document.getElementById('de-save').click();
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.djs[0].name).toBe('Sam Updated');
    expect(savedConfig.station.djs[0].personality).toBe('breezier now');
    expect(savedConfig.station.djs.length).toBe(1);
    // Switches back to the list sub-view.
    expect(document.querySelector('.sidebar-roster').dataset.subView).toBe('list');
  });

  it('saving a new DJ appends to djs[] with a freshly-generated id', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    await openEditor('__new__');

    document.getElementById('de-name').value = 'New One';
    document.getElementById('de-personality').value = 'fresh take';
    document.getElementById('de-save').click();
    await flush();

    expect(savedConfig.station.djs.length).toBe(2);
    const created = savedConfig.station.djs[1];
    expect(created.name).toBe('New One');
    expect(typeof created.id).toBe('string');
    expect(created.id.length).toBeGreaterThan(0);
    expect(created.id).not.toBe(SAM.id);
  });

  it('save with blank required fields shows an error and does not PUT', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'PUT /config': (b) => { putCalls++; return b; },
    });
    await openEditor('__new__');

    // Leave name + personality empty.
    document.getElementById('de-save').click();
    await flush();

    expect(putCalls).toBe(0);
    expect(document.getElementById('de-preview-status').textContent).toContain('required');
  });

  it('"used in N shows" footer lists shows with their shift ranges and show names', async () => {
    const shows = [
      samShow({ id: 'show-1', name: 'Drivetime', shifts: [{ day: 'tuesday', start_hour: 8, end_hour: 12 }] }),
      samShow({ id: 'show-2', name: null, shifts: [
        { day: 'friday', start_hour: 22, end_hour: 23 },
        { day: 'saturday', start_hour: 22, end_hour: 23 },
      ]}),
    ];
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM], shows }) });
    await openEditor('dj-sam');

    const uses = document.querySelector('.dj-editor-uses');
    expect(uses).toBeTruthy();
    expect(uses.textContent).toContain('Used in 2 shows');
    // Named show shows up with its name.
    expect(uses.textContent).toContain('Drivetime');
    // Shift ranges rendered.
    expect(uses.textContent).toContain('Tuesday');
    expect(uses.textContent).toContain('Friday');
  });

  it('footer shows a soft warning when the DJ has no shows', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [ORPHAN] }) });
    await openEditor('dj-orphan');

    const noUses = document.querySelector('.dj-editor-uses .no-uses');
    expect(noUses).toBeTruthy();
    expect(noUses.textContent).toContain('Not currently in any show');
  });
});

// ── Delete DJ + show reassignment ──────────────────────────────────────────

describe('delete DJ', () => {
  beforeEach(() => loadAppJs());

  it('delete with confirm=true reassigns affected shows to Default DJ', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({
        djs: [SAM, JESS],
        shows: [samShow(), { id: 'show-sam-2', name: 'Late Night', dj_id: 'dj-sam', shifts: [{ day: 'friday', start_hour: 22, end_hour: 23 }] }],
      }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    globalThis.confirm = vi.fn(() => true);
    await openEditor('dj-sam');

    document.getElementById('de-delete').click();
    await flush();

    expect(globalThis.confirm).toHaveBeenCalledOnce();
    expect(savedConfig).not.toBeNull();
    // DJ is gone.
    expect(savedConfig.station.djs.length).toBe(1);
    expect(savedConfig.station.djs[0].id).toBe('dj-jess');
    // Both shows survive; their dj_id is now null.
    expect(savedConfig.station.shows.length).toBe(2);
    expect(savedConfig.station.shows.every(s => s.dj_id === null)).toBe(true);
  });

  it('delete confirm prompt lists the affected shows', async () => {
    stubFetch({
      'GET /config': () => makeConfig({
        djs: [SAM],
        shows: [samShow({ name: 'Drivetime', shifts: [{ day: 'tuesday', start_hour: 8, end_hour: 12 }] })],
      }),
      'PUT /config': (body) => body,
    });
    let confirmPrompt = '';
    globalThis.confirm = vi.fn((msg) => { confirmPrompt = msg; return false; });

    await openEditor('dj-sam');
    document.getElementById('de-delete').click();
    await flush();

    expect(confirmPrompt).toContain('Drivetime');
    expect(confirmPrompt).toContain('Tuesday');
    expect(confirmPrompt).toContain('Default DJ');
  });

  it('delete with confirm=false does not PUT', async () => {
    let putCalls = 0;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM], shows: [samShow()] }),
      'PUT /config': (b) => { putCalls++; return b; },
    });
    globalThis.confirm = vi.fn(() => false);

    await openEditor('dj-sam');
    document.getElementById('de-delete').click();
    await flush();

    expect(putCalls).toBe(0);
  });

  it('delete works even when the DJ has zero shows (no reassignment list in prompt)', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [ORPHAN] }),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    globalThis.confirm = vi.fn(() => true);

    await openEditor('dj-orphan');
    document.getElementById('de-delete').click();
    await flush();

    expect(savedConfig.station.djs.length).toBe(0);
    expect(savedConfig.station.shows.length).toBe(0);
  });
});

// ── Voice preview ───────────────────────────────────────────────────────────

describe('voice preview', () => {
  beforeEach(() => loadAppJs());

  it('preview POSTs to /tts/preview with the current form voice + instructions', async () => {
    let previewBody = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'POST /tts/preview': (body) => { previewBody = body; return { clip_url: '/tmp/x.mp3' }; },
    });
    await openEditor('dj-sam');

    document.getElementById('de-voice').value = 'nova';
    document.getElementById('de-vi').value = 'fast paced';
    document.getElementById('de-preview-btn').click();
    await flush();

    expect(previewBody).not.toBeNull();
    expect(previewBody.voice).toBe('nova');
    expect(previewBody.voice_instructions).toBe('fast paced');
    expect(typeof previewBody.text).toBe('string');
    expect(previewBody.text.length).toBeGreaterThan(0);
  });
});

// ── Roster mode wiring ─────────────────────────────────────────────────────

describe('roster mode', () => {
  beforeEach(() => loadAppJs());

  it('_setRosterMode(true) sets wrap data-mode=roster and renders the list', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openRoster();
    expect(document.getElementById('wrap').dataset.mode).toBe('roster');
    expect(document.querySelectorAll('#rosterList .roster-row').length).toBe(1);
  });

  it('_setRosterMode(false) returns to default mode', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openRoster();
    globalThis._setRosterMode(false);
    expect(document.getElementById('wrap').dataset.mode).toBe('default');
  });

  it('cancel from the editor returns to the list sub-view', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('dj-sam');
    expect(document.querySelector('.sidebar-roster').dataset.subView).toBe('edit');

    document.getElementById('de-cancel').click();
    expect(document.querySelector('.sidebar-roster').dataset.subView).toBe('list');
  });
});

// ── DJ avatars ──────────────────────────────────────────────────────────────

describe('DJ avatars', () => {
  beforeEach(() => loadAppJs());

  it('roster row renders a 60 px avatar circle with an <img> overlay', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openRoster();

    const avatar = document.querySelector('#rosterList .dj-avatar');
    expect(avatar).toBeTruthy();
    // -md = 60 px, sized up from the old 28 px swatch because the avatar is
    // now doing actual portrait work, not just colour-coding.
    expect(avatar.classList.contains('dj-avatar-md')).toBe(true);
    // Background colour is the DJ's palette colour (placeholder if the
    // <img> fails to load).
    expect(avatar.style.background).toBeTruthy();
    const img = avatar.querySelector('img');
    expect(img).toBeTruthy();
    expect(img.getAttribute('src')).toContain('/media/dj-icon/dj-sam');
    // Cache-bust query string so we don't serve stale browser-cached images.
    expect(img.getAttribute('src')).toMatch(/\?v=\d+/);
    // onerror self-removes the <img> so the coloured circle stays as the
    // placeholder when no avatar has been generated yet.
    expect(img.getAttribute('onerror')).toContain('this.remove()');
  });

  it('roster row uses a horizontal layout with avatar + content stack', async () => {
    // Layout regression guard. The 60 px avatar looks unbalanced when the
    // row is a vertical stack (avatar towers above three small text lines);
    // the row needs to be flex-row with the text content stacked beside the
    // avatar so the eye reads the two columns evenly. Asserting the DOM
    // structure is the cheapest way to lock that in without trying to
    // measure pixels in happy-dom.
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openRoster();

    const row = document.querySelector('#rosterList .roster-row');
    expect(row).toBeTruthy();
    // Two direct children: avatar + content. The text fields (name,
    // personality, meta) live inside .roster-row-content, not as direct
    // children of .roster-row.
    const directChildren = Array.from(row.children);
    expect(directChildren.length).toBe(2);
    expect(directChildren[0].classList.contains('dj-avatar')).toBe(true);
    expect(directChildren[1].classList.contains('roster-row-content')).toBe(true);
    // The text fields all live inside the content stack.
    expect(directChildren[1].querySelector('.roster-row-name')).toBeTruthy();
    expect(directChildren[1].querySelector('.roster-row-meta')).toBeTruthy();
  });

  it('editor avatar shows the image + a Regenerate avatar button for existing DJs', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('dj-sam');

    const avatar = document.querySelector('#djEditorForm .dj-avatar');
    expect(avatar).toBeTruthy();
    expect(avatar.classList.contains('dj-avatar-lg')).toBe(true);
    expect(avatar.querySelector('img')).toBeTruthy();

    const btn = document.getElementById('de-regen-avatar');
    expect(btn).toBeTruthy();
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain('Regenerate avatar');
  });

  it('editor avatar is just the coloured circle (no <img>) for new DJs, button disabled', async () => {
    stubFetch({ 'GET /config': () => makeConfig({ djs: [SAM] }) });
    await openEditor('__new__');

    const avatar = document.querySelector('#djEditorForm .dj-avatar');
    expect(avatar).toBeTruthy();
    // No <img> on new DJs — they don't have a server-side id yet.
    expect(avatar.querySelector('img')).toBeNull();

    const btn = document.getElementById('de-regen-avatar');
    expect(btn.disabled).toBe(true);
    // Hint copy tells the user what's blocking them.
    const hint = document.querySelector('.dj-editor-avatar-hint');
    expect(hint.textContent).toContain('Save first');
  });

  it('clicking Regenerate POSTs to /djs/{id}/avatar and bumps the cache-bust', async () => {
    let postPath = null;
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'POST /djs/dj-sam/avatar': () => {
        postPath = '/djs/dj-sam/avatar';
        return { url: '/media/dj-icon/dj-sam', generated_at: 99999 };
      },
    });
    await openEditor('dj-sam');

    const imgBefore = document.querySelector('#djEditorForm .dj-avatar img');
    const srcBefore = imgBefore.getAttribute('src');

    document.getElementById('de-regen-avatar').click();
    await flush();

    expect(postPath).toBe('/djs/dj-sam/avatar');
    // The form re-rendered with the new cache-bust timestamp.
    const imgAfter = document.querySelector('#djEditorForm .dj-avatar img');
    expect(imgAfter.getAttribute('src')).toContain('?v=99999');
    expect(imgAfter.getAttribute('src')).not.toBe(srcBefore);
    // Status surfaces success to the user.
    expect(document.getElementById('de-preview-status').textContent).toContain('Avatar updated');
  });

  it('regenerate failure surfaces the error inline and re-enables the button', async () => {
    stubFetch({
      'GET /config': () => makeConfig({ djs: [SAM] }),
      'POST /djs/dj-sam/avatar': () => {
        throw new Error('502 server failed');
      },
    });
    await openEditor('dj-sam');

    document.getElementById('de-regen-avatar').click();
    await flush();

    expect(document.getElementById('de-preview-status').textContent).toContain('Avatar generation failed');
    // Button is re-enabled so the user can retry.
    expect(document.getElementById('de-regen-avatar').disabled).toBe(false);
  });

  it('_djAvatarUrl includes a cache-bust query parameter on every call', () => {
    const url = globalThis._djAvatarUrl('dj-anything');
    expect(url).toMatch(/^\/media\/dj-icon\/dj-anything\?v=\d+$/);
  });
});

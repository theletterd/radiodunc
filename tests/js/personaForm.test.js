// Tests for the persona editor form in app/ui/app.js.
//
// Covers _openPersonaEditor, _renderPersonaForm, _renderShifts, _savePersona,
// _deletePersona, _previewVoice, and _readFormIntoWorkingPersona.

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

// ── Helpers ─────────────────────────────────────────────────────────────────

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

function morganRoster() {
  return [
    {
      name: 'Morgan',
      personality: 'warm and welcoming',
      voice: 'coral',
      voice_instructions: 'speak slowly',
      shifts: [{ day: 'tuesday', start_hour: 8, end_hour: 12 }],
    },
  ];
}

// Open the persona editor by switching to scheduler mode then calling _openPersonaEditor.
async function openEditor(personaIdx) {
  globalThis._setSchedulerMode(true);
  await flush();
  await globalThis._openPersonaEditor(personaIdx);
  await flush();
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('persona editor form', () => {
  beforeEach(() => {
    loadAppJs();
    // Reset the cached app source between tests is not needed — loadAppJs
    // re-evals and re-seeds globalThis each time.
  });

  // 1. Opening editor for existing persona populates fields
  it('opening editor for existing persona populates fields', async () => {
    stubFetch({ 'GET /config': () => configWithRoster(morganRoster()) });

    await openEditor(0);

    expect(document.getElementById('pf-name').value).toBe('Morgan');
    expect(document.getElementById('pf-personality').value).toBe('warm and welcoming');
    // happy-dom doesn't update select.value from innerHTML `selected` attributes,
    // so we check the selected option's value attribute directly.
    const voiceSelect = document.getElementById('pf-voice');
    const selectedVoiceOption = voiceSelect.querySelector('option[selected]');
    expect(selectedVoiceOption?.value).toBe('coral');
    expect(document.getElementById('pf-voice-instructions').value).toBe('speak slowly');
  });

  // 2. Opening editor for new (-1) gives blank form
  it('opening editor for new persona (-1) gives blank form', async () => {
    stubFetch({ 'GET /config': () => configWithRoster(morganRoster()) });

    await openEditor(-1);

    expect(document.getElementById('pf-name').value).toBe('');
    expect(document.getElementById('pf-personality').value).toBe('');
    expect(document.getElementById('pf-voice').value).toBe('');
    expect(document.getElementById('pf-voice-instructions').value).toBe('');

    const submitBtn = document.querySelector('#personaForm button[type="submit"]');
    expect(submitBtn.textContent).toContain('Create persona');

    expect(document.getElementById('pf-delete')).toBeNull();
  });

  // 3. Shifts list renders one row per shift
  it('renders one .shift-row per shift', async () => {
    const roster = [{
      name: 'Triple', personality: 'x', voice: null, voice_instructions: null,
      shifts: [
        { day: 'monday', start_hour: 6, end_hour: 8 },
        { day: 'wednesday', start_hour: 12, end_hour: 14 },
        { day: 'friday', start_hour: 20, end_hour: 22 },
      ],
    }];
    stubFetch({ 'GET /config': () => configWithRoster(roster) });

    await openEditor(0);

    const rows = document.querySelectorAll('#pf-shifts .shift-row');
    expect(rows.length).toBe(3);

    rows.forEach(row => {
      expect(row.querySelector('select[data-field="day"]')).toBeTruthy();
      expect(row.querySelectorAll('input[type="number"]').length).toBe(2);
      expect(row.querySelector('.remove-shift')).toBeTruthy();
    });
  });

  // 4. Add-shift button appends a default 9-17 monday shift
  it('add-shift button appends a default monday 9–17 shift', async () => {
    stubFetch({ 'GET /config': () => configWithRoster(morganRoster()) });

    await openEditor(0);

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

  // 5. Remove-shift button removes that row
  it('remove-shift removes that row and preserves the remaining shift', async () => {
    const roster = [{
      name: 'Two', personality: 'x', voice: null, voice_instructions: null,
      shifts: [
        { day: 'monday', start_hour: 6, end_hour: 8 },
        { day: 'friday', start_hour: 20, end_hour: 22 },
      ],
    }];
    stubFetch({ 'GET /config': () => configWithRoster(roster) });

    await openEditor(0);

    // Remove the first shift
    document.querySelector('#pf-shifts .remove-shift').click();
    await flush();

    const rows = document.querySelectorAll('#pf-shifts .shift-row');
    expect(rows.length).toBe(1);
    // The surviving row should now be the originally-second shift (friday 20-22).
    // happy-dom doesn't update select.value from innerHTML `selected` attributes,
    // so check the selected option's value attribute directly.
    const daySelect = rows[0].querySelector('select[data-field="day"]');
    const selectedOption = daySelect.querySelector('option[selected]');
    expect(selectedOption?.value).toBe('friday');
    expect(rows[0].querySelector('input[data-field="start_hour"]').value).toBe('20');
  });

  // 6. Editing a shift field updates the working persona state
  it('changing a day select updates the schedulerWorkingPersona state', async () => {
    stubFetch({ 'GET /config': () => configWithRoster(morganRoster()) });

    await openEditor(0);

    const daySelect = document.querySelector('#pf-shifts select[data-field="day"]');
    daySelect.value = 'thursday';
    daySelect.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();

    const wp = globalThis.__getSchedulerWorkingPersona();
    expect(wp.shifts[0].day).toBe('thursday');
  });

  // 7. Save flow: existing persona PUT fires with updated values
  it('submit fires PUT /config with the updated persona', async () => {
    const roster = morganRoster();
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster(roster),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor(0);

    document.getElementById('pf-name').value = 'Morgan Updated';
    document.getElementById('pf-personality').value = 'energetic';

    const form = document.getElementById('personaForm');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.dj_roster[0].name).toBe('Morgan Updated');
    expect(savedConfig.station.dj_roster[0].personality).toBe('energetic');
    expect(savedConfig.station.dj_roster.length).toBe(1);
  });

  // 8. Save flow: new persona is appended to the roster
  it('submit for new persona (-1) appends to the roster', async () => {
    const roster = morganRoster();
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster(roster),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });

    await openEditor(-1);

    document.getElementById('pf-name').value = 'Alex';
    document.getElementById('pf-personality').value = 'cool and calm';

    const form = document.getElementById('personaForm');
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flush();

    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.dj_roster.length).toBe(2);
    const newPersona = savedConfig.station.dj_roster[1];
    expect(newPersona.name).toBe('Alex');
    expect(newPersona.personality).toBe('cool and calm');
  });

  // 9. Delete: confirms then PUTs config without that persona
  it('delete with confirm=true fires PUT and removes the persona', async () => {
    const roster = [
      ...morganRoster(),
      { name: 'Lou', personality: 'chill', voice: null, voice_instructions: null, shifts: [] },
    ];
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster(roster),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    globalThis.confirm = vi.fn(() => true);

    await openEditor(0);

    document.getElementById('pf-delete').click();
    await flush();

    expect(globalThis.confirm).toHaveBeenCalledOnce();
    expect(savedConfig).not.toBeNull();
    expect(savedConfig.station.dj_roster.length).toBe(1);
    expect(savedConfig.station.dj_roster.find(p => p.name === 'Morgan')).toBeUndefined();
  });

  // 10. Delete: cancelled when confirm returns false
  it('delete with confirm=false does not fire PUT', async () => {
    const roster = morganRoster();
    let putCalls = 0;
    stubFetch({
      'GET /config': () => configWithRoster(roster),
      'PUT /config': (body) => { putCalls++; return body; },
    });
    globalThis.confirm = vi.fn(() => false);

    await openEditor(0);

    document.getElementById('pf-delete').click();
    await flush();

    expect(globalThis.confirm).toHaveBeenCalledOnce();
    expect(putCalls).toBe(0);
  });

  // 11. Voice preview: POST /tts/preview with current form values
  it('preview button POSTs to /tts/preview with form voice fields', async () => {
    let previewBody = null;
    stubFetch({
      'GET /config': () => configWithRoster(morganRoster()),
      'POST /tts/preview': (body) => { previewBody = body; return { clip_url: '/tmp/clip.mp3' }; },
    });

    await openEditor(0);

    // Update voice fields in the form before clicking preview
    document.getElementById('pf-voice').value = 'nova';
    document.getElementById('pf-voice-instructions').value = 'fast paced';

    document.getElementById('pf-preview-btn').click();
    await flush();

    expect(previewBody).not.toBeNull();
    expect(previewBody.voice).toBe('nova');
    expect(previewBody.voice_instructions).toBe('fast paced');
    expect(typeof previewBody.text).toBe('string');
    expect(previewBody.text.length).toBeGreaterThan(0);
  });

  // Volume-trim slider plumbing
  it('the volume slider populates from voice_gain_offset_db on open', async () => {
    const roster = [{
      name: 'Loud', personality: 'shouts',
      voice: 'sage', voice_instructions: null, voice_gain_offset_db: -6, shifts: [],
    }];
    stubFetch({ 'GET /config': () => configWithRoster(roster) });
    await openEditor(0);

    // -6 dB on the ±18 dB range → slider value 33 (50 + -6 * 50/18 = 33.33 → rounded)
    expect(document.getElementById('pf-volume-trim').value).toBe('33');
    expect(document.getElementById('pf-volume-readout').textContent).toBe('Quieter');
  });

  it('submit persists the slider position back as voice_gain_offset_db', async () => {
    let savedConfig = null;
    stubFetch({
      'GET /config': () => configWithRoster(morganRoster()),
      'PUT /config': (body) => { savedConfig = body; return body; },
    });
    await openEditor(0);

    // Drag the slider to "Much louder" → value 90, +14.4 dB (90-50)*18/50
    const slider = document.getElementById('pf-volume-trim');
    slider.value = '90';
    slider.dispatchEvent(new Event('input', { bubbles: true }));

    document.getElementById('personaForm').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    await flush();

    expect(savedConfig).not.toBeNull();
    const persona = savedConfig.station.dj_roster[0];
    expect(persona.voice_gain_offset_db).toBeCloseTo(14.4, 3);
  });
});

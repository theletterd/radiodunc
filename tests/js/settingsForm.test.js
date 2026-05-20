// Tests for the Station Settings sidebar panel.
//
// The settings panel is a takeover view that mirrors the scheduler: open
// from a sidebar button, render a grouped form pre-filled from /config,
// save back via PUT /config. These tests cover:
//   - form rendering with current config values
//   - reading form values back onto a config (the round-trip)
//   - the save flow (PUT fires, status updates, in-memory config refresh)

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { loadAppJs, flush } from './_loadApp.js';

function makeConfig(overrides = {}) {
  // A reasonably complete starting config — every field the form touches
  // is set so we can detect any that gets dropped on round-trip.
  return {
    music_folder: '/test/music',
    openai_text_temperature: 1.2,
    station: {
      name: 'Test FM',
      spoken_name: 'test eff em',
      tagline: 'tagline here',
      format: 'Eclectic',
      description: 'a description',
      era: '90s',
      genre_focus: ['indie rock', 'post-punk'],
      dj_name: 'Test DJ',
      dj_roster: [
        // The settings panel must preserve dj_roster untouched — it's edited
        // through the scheduler, not here.
        { name: 'Untouched', personality: 'x', shifts: [] },
      ],
    },
    alerts: {
      weather_location: 'Portland, OR',
      weather_latitude: 45.5,
      weather_longitude: -122.6,
      weather: { enabled: true, every_n_breaks: 4 },
      news: {
        enabled: true,
        rss_url: 'https://example.test/rss',
        headline_count: 3,
        every_n_breaks: 5,
        // Voice pools are edited via JSON for now; the panel must round-trip them.
        voices: [{ voice: 'onyx', name: 'Alex', voice_instructions: 'calm' }],
      },
      ads: {
        enabled: false,
        every_n_breaks: 6,
        pool_size: 100,
        risque_chance: 0.1,
        voices: [{ voice: 'echo', voice_instructions: 'classic' }],
      },
      station_id: { enabled: true, phrase_count: 40 },
    },
    ...overrides,
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
    return globalThis.__fakeJsonResponse(await handler(body));
  });
}

async function openSettings(config) {
  stubFetch({
    'GET /config': () => config,
    // Default echo-back save handler; tests can override.
    'PUT /config': (body) => body,
  });
  await globalThis._openSettings();
  await flush();
}

describe('settings form rendering', () => {
  beforeEach(() => loadAppJs());

  it('pre-fills every field from the current config', async () => {
    await openSettings(makeConfig());

    expect(document.querySelector('#s-name').value).toBe('Test FM');
    expect(document.querySelector('#s-spoken-name').value).toBe('test eff em');
    expect(document.querySelector('#s-tagline').value).toBe('tagline here');
    expect(document.querySelector('#s-format').value).toBe('Eclectic');
    expect(document.querySelector('#s-description').value).toBe('a description');
    expect(document.querySelector('#s-era').value).toBe('90s');
    // Genre focus is rendered as a CSV string.
    expect(document.querySelector('#s-genre-focus').value).toBe('indie rock, post-punk');

    expect(document.querySelector('#s-weather-enabled').checked).toBe(true);
    expect(document.querySelector('#s-weather-location').value).toBe('Portland, OR');
    expect(document.querySelector('#s-weather-lat').value).toBe('45.5');
    expect(document.querySelector('#s-weather-lon').value).toBe('-122.6');
    expect(document.querySelector('#s-weather-cadence').value).toBe('4');

    expect(document.querySelector('#s-news-enabled').checked).toBe(true);
    expect(document.querySelector('#s-news-rss').value).toBe('https://example.test/rss');
    expect(document.querySelector('#s-news-count').value).toBe('3');
    expect(document.querySelector('#s-news-cadence').value).toBe('5');

    expect(document.querySelector('#s-ads-enabled').checked).toBe(false);
    expect(document.querySelector('#s-ads-cadence').value).toBe('6');
    expect(document.querySelector('#s-ads-pool').value).toBe('100');
    expect(document.querySelector('#s-ads-risque').value).toBe('0.1');

    expect(document.querySelector('#s-sid-enabled').checked).toBe(true);
    expect(document.querySelector('#s-sid-count').value).toBe('40');

    expect(document.querySelector('#s-temperature').value).toBe('1.2');
  });

  it('flips wrap.dataset.mode to "settings" so the sidebar takes over', async () => {
    await openSettings(makeConfig());
    expect(document.getElementById('wrap').dataset.mode).toBe('settings');
  });

  it('handles a sparse config without crashing (missing nested objects)', async () => {
    // Some early/partial configs may not have alerts.* fully populated.
    await openSettings({
      music_folder: '/m',
      station: { name: 'Sparse', dj_name: 'x' },
      alerts: {},
    });
    // Form should render with sensible defaults rather than blowing up.
    expect(document.querySelector('#s-name').value).toBe('Sparse');
    // Default cadence values from the form helper.
    expect(document.querySelector('#s-weather-cadence').value).toBe('4');
    expect(document.querySelector('#s-news-cadence').value).toBe('5');
  });
});

describe('_applySettingsForm round-trip', () => {
  beforeEach(() => loadAppJs());

  it('writes edited values back onto the config object', async () => {
    await openSettings(makeConfig());

    // User edits some fields.
    document.querySelector('#s-name').value = 'Renamed FM';
    document.querySelector('#s-tagline').value = 'New tagline';
    document.querySelector('#s-genre-focus').value = 'jazz, soul, ambient';
    document.querySelector('#s-weather-enabled').checked = false;
    document.querySelector('#s-news-cadence').value = '10';
    document.querySelector('#s-ads-risque').value = '0.25';
    document.querySelector('#s-temperature').value = '0.9';

    const next = JSON.parse(JSON.stringify({ station: {}, alerts: {} }));
    globalThis._applySettingsForm(next, document.getElementById('settingsForm'));

    expect(next.station.name).toBe('Renamed FM');
    expect(next.station.tagline).toBe('New tagline');
    expect(next.station.genre_focus).toEqual(['jazz', 'soul', 'ambient']);
    expect(next.alerts.weather.enabled).toBe(false);
    expect(next.alerts.news.every_n_breaks).toBe(10);
    expect(next.alerts.ads.risque_chance).toBe(0.25);
    expect(next.openai_text_temperature).toBe(0.9);
  });

  it('converts empty optional text fields to null (not empty string)', async () => {
    // The pydantic schema accepts null for description/era/spoken_name but
    // rejects empty strings on some — clearing a field should send null.
    const cfg = makeConfig();
    cfg.station.description = 'will be cleared';
    await openSettings(cfg);

    document.querySelector('#s-description').value = '';
    document.querySelector('#s-era').value = '';
    document.querySelector('#s-spoken-name').value = '';

    const next = { station: {}, alerts: {} };
    globalThis._applySettingsForm(next, document.getElementById('settingsForm'));
    expect(next.station.description).toBeNull();
    expect(next.station.era).toBeNull();
    expect(next.station.spoken_name).toBeNull();
  });

  it('clears latitude/longitude to null when the input is emptied', async () => {
    // Number inputs read 0 from .value when blank, which would falsely
    // anchor the weather location at 0,0. Explicit empty-check guards this.
    await openSettings(makeConfig());
    document.querySelector('#s-weather-lat').value = '';
    document.querySelector('#s-weather-lon').value = '';

    const next = { station: {}, alerts: {} };
    globalThis._applySettingsForm(next, document.getElementById('settingsForm'));
    expect(next.alerts.weather_latitude).toBeNull();
    expect(next.alerts.weather_longitude).toBeNull();
  });
});

describe('save flow', () => {
  beforeEach(() => loadAppJs());

  it('PUTs the merged config, preserving dj_roster and voice pools', async () => {
    const cfg = makeConfig();
    let savedBody = null;
    stubFetch({
      'GET /config': () => cfg,
      'PUT /config': (body) => { savedBody = body; return body; },
    });
    await globalThis._openSettings();
    await flush();

    // Edit something + click save.
    document.querySelector('#s-name').value = 'Saved FM';
    await globalThis._saveSettings();
    await flush();

    expect(savedBody).not.toBeNull();
    // Edited field made it.
    expect(savedBody.station.name).toBe('Saved FM');
    // Fields the panel doesn't expose are preserved verbatim from the
    // fetched config (this is the main thing keeping the panel safe to
    // ship before we have a voices-pool editor).
    expect(savedBody.station.dj_roster).toEqual(cfg.station.dj_roster);
    expect(savedBody.alerts.news.voices).toEqual(cfg.alerts.news.voices);
    expect(savedBody.alerts.ads.voices).toEqual(cfg.alerts.ads.voices);
    // Status surfaces success to the user.
    expect(document.querySelector('#s-status').textContent).toBe('Saved.');
  });

  it('shows the error message inline when save fails', async () => {
    stubFetch({
      'GET /config': () => makeConfig(),
      'PUT /config': () => { throw new Error('server exploded'); },
    });
    await globalThis._openSettings();
    await flush();

    await globalThis._saveSettings();
    await flush();

    expect(document.querySelector('#s-status').textContent).toMatch(/Save failed/);
  });

  it('refreshes the on-page station header after a successful save', async () => {
    stubFetch({
      'GET /config': () => makeConfig(),
      'PUT /config': (body) => body,
    });
    await globalThis._openSettings();
    await flush();

    document.querySelector('#s-name').value = 'Header Refresh FM';
    document.querySelector('#s-tagline').value = 'New tagline';
    await globalThis._saveSettings();
    await flush();

    expect(document.getElementById('stationName').textContent).toBe('Header Refresh FM');
    expect(document.getElementById('stationMeta').textContent).toBe('New tagline');
  });
});

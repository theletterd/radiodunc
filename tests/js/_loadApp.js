// Helper for loading the production app/ui/app.js into the test's global
// scope. Sets up the actual DOM from index.html first so init() finds every
// element it expects.
//
// We can't rely on indirect eval making function declarations members of
// globalThis (Node's module-context eval doesn't behave like script eval),
// so we append a small re-export block to the source that copies every
// function we want to test onto globalThis via the lexical-scope binding
// it has at that point.

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const APP_JS_PATH    = resolve(__dirname, '../../app/ui/app.js');
const INDEX_HTML_PATH = resolve(__dirname, '../../app/ui/index.html');

let _cachedAppSource = null;
let _cachedBodyHtml = null;

// Names of functions inside app.js that tests need to reach. These get
// copied onto globalThis via the appended re-export block. If you add a
// new top-level function declaration and want to test it, list it here.
const EXPORTED_NAMES = [
  // pure helpers
  '_personaColor', '_jsDayToGridIndex',
  '_gridRowHeightPx', '_gridColWidthPx',
  '_fmtHourBoundary', '_fmtShiftRange',
  '_dbToGainMultiplier',
  '_volumeSliderToDb', '_dbToVolumeSlider', '_volumeSliderLabel',
  // schedule rendering / mode
  'renderSchedule', '_attachBlockClickHandlers',
  '_setSchedulerMode', '_setSchedulerSubView',
  // persona form
  '_openPersonaEditor', '_renderPersonaForm', '_renderShifts',
  '_savePersona', '_deletePersona', '_previewVoice',
  '_readFormIntoWorkingPersona',
  // drag-to-resize
  '_startResizeDrag', '_onDragMove', '_onDragEnd',
  // drag-to-move
  '_startMoveDrag', '_onMoveDragMove', '_onMoveDragEnd',
  '_onBlockMouseDown', '_onBlockClick',
  // player rendering
  'renderPlayer', 'renderQueue', 'renderAll',
  'refreshServerState', 'refreshLibraryStatus',
  'setOnAirMode', 'animateLabel',
  // playback timing
  'pausePlayback', 'resumePlayback', 'stopPlayback',
  'clearAutoTrigger', 'scheduleAutoTrigger',
  'clearModeTimers', 'scheduleMode',
  'clearStingerTimer', 'clearPrefetchTimer', 'schedulePrefetch',
];

function _appSource() {
  if (_cachedAppSource === null) {
    const raw = readFileSync(APP_JS_PATH, 'utf-8');
    const exports = EXPORTED_NAMES.map(n => `globalThis.${n} = ${n};`).join('\n');
    // Expose the `schedulerWorkingPersona` let-binding via an accessor because
    // let/const bindings can't be assigned to globalThis directly from outside
    // the module scope. Tests call globalThis.__getSchedulerWorkingPersona()
    // to read the current mutable form state without going through the DOM.
    const accessor = `globalThis.__getSchedulerWorkingPersona = () => schedulerWorkingPersona;`;
    // Accessors for private let-bindings needed by player render tests.
    const playerAccessors = [
      `globalThis.__setServerState = (s) => { serverState = s; };`,
      `globalThis.__setCtx = (c) => { ctx = c; };`,
      `globalThis.__getCtx = () => ctx;`,
      `globalThis.__setPaused = (p) => { paused = p; };`,
      `globalThis.__getPaused = () => paused;`,
      `globalThis.__setOnAirModeVar = (m) => { onAirMode = m; };`,
      `globalThis.__getOnAirMode = () => onAirMode;`,
      `globalThis.__getAutoTriggerTimer = () => autoTriggerTimer;`,
      `globalThis.__getAutoTriggerRemaining = () => _autoTriggerRemaining;`,
      `globalThis.__getStingerTimer = () => _stingerTimer;`,
      `globalThis.__getPrefetchTimer = () => _prefetchTimer;`,
      `globalThis.__getModeTimers = () => _modeTimers;`,
      `globalThis.__setActiveSlot = (s) => { activeSlot = s; };`,
      `globalThis.__setSlots = (newSlots) => { for (const k in newSlots) slots[k] = newSlots[k]; };`,
    ].join('\n');
    _cachedAppSource = raw + '\n;\n' + exports + '\n' + accessor + '\n' + playerAccessors + '\n';
  }
  return _cachedAppSource;
}

function _bodyHtml() {
  if (_cachedBodyHtml === null) {
    const html = readFileSync(INDEX_HTML_PATH, 'utf-8');
    const m = html.match(/<body[^>]*>([\s\S]*?)<\/body>/i);
    // Strip the <script src="..."> reference — we load app.js ourselves via eval.
    _cachedBodyHtml = (m ? m[1] : '').replace(/<script[\s\S]*?<\/script>/gi, '');
  }
  return _cachedBodyHtml;
}

/**
 * Render the index.html body and evaluate app.js into the test's global scope.
 * After this returns, app.js's function declarations are on globalThis and
 * init() has been kicked off (it's async; await microtasks before asserting
 * on init-driven state).
 */
export function loadAppJs() {
  document.body.innerHTML = _bodyHtml();
  (0, eval)(_appSource());
}

/**
 * Drain microtasks so any in-flight init()/fetch chains settle before assertions.
 * Use after loadAppJs() when a test depends on rendering having completed.
 */
export async function flush() {
  // A few macrotask + microtask cycles cover the typical fetch().then() chains.
  for (let i = 0; i < 5; i++) {
    await new Promise(r => setTimeout(r, 0));
  }
}

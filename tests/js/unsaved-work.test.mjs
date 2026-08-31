// Unit tests for hasUnsavedWork/makeBeforeUnloadHandler/
// installUnsavedWorkWarning (tier-1 round 2, finding 6; follow-up
// investigation after a maintainer report of "couldn't trigger the
// browser-level warning" -- see unsaved-work.js's own doc comment for the
// live-verified root-cause writeup). Plain `node --test`, zero dependencies
// -- see key-containment.test.mjs's own header comment for why this repo
// tests panel JS this way.
//
// What these tests CAN prove: the handler is registered at the right time
// (a fake `window.addEventListener` call happens), reads the live/current
// handle rather than a stale snapshot, and correctly calls
// preventDefault()/sets returnValue when there is unsaved work. What they
// CANNOT prove, and don't try to: whether a real browser actually shows
// the native "leave site?" dialog for a `defaultPrevented` beforeunload --
// that is Chrome's own rendering, entirely outside this module's code, and
// untestable without a real, unattended browser (automated browser
// harnesses -- this repo's own included -- auto-dismiss the dialog so
// tests don't hang).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  hasUnsavedWork,
  makeBeforeUnloadHandler,
  installUnsavedWorkWarning,
} from '../../custom_components/opendisplay/designer/frontend/panel/unsaved-work.js';

function fakeHandle(lastEditAt) {
  return { getStatus: () => ({ lastEditAt }) };
}

function fakeEvent() {
  return {
    defaultPrevented: false,
    returnValue: undefined,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

// A minimal window-shaped fake -- just enough of the EventTarget contract
// installUnsavedWorkWarning actually uses, plus a way for the test to
// dispatch a synthetic 'beforeunload' the same way key-containment's own
// fake nodes do.
function fakeWindow() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    _listenerCount(type) {
      return listeners.get(type)?.size ?? 0;
    },
    _fire(type, event) {
      for (const fn of listeners.get(type) ?? []) fn(event);
    },
  };
}

test('hasUnsavedWork is false before any edit (lastEditAt null)', () => {
  assert.equal(hasUnsavedWork(fakeHandle(null)), false);
});

test('hasUnsavedWork is true after an edit (lastEditAt is a timestamp)', () => {
  assert.equal(hasUnsavedWork(fakeHandle(Date.now())), true);
});

test('hasUnsavedWork is false when there is no handle at all (not yet mounted)', () => {
  assert.equal(hasUnsavedWork(null), false);
  assert.equal(hasUnsavedWork(undefined), false);
});

test('the beforeunload handler does nothing when there is no unsaved work', () => {
  const handler = makeBeforeUnloadHandler(() => fakeHandle(null));
  const event = fakeEvent();

  handler(event);

  assert.equal(event.defaultPrevented, false, 'must not warn when nothing was edited');
});

test('the beforeunload handler warns when there is unsaved work', () => {
  const handler = makeBeforeUnloadHandler(() => fakeHandle(Date.now()));
  const event = fakeEvent();

  handler(event);

  assert.equal(event.defaultPrevented, true);
  assert.equal(event.returnValue, '', 'returnValue must be set for the cross-browser beforeunload contract');
});

test('the handler reads the handle fresh on every call, not a stale snapshot', () => {
  // A remount can reassign the handle after the listener is registered --
  // the factory takes a thunk specifically so it never closes over a stale
  // handle from before a remount.
  let current = fakeHandle(null);
  const handler = makeBeforeUnloadHandler(() => current);

  const before = fakeEvent();
  handler(before);
  assert.equal(before.defaultPrevented, false);

  current = fakeHandle(Date.now());
  const after = fakeEvent();
  handler(after);
  assert.equal(after.defaultPrevented, true);
});

// --- installUnsavedWorkWarning: "is the handler actually registered" -----
// (coordinator candidate 1: is the beforeunload listener registered at the
// right time). This can't reach into the real panel's connectedCallback
// without a real DOM (no jsdom in this repo), but it proves the function
// that call site invokes -- one line each way -- actually wires up and
// tears down a real 'beforeunload' listener correctly.

test('installUnsavedWorkWarning registers a beforeunload listener on the given window', () => {
  const win = fakeWindow();
  assert.equal(win._listenerCount('beforeunload'), 0);

  installUnsavedWorkWarning(win, () => fakeHandle(null));

  assert.equal(win._listenerCount('beforeunload'), 1, 'connectedCallback\'s call must actually register a listener');
});

test('installUnsavedWorkWarning\'s handler reads the LIVE handle at fire time, not a snapshot from install time', () => {
  // Mirrors the real call site exactly: () => this._handle, not
  // this._handle captured once -- a remount can reassign the handle after
  // installUnsavedWorkWarning ran.
  let liveHandle = fakeHandle(null);
  const win = fakeWindow();
  installUnsavedWorkWarning(win, () => liveHandle);

  const beforeEdit = fakeEvent();
  win._fire('beforeunload', beforeEdit);
  assert.equal(beforeEdit.defaultPrevented, false, 'no edit yet -- must not warn');

  liveHandle = fakeHandle(Date.now()); // an edit happens after install, before unload
  const afterEdit = fakeEvent();
  win._fire('beforeunload', afterEdit);
  assert.equal(afterEdit.defaultPrevented, true, 'live handle now has unsaved work -- must warn');
  assert.equal(afterEdit.returnValue, '');
});

test('installUnsavedWorkWarning is a no-op (never warns) if the handle is destroyed/gone at fire time', () => {
  // Coordinator candidate 2's "handle destroyed before beforeunload"
  // ordering concern -- if getHandle() ever returns null/undefined at
  // fire time (e.g. disconnectedCallback's destroy() ran first, for
  // whichever navigation path can reach that ordering), this must not
  // throw and must not warn, matching hasUnsavedWork's own null handling.
  const win = fakeWindow();
  installUnsavedWorkWarning(win, () => null);

  const event = fakeEvent();
  assert.doesNotThrow(() => win._fire('beforeunload', event));
  assert.equal(event.defaultPrevented, false);
});

test('installUnsavedWorkWarning\'s disposer actually removes the listener', () => {
  const win = fakeWindow();
  const uninstall = installUnsavedWorkWarning(win, () => fakeHandle(Date.now()));
  assert.equal(win._listenerCount('beforeunload'), 1);

  uninstall();

  assert.equal(win._listenerCount('beforeunload'), 0, "disconnectedCallback's call must actually remove it");
  // And firing after disposal must not warn either -- confirms the
  // specific handler instance was removed, not a no-op disposer.
  const event = fakeEvent();
  win._fire('beforeunload', event);
  assert.equal(event.defaultPrevented, false);
});

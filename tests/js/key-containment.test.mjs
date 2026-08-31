// Unit tests for containKeyEvents (tier-1 round 2, CRITICAL finding 1).
//
// Plain `node --test`, zero dependencies (no jsdom, no npm) -- this repo has
// no JS package manager at all, and pulling one in just for this would be
// disproportionate to one small, pure function. A plain EventTarget doesn't
// bubble through a "parent" the way real DOM nodes do, so this hand-rolls a
// minimal three-node bubble chain (inner -> host -> window) and a minimal
// Event stand-in with stopPropagation()/preventDefault() tracking -- just
// enough to exercise containKeyEvents' actual contract (which only calls
// addEventListener/removeEventListener/event.stopPropagation on whatever
// `host` it's given), not to re-verify that real browsers bubble events
// (they do; that's not this file's job).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { containKeyEvents } from '../../custom_components/opendisplay/designer/frontend/panel/key-containment.js';

function makeNode(name) {
  const listeners = new Map();
  return {
    name,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    _fire(type, event) {
      for (const fn of listeners.get(type) ?? []) fn(event);
    },
  };
}

function makeEvent(type) {
  return {
    type,
    stopped: false,
    defaultPrevented: false,
    stopPropagation() {
      this.stopped = true;
    },
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

// Dispatches `type` starting at path[0], bubbling through path[1], path[2],
// ... in order -- exactly DOM bubble order (innermost first) -- and stops
// walking the path (not just marking the event) once something calls
// stopPropagation, matching how a real browser actually stops delivering an
// event to further listeners rather than merely flagging it.
function dispatchBubbling(path, type) {
  const event = makeEvent(type);
  for (const node of path) {
    node._fire(type, event);
    if (event.stopped) break;
  }
  return event;
}

test('containKeyEvents stops a keydown from reaching a listener outside the mount', () => {
  const inner = makeNode('inner'); // e.g. CodeMirror's contenteditable div
  const host = makeNode('host'); // the panel custom element (shadow host)
  const win = makeNode('window'); // stands in for HA's tinykeys(window, ...)

  let windowSawKeydown = false;
  win.addEventListener('keydown', () => {
    windowSawKeydown = true;
  });

  containKeyEvents(host);

  const event = dispatchBubbling([inner, host, win], 'keydown');

  assert.equal(windowSawKeydown, false, 'window-level listener must not see a keydown that originated inside the mount');
  assert.equal(event.stopped, true, 'propagation must actually be stopped');
  assert.equal(event.defaultPrevented, false, 'must NEVER preventDefault -- that would break native typing');
});

test('containKeyEvents covers keyup and keypress too, not just keydown', () => {
  const inner = makeNode('inner');
  const host = makeNode('host');
  const win = makeNode('window');

  const seen = { keyup: false, keypress: false };
  win.addEventListener('keyup', () => {
    seen.keyup = true;
  });
  win.addEventListener('keypress', () => {
    seen.keypress = true;
  });

  containKeyEvents(host);

  dispatchBubbling([inner, host, win], 'keyup');
  dispatchBubbling([inner, host, win], 'keypress');

  assert.equal(seen.keyup, false, 'keyup must be contained too');
  assert.equal(seen.keypress, false, 'keypress must be contained too');
});

test('a keydown that never passes through the mount is untouched', () => {
  // A keystroke somewhere else on the HA page entirely -- e.g. the sidebar
  // search, or another panel -- must still reach window normally. This is
  // the "quick-bar still opens when focus is outside the designer" half of
  // the acceptance criteria: containment must be scoped to the mount, not a
  // page-wide keyboard blackhole.
  const elsewhere = makeNode('elsewhere');
  const win = makeNode('window');
  const host = makeNode('host'); // present in the DOM, but not in this path

  let windowSawKeydown = false;
  win.addEventListener('keydown', () => {
    windowSawKeydown = true;
  });

  containKeyEvents(host);

  const event = dispatchBubbling([elsewhere, win], 'keydown');

  assert.equal(windowSawKeydown, true, 'a keydown outside the mount must still reach window-level listeners (e.g. HA quick-bar shortcuts)');
  assert.equal(event.stopped, false);
});

test('the returned disposer actually removes the listeners', () => {
  const inner = makeNode('inner');
  const host = makeNode('host');
  const win = makeNode('window');

  let windowSawKeydown = false;
  win.addEventListener('keydown', () => {
    windowSawKeydown = true;
  });

  const dispose = containKeyEvents(host);
  dispose();

  dispatchBubbling([inner, host, win], 'keydown');

  assert.equal(windowSawKeydown, true, 'after disposal, events must bubble through normally again (e.g. on unmount)');
});

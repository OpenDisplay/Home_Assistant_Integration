// Unit tests for containKeyEvents (tier-1 round 2, CRITICAL finding 1;
// narrowed from a blanket version in a follow-up fix round -- see
// key-containment.js's own "Residual tradeoff" doc comment).
//
// Plain `node --test`, zero dependencies (no jsdom, no npm) -- this repo has
// no JS package manager at all, and pulling one in just for this would be
// disproportionate to one small, pure function. A plain EventTarget doesn't
// bubble through a "parent" the way real DOM nodes do, so this hand-rolls a
// minimal bubble chain and a minimal Event/Element stand-in -- just enough
// to exercise containKeyEvents' actual contract, not to re-verify that real
// browsers bubble events (they do; that's not this file's job).
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

// A minimal stand-in for an Element -- just enough of the shape
// isEditableTarget actually reads (tagName/closest/isContentEditable).
function makeElement({ tagName = 'DIV', matchesCmEditor = false, isContentEditable = false } = {}) {
  return {
    tagName,
    isContentEditable,
    closest(selector) {
      return selector === '.cm-editor' && matchesCmEditor ? this : null;
    },
  };
}

const editorTarget = () => makeElement({ tagName: 'DIV', matchesCmEditor: true }); // CodeMirror's own contenteditable div
const canvasTarget = () => makeElement({ tagName: 'CANVAS' }); // the designer's drawing surface -- not editable

function makeKeyEvent(type, { key, ctrlKey = false, metaKey = false, altKey = false, shiftKey = false, path }) {
  return {
    type,
    key,
    ctrlKey,
    metaKey,
    altKey,
    shiftKey,
    stopped: false,
    defaultPrevented: false,
    stopPropagation() {
      this.stopped = true;
    },
    preventDefault() {
      this.defaultPrevented = true;
    },
    composedPath() {
      return path;
    },
  };
}

// Dispatches starting at path[0], bubbling through path[1], path[2], ... in
// order -- DOM bubble order (innermost first) -- stopping once something
// calls stopPropagation, matching how a real browser stops delivering an
// event to further listeners rather than merely flagging it.
function dispatchBubbling(nodePath, event) {
  for (const node of nodePath) {
    node._fire(event.type, event);
    if (event.stopped) break;
  }
  return event;
}

function threeNodeMount() {
  const inner = makeNode('inner');
  const host = makeNode('host'); // the panel custom element (shadow host)
  const win = makeNode('window'); // stands in for HA's tinykeys(window, ...)
  return { inner, host, win };
}

// --- Quick-bar suppression (bare letter, HA's own quick-bar shape) --------

test('a bare letter from the YAML editor is contained (editable-target rule)', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'e', path: [editorTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, false, 'a bare "e" typed in the editor must never reach window (would open HA quick-bar)');
  assert.equal(event.stopped, true);
  assert.equal(event.defaultPrevented, false, 'must NEVER preventDefault');
});

test('a bare letter from the CANVAS (not the editor) is ALSO contained', () => {
  // This is the case a target-only guard would miss: the canvas is not an
  // editable target, but the designer still has no bare-letter shortcuts of
  // its own to lose, and HA's quick-bar must not pop up over the canvas
  // either (key-containment.js's own "Residual tradeoff" doc comment).
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'e', path: [canvasTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, false, 'a bare "e" from canvas focus must still be contained');
  assert.equal(event.stopped, true);
});

test('a bare letter that never passes through the mount still reaches window', () => {
  // The "quick-bar still opens when focus is outside the designer" half of
  // the acceptance criteria -- containment must be scoped to the mount.
  const elsewhere = makeElement();
  const win = makeNode('window');
  const host = makeNode('host'); // present in the DOM, but not in this path

  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'e', path: [elsewhere, win] });
  dispatchBubbling([win], event); // host never in the bubble path at all

  assert.equal(windowSaw, true, 'a keydown outside the mount must still reach window-level listeners');
  assert.equal(event.stopped, false);
});

test('covers keyup and keypress too, not just keydown', () => {
  const { inner, host, win } = threeNodeMount();
  const seen = { keyup: false, keypress: false };
  win.addEventListener('keyup', () => {
    seen.keyup = true;
  });
  win.addEventListener('keypress', () => {
    seen.keypress = true;
  });

  containKeyEvents(host);
  dispatchBubbling([inner, host, win], makeKeyEvent('keyup', { key: 'c', path: [editorTarget(), inner, host, win] }));
  dispatchBubbling([inner, host, win], makeKeyEvent('keypress', { key: 'd', path: [editorTarget(), inner, host, win] }));

  assert.equal(seen.keyup, false);
  assert.equal(seen.keypress, false);
});

// --- Designer-shortcut survival (the follow-up fix's whole point) --------

test('Delete from canvas focus reaches window (designer delete-element survives)', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'Delete', path: [canvasTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, true, "Delete must reach the designer's own window-level canvas-keyboard.ts handler");
  assert.equal(event.stopped, false);
});

test('ctrl+z from canvas focus reaches window (designer undo survives)', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'z', ctrlKey: true, path: [canvasTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, true, "ctrl+z must reach the designer's own undo-keyboard.ts handler");
  assert.equal(event.stopped, false);
});

test('ArrowLeft from canvas focus reaches window (designer nudge survives)', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'ArrowLeft', path: [canvasTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, true, "ArrowLeft must reach the designer's own nudge handler");
  assert.equal(event.stopped, false);
});

test('Escape from canvas focus reaches window (designer deselect survives)', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', { key: 'Escape', path: [canvasTarget(), inner, host, win] });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, true, "Escape must reach the designer's own deselect handler");
  assert.equal(event.stopped, false);
});

test('ctrl+shift+z (redo) from canvas focus reaches window', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  containKeyEvents(host);
  const event = makeKeyEvent('keydown', {
    key: 'z',
    ctrlKey: true,
    shiftKey: true,
    path: [canvasTarget(), inner, host, win],
  });
  dispatchBubbling([inner, host, win], event);

  assert.equal(windowSaw, true, "ctrl+shift+z (redo) must reach the designer's own undo-keyboard.ts handler");
});

// --- The returned disposer -------------------------------------------------

test('the returned disposer actually removes the listeners', () => {
  const { inner, host, win } = threeNodeMount();
  let windowSaw = false;
  win.addEventListener('keydown', () => {
    windowSaw = true;
  });

  const dispose = containKeyEvents(host);
  dispose();

  dispatchBubbling([inner, host, win], makeKeyEvent('keydown', { key: 'e', path: [editorTarget(), inner, host, win] }));

  assert.equal(windowSaw, true, 'after disposal, events must bubble through normally again (e.g. on unmount)');
});

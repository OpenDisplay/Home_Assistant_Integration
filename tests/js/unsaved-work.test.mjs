// Unit tests for hasUnsavedWork/makeBeforeUnloadHandler (tier-1 round 2,
// finding 6). Plain `node --test`, zero dependencies -- see
// key-containment.test.mjs's own header comment for why this repo tests
// panel JS this way.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { hasUnsavedWork, makeBeforeUnloadHandler } from '../../custom_components/opendisplay/designer/frontend/panel/unsaved-work.js';

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

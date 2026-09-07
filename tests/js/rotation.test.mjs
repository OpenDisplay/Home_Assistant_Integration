// Unit tests for rotateDeltaFor (tier-2 real-hardware finding: a base-rotated
// display's SERVER preview rendered content as if the designer's Orientation
// control hadn't been applied). Plain `node --test`, zero dependencies -- see
// key-containment.test.mjs's own header comment for why this repo tests
// panel JS this way.
//
// Pins EXACT delta values (not just quarter/half-turn parity) for every
// (base, target-orientation) combination in {0, 90, 180, 270}^2 -- a bug that
// swapped the subtraction order would still pass a parity-only check (a 90°
// and a 270° rotation transpose width/height identically) while shipping
// mirrored/sideways content. Expected values are derived directly from
// services.py's own documented contract (rotate = target - base, mod 360;
// see rotation.js's own doc comment for the full derivation), independent of
// this module's implementation.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { rotateDeltaFor } from '../../custom_components/opendisplay/designer/frontend/panel/rotation.js';

const ORIENTATIONS = [0, 90, 180, 270];

function expectedDelta(base, target) {
  return ((target - base) % 360 + 360) % 360;
}

test('rotateDeltaFor matches (target - base) mod 360 for every combination', () => {
  for (const base of ORIENTATIONS) {
    for (const target of ORIENTATIONS) {
      const displaySpec = { rotationDegrees: base };
      const actual = rotateDeltaFor(displaySpec, target);
      assert.equal(
        actual,
        expectedDelta(base, target),
        `base=${base} target=${target}: got ${actual}`
      );
    }
  }
});

test('an untouched Orientation control (target === base) needs no extra rotate', () => {
  // The designer's canvas orientation is seeded from the pushed display
  // rotationDegrees (base); an un-clicked control reports exactly base, and
  // the device's own fixed mounting already achieves that view -- no
  // additional `rotate` should ever be sent for the seeded default.
  for (const base of ORIENTATIONS) {
    assert.equal(rotateDeltaFor({ rotationDegrees: base }, base), 0);
  }
});

test('a base-rotated display (base=90) needs a non-zero rotate for every other orientation', () => {
  // The regression this module guards: base=90, target=90 (the designer's
  // Orientation control set to 90, matching a physically landscape-mounted
  // panel) must NOT collapse to rotate=0 -- that specific collapse is what
  // made the server render as if the rotation weren't applied.
  assert.equal(rotateDeltaFor({ rotationDegrees: 90 }, 90), 0);
  assert.equal(rotateDeltaFor({ rotationDegrees: 90 }, 0), 270);
  assert.equal(rotateDeltaFor({ rotationDegrees: 90 }, 180), 90);
  assert.equal(rotateDeltaFor({ rotationDegrees: 90 }, 270), 180);
});

test('missing/undefined display spec falls back to base=0', () => {
  assert.equal(rotateDeltaFor(undefined, 90), 90);
  assert.equal(rotateDeltaFor(null, 270), 270);
  assert.equal(rotateDeltaFor({}, 180), 180);
});

test('the maintainer\'s real ESL 5 3.5" device (tier-2 acceptance vector)', () => {
  // rotationDegrees (base) = 0 -- native portrait 184x384, no base
  // persisted; physically mounted landscape, his own working automation
  // passes rotate: 270 on every drawcustom call. Setting the designer's
  // Orientation control to 270 must derive exactly that.
  const displaySpec = {
    pixelWidth: 184,
    pixelHeight: 384,
    rotationDegrees: 0,
    renderWidth: 184,
    renderHeight: 384,
  };
  assert.equal(rotateDeltaFor(displaySpec, 270), 270);
});

test('a non-numeric target falls back to 0', () => {
  assert.equal(rotateDeltaFor({ rotationDegrees: 90 }, undefined), 270);
  assert.equal(rotateDeltaFor({ rotationDegrees: 0 }, NaN), 0);
});

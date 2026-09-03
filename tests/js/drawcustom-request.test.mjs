// Unit tests for the panel's two request builders (drawcustom-request.js).
//
// What this file exists to pin (designer 3.0.0, issue #105 WYSIWYG-send
// slice): SEND READS THE ACTION CONTEXT. The builders are pure functions of
// (elements, pushed display spec, designer context) with no state at all --
// which is the point. The panel used to remember the last preview's dither
// and rotate (`_lastPreviewDitherHA`/`_lastPreviewRotate`) and reuse them at
// Send time, so a Send with no preview ever run shipped `dither: 'none'`,
// `rotate: 0` regardless of what the designer's controls showed -- sideways
// content on a rotated display, on real hardware. Those fields are gone. A
// pure builder cannot have that bug: there is nowhere for a stale value to
// live, and these tests are written so a reintroduction (a module-level
// cache, a "first call wins" memo) fails them.
//
// Plain `node --test`, zero dependencies -- see key-containment.test.mjs's
// own header comment for why this repo tests panel JS this way.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ditherToHaString,
  renderRequestBody,
  sendCallData,
} from '../../custom_components/opendisplay/designer/frontend/panel/drawcustom-request.js';

/** A frozen HostActionContext/HostPreviewContext, as the designer hands one over. */
function context({ targetId, width = 384, height = 184, rotation = 0, dither = 0 }) {
  return Object.freeze({
    targetId,
    display: Object.freeze({ width, height, rotation }),
    render: Object.freeze({ dither }),
  });
}

const ELEMENTS = [{ type: 'text', value: 'hi' }];

// The maintainer's real ESL 5 3.5": native portrait 184x384, no base
// rotation persisted, physically mounted landscape.
const ESL5 = { pixelWidth: 184, pixelHeight: 384, rotationDegrees: 0 };

test('ditherToHaString maps the designer domain onto the service vocabulary', () => {
  assert.equal(ditherToHaString(0), 'none');
  assert.equal(ditherToHaString(1), 'ordered');
  assert.equal(ditherToHaString(2), 'ordered');
  // Anything the designer has not published a meaning for stays on the safe
  // side rather than silently shipping flat output.
  assert.equal(ditherToHaString(undefined), 'ordered');
  assert.equal(ditherToHaString(7), 'ordered');
});

test('send with NO preview ever run still carries the live rotate and dither', () => {
  // THE regression this round removes. Nothing has previewed; the only
  // input is the action context, which reports the user's live Orientation
  // (270) and dither (2 -> 'ordered') controls.
  const data = sendCallData(ELEMENTS, ESL5, context({ targetId: 'dev1', rotation: 270, dither: 2 }));

  assert.equal(data.rotate, 270, 'rotate must come from context.display.rotation');
  assert.equal(data.dither, 'ordered', 'dither must come from context.render.dither');
  assert.deepEqual(data.device_id, ['dev1']);
  assert.equal(data.background, 'white');
  assert.equal(data.refresh_type, 'full');
  assert.equal(data.payload, ELEMENTS);
});

test('two sends in a row follow the controls -- no value survives between calls', () => {
  // A sticky field (the old `_lastPreviewRotate` shape, or any memo added
  // later) would make the second call answer with the first call's values.
  const first = sendCallData(ELEMENTS, ESL5, context({ targetId: 'dev1', rotation: 270, dither: 2 }));
  const second = sendCallData(ELEMENTS, ESL5, context({ targetId: 'dev1', rotation: 0, dither: 0 }));

  assert.equal(first.rotate, 270);
  assert.equal(first.dither, 'ordered');
  assert.equal(second.rotate, 0);
  assert.equal(second.dither, 'none');
});

test('send subtracts the target display spec base rotation, not the raw orientation', () => {
  // base=90 panel with the Orientation control left at its seeded 90: the
  // device's own mounting already achieves that view, so no extra rotate.
  const base90 = { rotationDegrees: 90 };
  assert.equal(sendCallData(ELEMENTS, base90, context({ targetId: 'd', rotation: 90 })).rotate, 0);
  assert.equal(sendCallData(ELEMENTS, base90, context({ targetId: 'd', rotation: 0 })).rotate, 270);
  assert.equal(sendCallData(ELEMENTS, base90, context({ targetId: 'd', rotation: 180 })).rotate, 90);
});

test('send with no pushed display spec for the target falls back to base=0', () => {
  // `_targetDisplaySpecs.get()` returns undefined for a target whose
  // attributes have not landed; the absolute orientation then IS the delta.
  assert.equal(
    sendCallData(ELEMENTS, undefined, context({ targetId: 'd', rotation: 180 })).rotate,
    180
  );
});

test('preview and send derive the same dither and rotate from one context', () => {
  // WYSIWYG: whatever the preview asked the endpoint to render is exactly
  // what a Send in that same instant ships to the panel.
  const ctx = context({ targetId: 'dev1', rotation: 270, dither: 2 });
  const preview = renderRequestBody(ELEMENTS, ESL5, ctx);
  const send = sendCallData(ELEMENTS, ESL5, ctx);

  assert.equal(preview.rotate, send.rotate);
  assert.equal(preview.dither, send.dither);
  assert.equal(preview.device_id, 'dev1'); // endpoint takes a bare id
  assert.deepEqual(send.device_id, ['dev1']); // the service takes a list
});

test('preview of the Virtual display sends geometry instead of a device, rotate 0', () => {
  // context.targetId is undefined for the designer's built-in Virtual pick;
  // context.display is already the final oriented surface, so there is no
  // base rotation to recover a delta against.
  const body = renderRequestBody(
    ELEMENTS,
    undefined,
    context({ targetId: undefined, width: 400, height: 300, rotation: 90, dither: 2 })
  );

  assert.deepEqual(body.display, { width: 400, height: 300 });
  assert.equal('device_id' in body, false);
  assert.equal(body.rotate, 0);
  assert.equal(body.dither, 'ordered');
});

test('the builders never mutate the frozen designer context', () => {
  // Both context objects are frozen since 3.0.0 -- a builder that tried to
  // normalize a field in place would throw in strict mode (ES modules are
  // always strict).
  const ctx = context({ targetId: 'dev1', rotation: 90, dither: 2 });
  renderRequestBody(ELEMENTS, ESL5, ctx);
  sendCallData(ELEMENTS, ESL5, ctx);
  assert.equal(ctx.display.rotation, 90);
  assert.equal(ctx.render.dither, 2);
});

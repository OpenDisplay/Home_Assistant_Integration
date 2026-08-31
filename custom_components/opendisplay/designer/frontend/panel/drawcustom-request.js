/**
 * The two drawcustom-shaped requests this panel builds — the render
 * endpoint's preview body and the `opendisplay.drawcustom` service call —
 * derived from ONE source: the designer context handed to the callback that
 * is asking.
 *
 * Since designer 3.0.0 (issue #105, the WYSIWYG-send slice) `onAction`'s
 * `HostActionContext` carries the same live `display` geometry
 * (`HostDisplayGeometry`) and `render` options (`HostRenderOptions`) that
 * `renderPreview`'s `HostPreviewContext` always carried, both read at the
 * instant the callback fires. That is what lets Send read the designer's
 * CURRENT orientation and dither controls directly. Before it, a host
 * reaching for WYSIWYG send had no choice but to remember the last preview
 * request's values — sticky, invisible, and wrong the moment a control moved
 * with preview off or unused. Those remembered fields are gone; nothing in
 * this panel stores a dither or a rotate between callbacks any more.
 *
 * Both builders live here, together, so preview and send provably derive the
 * same `dither`/`rotate` from the same context rather than two lookalike
 * expressions that can drift (`tests/js/drawcustom-request.test.mjs` pins
 * exactly that).
 */
import { rotateDeltaFor } from './rotation.js';

/**
 * Designer's own numeric dither domain (`HostRenderOptions.dither`: 0 flat/
 * none, 1 reserved, 2 ordered halftone — the vendored `.d.ts`: "the
 * designer's preview control produces 0 or 2 today") mapped onto the
 * drawcustom service's string `dither` options (services.yaml).
 * Deliberately a string lookup, not the raw int: the service's `dither`
 * field also accepts an int matching the DitherMode enum's own value order
 * (see `_dither_value` in services.py), but that ordering isn't published
 * anywhere this panel can read — forwarding the designer's int blind would
 * gamble on an enum layout instead of the documented string vocabulary. `1`
 * is currently unreachable from the designer's own dither control; mapped
 * conservatively to 'ordered' pending upstream clarification (see the PR
 * body's open questions).
 */
const DITHER_TO_HA_STRING = { 0: 'none', 1: 'ordered', 2: 'ordered' };

/** `HostRenderOptions.dither` -> the drawcustom service's own string vocabulary. */
export function ditherToHaString(dither) {
  return DITHER_TO_HA_STRING[dither] ?? 'ordered';
}

/**
 * Body for `POST /api/opendisplay/designer/render` (`renderPreview`).
 *
 * Virtual display (tier-1 round 2, finding 2): `context.targetId` is
 * undefined/null for the designer's built-in "Virtual display" pick — there
 * is no HA device to send a `device_id` for at all. `context.display`
 * (width/height, already the oriented logical drawing surface — see
 * `HostDisplayGeometry`'s own doc comment in the vendored `.d.ts`) is ALWAYS
 * present regardless of `targetId`, so that geometry alone is enough for the
 * endpoint's spec mode; `rotate` is always 0 there because `context.display`
 * is already the final oriented surface, with no separate device base
 * rotation to recover a delta against.
 *
 * @param {object[]} elements Parsed drawcustom payload.
 * @param {{rotationDegrees?: number}|undefined} displaySpec The selected
 *   target's own pushed `HostDisplaySpec`, or undefined for Virtual.
 * @param {{targetId?: string, display: {width: number, height: number, rotation: number}, render: {dither: number}}} context
 */
export function renderRequestBody(elements, displaySpec, context) {
  const common = {
    payload: elements,
    background: 'white',
    dither: ditherToHaString(context.render.dither),
  };
  if (!context.targetId) {
    return {
      display: { width: context.display.width, height: context.display.height },
      ...common,
      rotate: 0,
    };
  }
  return {
    device_id: context.targetId,
    ...common,
    rotate: rotateDeltaFor(displaySpec, context.display.rotation),
  };
}

/**
 * Service data for `opendisplay.drawcustom` (the `send` host action).
 *
 * `background`/`refresh_type` are still hardcoded (designer issue #105 will
 * expose the rest of the option set later); `dither` and `rotate` are read
 * LIVE off the action context, so what Send ships is what the designer's own
 * controls show at the moment of the click — no preview required, nothing
 * remembered from one.
 *
 * @param {object[]} elements Parsed drawcustom payload.
 * @param {{rotationDegrees?: number}|undefined} displaySpec The selected
 *   target's own pushed `HostDisplaySpec`.
 * @param {{targetId?: string, display: {rotation: number}, render: {dither: number}}} context
 */
export function sendCallData(elements, displaySpec, context) {
  return {
    // device_id here is the service's own required field (services.yaml's
    // `device_id` selector) -- not a duplicate of HA's separate service-call
    // "target" (which attributes the call to a device in the logbook/trace
    // UI, independent of what the service schema itself requires).
    device_id: [context.targetId],
    payload: elements,
    background: 'white',
    dither: ditherToHaString(context.render.dither),
    rotate: rotateDeltaFor(displaySpec, context.display.rotation),
    refresh_type: 'full',
  };
}

/**
 * Rotation delta derivation shared by preview (`renderPreview`) and send
 * (`onAction`'s `send`).
 *
 * The render endpoint's `rotate` field (and the `opendisplay.drawcustom`
 * service's identical field, from which the endpoint is deliberately not
 * allowed to drift -- see `docs/designer.md` and `tests/test_rotation_parity.py`
 * in the Python integration) is a DELTA on top of the device's stored BASE
 * rotation, not an absolute value. `_drawcustom_for_device`'s own contract
 * (services.py): "the payload is authored against the FINAL on-screen
 * orientation; the device applies (base + rotate)". Solving that for
 * `rotate`:
 *
 *     rotate = target - base   (mod 360)
 *
 * where `target` is the final on-screen orientation the payload assumes.
 * The designer's own canvas orientation control (the 0/90/180/270 buttons
 * next to Display Config) reports exactly that target, absolutely, as
 * `context.display.rotation` -- independent of whatever base rotation the
 * target display's own `capabilities.rotation_degrees` carries (issue #139:
 * `HostPreviewDisplayGeometry` is always already oriented for whatever the
 * designer's own control currently shows). `base` is the same target's
 * pushed `capabilities.rotation_degrees` -- the device's fixed mounting
 * rotation, read-only today (per-device persistent orientation is a
 * deferred upstream feature; every base-rotated panel currently needs an
 * explicit `rotate` on every call, matching the maintainer's own real
 * automation).
 *
 * This is intentionally NOT "compare dimensions": composing two
 * independent quarter-turns (the device's fixed base, then whatever the
 * designer's own orientation toggle adds on top) is associative, so the
 * LOGICAL SURFACE the SERVER's `generate_image` canvas is built at for
 * (base, rotate) and what the DESIGNER'S OWN canvas shows for a chosen
 * target orientation agree for every (base, target) combination -- proven
 * directly by `tests/js/rotation.test.mjs`'s full matrix and, Python-side,
 * by `tests/test_rotation_parity.py`'s dimension/content-orientation
 * assertions (NOT "render endpoint bytes == drawcustom send-path bytes" --
 * that was this file's own claim through a tier-2 round-1 investigation
 * that missed a real bug; see that test module's docstring for the
 * corrected story: preview's `prepare_image` call must target the LOGICAL
 * surface with no device-facing rotation, not the send path's own raw
 * device grid, even though this delta FORMULA was correct the whole time).
 * A bug that swapped the subtraction order (`base - target` instead of
 * `target - base`) would still pass a DIMENSION-only check -- quarter/
 * half-turn parity is sign-symmetric, a 90° and a 270° rotation transpose
 * width/height identically -- while shipping mirrored/sideways CONTENT.
 * The test matrix pins exact delta values, not just parity, specifically
 * to catch that class of bug.
 *
 * @param {{rotation_degrees?: number}|null|undefined} capabilities The
 *   target's own pushed capabilities (`HostCapabilities`), or undefined/null
 *   when none have been published for it yet (falls back to base=0 -- the
 *   same "no rotation known yet" default an untouched, never-configured
 *   device already has).
 * @param {number} targetOrientation The designer's live, absolute canvas
 *   orientation (`context.display.rotation`): 0, 90, 180, or 270.
 * @returns {number} The `rotate` value the render endpoint / drawcustom
 *   service expects: 0, 90, 180, or 270.
 */
export function rotateDeltaFor(capabilities, targetOrientation) {
  const baseRotation = Number(capabilities?.rotation_degrees) || 0;
  const target = Number(targetOrientation) || 0;
  return ((target - baseRotation) % 360 + 360) % 360;
}

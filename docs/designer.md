# Designer

The OpenDisplay Designer is a visual drawcustom editor available from the Home
Assistant sidebar ("OpenDisplay Designer"). It is a vendored
[`@schlomo/odl-drawcustom-designer`](https://github.com/schlomo/odl-drawcustom-designer)
build embedded via a thin panel wrapper this integration owns; the designer
itself never talks to Home Assistant directly.

## Contents
- [Architecture](#architecture)
- [Preview isolation](#preview-isolation)
- [The render endpoint](#the-render-endpoint)
- [Updating the vendored library](#updating-the-vendored-library)
- [Known gaps](#known-gaps)

## Architecture

**The designer owns the UI; the integration provides data.** Everything the
panel wrapper (`custom_components/opendisplay/designer/frontend/panel/
opendisplay-designer-panel.js`) does is either push data in or react to a
callback out — no host-built toolbar, device picker, Copy YAML button or Save
button exists; those are the designer's own `targets` picker, built-in Copy
YAML, and a `send` host action this integration registers.

| Designer concept | What this integration supplies it from |
|---|---|
| `targets` | Every HA device on the `opendisplay` platform with published capability attributes (`designer/capabilities.py`, gated on `pixel_width > 0` so a device whose capabilities haven't landed yet, or failed to build, never becomes a fabricated target) |
| `states` | Every HA entity state (`hass.states`), with `attributes.friendly_name` promoted to the designer's `name` field |
| `actions` | One button, `send`, disabled with a human reason while sending, the YAML is invalid, or no display is selected |
| `onAction('send', …)` | Calls the `opendisplay.drawcustom` service against the selected target's device |
| `renderPreview` | POSTs to this integration's own render endpoint (below) and hands back the PNG bytes |

Each real OpenDisplay device's `image.*` entity carries its designer
capability attributes (`pixel_width`, `pixel_height`, `render_width`,
`render_height`, `rotation_degrees`, `color_scheme`, `accent_color`,
`available_colors`, `color_map`, `palette_measured` — the designer's own
`HostCapabilities` shape) via `designer/image_entity.py`, published on
`async_added_to_hass` and refreshed on read.

## Preview isolation

**Designer play must never impact anything around a live display**
(maintainer ruling). Turning a payload into a picture while editing is
completely separate from sending it to a real panel:

- **Preview** (`renderPreview`) → `POST /api/opendisplay/designer/render` →
  rendered PNG bytes, straight back to the designer. No image-entity write,
  no `SIGNAL_IMAGE_UPDATED` dispatch, no BLE delivery, nothing above a debug
  log line.
- **Send** (`send` action) → `opendisplay.drawcustom` service call → the
  real send path: renders, dithers, uploads (or queues for a sleeping
  device), and updates the target's `image.*` entity.

A dashboard showing a device's real `image.*` entity is guaranteed to never
change because someone was previewing a design for that same device — the
preview path has no code path that could write to it.

## The render endpoint

`POST /api/opendisplay/designer/render` — authenticated (`requires_auth =
True`; the panel calls it with `hass.fetchWithAuth`, the standard pattern
for a custom panel to fetch a binary resource with the user's own HA
session). This is the integration's first HTTP view.

Request body:

```json
{
  "device_id": "<HA device registry id>",
  "payload": [ /* drawcustom elements, same shape as the service's payload */ ],
  "background": "white",
  "dither": "burkes",
  "rotate": 0
}
```

`background`, `dither` and `rotate` are optional, defaulting exactly as
`opendisplay.drawcustom`'s own schema does. `dither` accepts the same names
(`none`, `burkes`, `ordered`, …) and legacy numeric values the service
accepts.

**Template values are expanded server-side**, in Home Assistant's own
sandboxed Jinja environment — exactly as `opendisplay.drawcustom` itself
does (`services.py`'s `render_payload_templates`, one shared function used
by both call sites, so a preview and a real send can never disagree about
what a templated field evaluates to). A field referencing a state that
doesn't exist yet **degrades rather than errors**: HA's own template
functions (`is_state`, `is_state_attr`, …) return a sensible default for a
merely-missing entity instead of raising, so a template written against a
not-yet-onboarded device still renders through cleanly. Only a template
that actually raises (a broken reference, not a missing state) is treated
as an error — see the `400` row below.

Response: `200` with `Content-Type: image/png` and the rendered bytes, at
the target device's exact render resolution (already transposed for a
quarter-turn rotation, matching the service's own canvas-orientation rule).
Errors:

| Status | When |
|---|---|
| `400` | Malformed JSON, a schema violation (e.g. `payload` isn't a list), a payload odl-renderer can't render, or a broken template in one element (`{"message": "drawcustom payload element <index> (type '<type>') has an invalid template: <reason>"}`) |
| `401` | No valid Home Assistant auth |
| `404` | `device_id` doesn't resolve to a loaded OpenDisplay config entry |

**Implementation shares two calls with the send path**: `generate_image`
(odl-renderer) followed by `prepare_image`'s dither + quantize step
(`opendisplay`), with `compress=False` (no point building the
BLE-upload-ready compressed payload for a preview) and nothing called after
it — no upload, no queue, no entity write, no dispatched signal.

`prepare_image` is called with the **same `tone`/`use_measured_palettes`
values `_drawcustom_for_device` derives for a call that supplies neither
`tone_compression` nor `measured_palette`** — `tone="auto"`,
`use_measured_palettes=False` (`SCHEMA_DRAWCUSTOM`'s own defaults). This is
deliberate and load-bearing, not incidental: passing neither kwarg at all
would silently pick up `prepare_image`'s *own*, different defaults
(`tone=0.0`, `use_measured_palettes=True`) instead, which render visibly
differently on any panel with a measured palette (adversarial review
finding B1 — `tests/test_designer_render.py`'s
`test_render_uses_send_paths_defaults_not_prepare_images_own` pins this on
a real measured-palette IC so the parity gap is pixel-observable, not just
a kwarg-inspection nicety). The payload element count is capped
(`_MAX_ELEMENTS`, currently 1000, `400` above it) — see "Known gaps" for
why `generate_image` itself still runs on the event loop rather than in an
executor.

The panel wrapper derives the request's `rotate` field from the designer's
**live** canvas orientation, not from the device's stored base rotation:
the 0°/90°/180°/270° orientation control next to Display Config lets the
user pick an effective orientation independent of what `capabilities.
rotation_degrees` published, and `renderPreview`'s `context.display.
rotation` reports whichever one is currently showing. `_rotateDeltaFor()`
compares that against the target's own pushed `rotation_degrees` (its
base) to recover the delta the endpoint's `rotate` field expects — sending
`rotate: 0` unconditionally (as an earlier version of this endpoint's
caller did) meant a preview of a rotated canvas letterboxed against an
image rendered for the wrong orientation.

## Updating the vendored library

The designer ships vendored from npm rather than as a git dependency (Home
Assistant custom components can't `npm install` at runtime). See
[`custom_components/opendisplay/designer/frontend/vendor/README.md`](../custom_components/opendisplay/designer/frontend/vendor/README.md)
for the full procedure; in short:

```bash
scripts/update-designer-vendor.py             # re-verify the current pin
scripts/update-designer-vendor.py --pin 2.7.0  # bump to a new release
git diff custom_components/opendisplay/designer/frontend/vendor/
```

Both modes verify the npm-registry-declared `sha512` integrity against the
actual downloaded bytes before writing anything to `vendor/` — a mismatch
exits non-zero and changes nothing. After bumping the pin, review the diff
of `odl-drawcustom-designer.d.ts` against the panel wrapper's own usage
(`../panel/opendisplay-designer-panel.js`) — the wrapper is hand-written
against the 2.x host contract and is not regenerated by the script.

## Known gaps

- **`rotation_degrees` still publishes only the base panel rotation** in
  `HostCapabilities` (`capabilities.py`'s `user_rotate_deg` is always `0` —
  no host seam exists yet to carry a live rotate choice into
  `build_capabilities`). **Preview** works around this at the point of use
  (`_rotateDeltaFor`, above) by comparing the designer's own live canvas
  orientation against that base value, so a rotated preview renders
  correctly today despite the gap. **Send does not**: `onAction`'s
  `HostActionContext` carries only `targetId`, with no equivalent geometry
  to derive a delta from, so the `drawcustom` service call `send` makes
  never carries a `rotate` value at all (stays at the service's own
  default). Sending a payload the user rotated in the designer's own
  control can therefore ship un-rotated. Flagged rather than silently
  "fixed" against a seam that doesn't exist yet on the `onAction` side.
- **Send's `dither` value is a sticky memory of the last preview**, not a
  live read of the designer's current dither control (same
  `HostActionContext` limitation as above — it carries only `targetId`, not
  the `HostPreviewServiceOptions` a `renderPreview` call receives). Defaults
  to `'none'` (the designer's own default dither control state) if no
  preview ran this session — see the PR body's open questions for the
  upstream seam both of these need (`odl-drawcustom-designer` issue #105
  territory: extending `HostActionContext` the same way `HostPreviewContext`
  already carries geometry and service options).
- **The `dry-run` field on `opendisplay.drawcustom` still ignores `dither`**
  (it always renders the flat, un-dithered image) — an independent,
  pre-existing gap the render endpoint above does not share (it always
  dithers with the send path's own tone/measured-palette values — see "The
  render endpoint" above), noted here rather than silently fixed as part of
  this work.
- **`generate_image` still runs on the event loop**, inside the render
  endpoint exactly like it already does in `_drawcustom_for_device`'s own
  send path — moving it into an executor would mean handing a loop-bound
  `aiohttp.ClientSession` across threads (unsafe: a session is not safe to
  use off the loop that created it) or forking `odl_renderer`'s own
  async/CPU-bound mix, neither of which this endpoint should do
  unilaterally. Bounded instead by the payload element cap (`_MAX_ELEMENTS`
  = 1000, `400` above it): measured directly against a large canvas
  (800×480, matching the fabricated large BWRY panel) with 1000 short text
  elements at ~0.22–0.25s per render, worst case observed. A smaller canvas
  or simpler elements render considerably faster (a 296×128 canvas with the
  same 1000 elements measured ~0.02s). The render endpoint is called far
  more often than a deliberate send (once per debounced live-preview edit),
  which is why it gets this cap and the send path does not.
- **`capabilities.py`'s canvas palette and `drawcustom`'s send default
  disagree on `use_measured_palettes`.** `build_capabilities` calls
  `get_palette_for_display(panel_ic_type, scheme)` with no
  `use_measured` argument, taking that function's own default (`True`) for
  the color swatches/`color_map` the designer's canvas shows while editing.
  `opendisplay.drawcustom` (and this PR's render endpoint, matching it —
  see "The render endpoint" above) both default `measured_palette`/
  `use_measured_palettes` to `False` for the actual render. On a
  measured-palette panel this means the canvas the user designs against can
  show slightly different colors than what a **Send** with default options
  actually ships (a **Display preview**, by contrast, always matches Send
  exactly, since both go through the same endpoint/service defaults). Which
  default should win end-to-end is a real open question for the
  maintainers, not something this PR resolves unilaterally — see the PR
  body's open asks.
- **No `resolveAsset` provider is wired up.** Fonts referenced by name in a
  payload (e.g. a `.ttf` in `www/fonts`/`media/fonts`) render correctly
  server-side (`generate_image`'s own font-directory search, unchanged from
  the `drawcustom` service) but the designer's own client-side canvas has no
  way to resolve the same name locally — the designer's `resolveAsset` host
  option (docs: "the host asks for any asset reference left over after its
  own tiers") is simply never supplied, so a payload referencing a
  host-only font renders correctly through **Send** and through **Display
  preview** (both go through the real backend), but shows the designer's
  own client-side render-error state in the **non-preview canvas view**
  (its default rendering, without Display preview toggled on) for that same
  element. Two contradictory-looking views of the same payload, from a real
  gap rather than a bug in either view. A follow-up asset endpoint (`GET
  /api/opendisplay/designer/asset/<kind>/<name>` or similar, feeding
  `resolveAsset`) would close this — intentionally not built in this round;
  filed as planned follow-up work, not fixed here.

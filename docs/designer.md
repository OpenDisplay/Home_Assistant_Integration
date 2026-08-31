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
- [The asset endpoint](#the-asset-endpoint)
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
| `onAction('send', …)` | Calls the `opendisplay.drawcustom` service against the selected target's device, with `dither` and `rotate` read live off the action context at click time |
| `renderPreview` | POSTs to this integration's own render endpoint (below) and hands back the PNG bytes |
| `resolveAsset` | GETs this integration's own asset endpoint (below) for a font the designer couldn't resolve locally, and hands back the bytes |

Each real OpenDisplay device's `image.*` entity carries its designer
display attributes (`pixel_width`, `pixel_height`, `render_width`,
`render_height`, `rotation_degrees`, `color_scheme`, `accent_color`,
`available_colors`, `color_map`, `palette_measured`) via
`designer/image_entity.py`, published on `async_added_to_hass` and
refreshed on read.

**Those attribute names are snake_case and stay that way** — they are Home
Assistant entity attributes, and HA's own convention governs them. The
designer's matching type, `HostDisplaySpec` (renamed from
`HostCapabilities` in designer 3.0.0, and camelCase throughout since the
same release), is a different vocabulary: `pixelWidth`, `rotationDegrees`,
`paletteMeasured`, and so on. The two meet in exactly one function — the
panel wrapper's `displaySpecFromAttrs()` — whose result is pushed as a
target's `display` field (`HostTarget.display`, renamed from
`.capabilities` in 3.0.0). Neither side is "aligned" to the other.

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

Request body — **either `device_id` or `display` is required** (at least
one, not exactly one — see below):

```json
{
  "device_id": "<HA device registry id>",
  "payload": [ /* drawcustom elements, same shape as the service's payload */ ],
  "background": "white",
  "dither": "burkes",
  "rotate": 0
}
```

For the designer's built-in **Virtual display** pick, there is no HA device
at all (`context.targetId` is `null`) — the panel sends an explicit
`display` spec instead of `device_id`:

```json
{
  "display": { "width": 384, "height": 184, "color_scheme": 0 },
  "payload": [ /* … */ ]
}
```

`generate_image`/`prepare_image` never actually needed a device object,
only geometry and a palette, so a syntactically real but device-less
`GlobalConfig` is built from the spec (`_synthetic_global_config`,
`render.py`). `color_scheme` is optional (default `0`/MONO, the same
numeric vocabulary `designer/capabilities.py` already publishes as
`color_scheme` to the panel) — the designer's `renderPreview` context
(`HostPreviewContext.display`) carries only width/height/rotation, and the
designer keeps its own color-mode control entirely inside its chrome
(ADR-018: no host UI for it), so this host genuinely has no way to know
which color scheme the user picked for Virtual and does not guess at one;
the panel itself never sends `color_scheme` at all today.

**Precedence if a request somehow carries both**: `device_id` wins,
silently — the schema only requires *at least* one of the two, not
*exactly* one (see `render.py`'s own comment on `_SCHEMA` for why: nothing
rejects a request carrying both). This has no live caller today — the
panel's `renderPreview` sends exactly one, gated on whether
`context.targetId` is `null` — documented here so a future direct API
caller (or a designer change) doesn't have to read the source to find out.

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
the **logical drawing surface's** resolution — the same shape the designer's
own canvas is at when it made the request (`HostDisplayGeometry`,
vendored `.d.ts`: "the logical drawing surface the payload is authored
against ... never the raw physical panel size"), already transposed for a
quarter-turn rotation. This is deliberately **not** the target device's raw
native pixel grid whenever the two differ (see the tier-2 root-cause note
below for the bug this distinction fixes) — that grid is what the send path
uploads to, not what preview returns. Errors:

| Status | When |
|---|---|
| `400` | Malformed JSON, a schema violation (e.g. `payload` isn't a list), neither `device_id` nor `display` supplied (`{"message": "either device_id or display (width/height) is required"}`), a payload odl-renderer can't render, or a broken template in one element (`{"message": "drawcustom payload element <index> (type '<type>') has an invalid template: <reason>"}`) |
| `401` | No valid Home Assistant auth |
| `404` | `device_id` doesn't resolve to a loaded OpenDisplay config entry |

**Implementation shares two calls with the send path**: `generate_image`
(odl-renderer) followed by `prepare_image`'s dither + quantize step
(`opendisplay`), with `compress=False` (no point building the
BLE-upload-ready compressed payload for a preview) and nothing called after
it — no upload, no queue, no entity write, no dispatched signal.

**One deliberate divergence from the send path** (tier-2 round 2 fix):
`prepare_image` is called with an explicit `DeviceCapabilities` describing
the LOGICAL surface itself (`width`/`height` = the already-transposed
`generate_image` canvas, `rotation=0`) and `rotate=Rotation.ROTATE_0` —
**not** the real device's own capabilities and the request's `rotate`
value, which is what the send path passes. `prepare_image`'s own
`rotate`/target-size handling is DEVICE-FACING: given the real device
capabilities it always fits its output to the raw native pixel grid
regardless of what `rotate` is passed, composing that `rotate` with the
device's stored base rotation on top. That is exactly right for a real
upload (the device needs its own native buffer) and exactly wrong for
preview (which needs the logical surface, untouched) — see the root-cause
note below for the bug this now avoids. `config` is still passed alongside
the synthetic capabilities, for `panel_ic_type`/palette derivation from the
real device.

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

### The rotation mapping

**In one sentence:** the designer reports an ABSOLUTE on-screen orientation
(`context.display.rotation`, the orientation `context.display.width`/
`height` are already expressed in), while `rotate` — on this endpoint and
on `opendisplay.drawcustom` alike — is a DELTA the device composes onto its
own stored base rotation, so the panel converts with `rotate =
(context.display.rotation − target.display.rotationDegrees) mod 360`.

The panel wrapper therefore derives `rotate` from the designer's **live**
canvas orientation, not from the device's stored base rotation: the
0°/90°/180°/270° orientation control next to Display Config lets the user
pick an effective orientation independent of what `rotation_degrees`
published, and `context.display.rotation` reports whichever one is
currently showing. `rotateDeltaFor()` (`frontend/panel/rotation.js`,
extracted from the panel wrapper for its own unit tests —
`tests/js/rotation.test.mjs`) compares that against the target's own pushed
`display.rotationDegrees` (its base) to recover the delta — sending
`rotate: 0` unconditionally (as an earlier version of this endpoint's
caller did) meant a preview of a rotated canvas letterboxed against an
image rendered for the wrong orientation.

**Both channels read the same live context.** Since designer 3.0.0,
`onAction`'s `HostActionContext` carries the same frozen `display`
geometry and `render` options (`HostDisplayGeometry`, `HostRenderOptions`)
that `renderPreview`'s context always carried, read at the instant the
button is clicked. Preview and Send build their requests from one module
(`frontend/panel/drawcustom-request.js`), so what a Send ships is what the
designer's own Orientation and dither controls show at that moment — with
or without a preview ever having run. The Python side is pinned by
`tests/test_rotation_parity.py`'s
`test_send_without_preview_lands_right_side_up` (asserting on the buffer
handed to `upload_prepared_image`); the JS side by
`tests/js/drawcustom-request.test.mjs`.

Tier-2 (real hardware) root-cause note — **two bugs, not one, and the first
investigation round's "not a formula bug" conclusion was itself wrong** (a
reviewer re-verification caught this): a maintainer report that a rotated
display's SERVER preview rendered sideways, despite a correct CLIENT
canvas. His actual device (an ESL 5 3.5", verbatim reported attributes:
`pixel_width` 184, `pixel_height` 384, `rotation_degrees` **0**,
`render_width` 184, `render_height` 384, `color_scheme` 3/BWRY,
`palette_measured` false) has **base 0** — native portrait, no base
persisted, physically mounted landscape, his own working automation
compensating with `rotate: 270` on every call.

**Bug 1 (real): Send never carried a `rotate` value at all.** The
maintainer's real workaround (a manual helper script passing `rotate: 270`
on every `drawcustom` call) had no designer-side equivalent, so a payload
rotated in the designer's own Orientation control shipped un-rotated
through Send. The first fix round wired Send to reuse the last-previewed
`rotate` — the only thing the 2.x host contract allowed, and a sticky,
stale residual that was wrong whenever no preview had run or the control
had moved since. Designer 3.0.0 removed the constraint: `HostActionContext`
now carries the live geometry and render options, Send reads them at click
time, and the remembered values are gone from this panel entirely.

**Bug 2 (the actual "sideways preview" symptom, missed the first round,
found by re-verification): the render endpoint returned the DEVICE-FACING
grid, not the logical surface preview needs.** The first round's own fix
(`rotateDeltaFor`) correctly derives `rotate = target − base (mod 360)`
per `_drawcustom_for_device`'s own contract — that part was never wrong.
But the render endpoint then fed that `rotate` into `prepare_image`
UNCHANGED, alongside the real device's own capabilities — exactly what the
send path does, and exactly wrong for preview: `prepare_image`'s `rotate`
is device-facing (it composes with the device's stored base rotation and
always re-fits its output to the real device's raw, untransposed native
pixel grid, regardless of what `rotate` is). For base=0 with a 90°/270°
orientation, that meant **no `rotate` value could make the endpoint return
the transposed logical surface at all** — it always answered 184×384
(native portrait), never 384×184 (the designer's own canvas shape for that
orientation). The designer letterboxed that wrong-shaped answer into its
own 384×184 canvas: sideways content, despite `rotateDeltaFor` computing
the mathematically correct delta the whole time.

The first round's own regression suite did not catch this because it
asserted `endpoint bytes == send-path bytes` and both code paths shared the
identical (buggy, for preview) `prepare_image` call — trivially agreeing
with each other while both landing on the wrong shape. Byte-parity-with-send
was the wrong property to prove; see
`tests/test_rotation_parity.py`'s current module docstring for the full
corrected writeup and the three properties it asserts now (dimensions
against an independently-derived expectation, content orientation via an
asymmetric top-edge-bar payload, and pipeline parity against the send
path's own real `generate_image` output run through `prepare_image` a
second time with the LOGICAL surface as target). Fixed in `render.py`: an
explicit synthetic `DeviceCapabilities` (logical-surface dims, rotation=0)
plus `rotate=Rotation.ROTATE_0` for the preview call — device-facing
rotation belongs only on the send path (see "The render endpoint" above for
the implementation note). `rotateDeltaFor` is unchanged and still correct;
it was never the site of this bug.

A separate, unrelated finding from the same tier-2 screenshots: the
designer's own **Resolution field** showed "384×184" while the pushed
capabilities report 184×384. Not a bug — `ResolutionSelect`'s `canvasWidth`/
`canvasHeight` props (vendored `odl-drawcustom-designer.js`,
`src/ui/components/DisplayConfig` region) read the CURRENT, already-rotated
canvas (`canvas.width`/`canvas.height`), not the raw `pixel_width`/
`pixel_height` capabilities carry — with Orientation already set to 270 (a
quarter turn), the canvas is legitimately 384×184 at that moment. The field
is accurately describing the active drawing surface, just not labeled to
make clear it's post-rotation, which reads as a mismatch against a
capabilities panel showing native dims. Not something this integration's
own code can relabel (vendored, third-party UI) — worth a small upstream
ask (`odl-drawcustom-designer`) to clarify the label or add a tooltip;
tracked as PR-body follow-up, not fixed here.

### Displays mounted rotated

A display whose native pixel grid doesn't match its physical mounting
(the common case: a narrow portrait panel mounted landscape) needs an
explicit `rotate` on every render — HA does not yet have a way to
*persist* that as the display's own default (see below). In the designer:
pick the target display, set its Orientation control (0°/90°/180°/270°,
next to Display Config) to match how the panel is actually mounted, then
design and Send as normal — both Preview and Send honor that choice, read
live from the designer at the moment of the request or the click
(`rotateDeltaFor`, above). No preview has to run first: the earlier
"preview at least once before Send, or the payload ships un-rotated"
caveat described a 2.x host-contract limitation that designer 3.0.0
removed, and it no longer applies. A persistent, per-display default
orientation (so this wouldn't need re-picking every session) is a
deliberate deferral pending the upstream discussion on `rotation_degrees`
base-vs-effective (see the PR body's open questions) — not a limitation of
this endpoint or the panel wrapper.

## The asset endpoint

`GET /api/opendisplay/designer/asset?kind=font&name=<name>`
(`custom_components/opendisplay/designer/asset.py`) implements the LAST
tier of the designer's own asset resolution (`resolveAsset`/
`HostAssetResolver`, issue #138, ADR-002 amendment): asked only for a font
the designer could not resolve itself (its local content map, then its own
bundled assets). Maintainer ruling (tier-2, real hardware): "if the server
renderer can use it, the client must get it mapped" — before this endpoint,
a payload referencing a font by bare name (the same way a hand-written
`drawcustom` payload does) rendered correctly on send but showed the
designer's own explicit render-error state in preview, since the designer
had no way to reach this integration's font directories at all.

`kind=font` only, resolved against `_font_search_dirs` (`services.py`:
`www/fonts`, `media/fonts`, `/media/fonts`) with the exact same bare-name
`.ttf` auto-append `odl_renderer.fonts.FontManager` applies, so a name the
designer resolves through this endpoint is always the identical file a real
render/send would load for that same reference — never a font the server
renders with but the designer substitutes or errors on, and never a
different file behind the same name. Path-traversal-guarded the same way
as `OpenDisplayDesignerStaticView` (`panel.py`): resolve the candidate,
then require it stay under the search directory it came from. Authenticated
(`requires_auth = True`) — this is the integration's second authenticated
HTTP view, after the render endpoint. `kind=image` (or anything else) is a
400: this integration has no font-independent image search path today, so
font-only is the honest v1 rather than a resolver that silently answers
`null` for images forever.

Not cache-busted like the static view's own `?v=` token (font files in the
search directories can change without this integration knowing) — served
with `Cache-Control: no-cache, must-revalidate` instead, acceptable since
the designer resolves and caches an asset once per session rather than
re-requesting it on every render.

## Updating the vendored library

The designer ships vendored from npm rather than as a git dependency (Home
Assistant custom components can't `npm install` at runtime). See
[`custom_components/opendisplay/designer/frontend/vendor/README.md`](../custom_components/opendisplay/designer/frontend/vendor/README.md)
for the full procedure; in short:

```bash
scripts/update-designer-vendor.py              # re-verify the current pin
scripts/update-designer-vendor.py --pin 3.0.1  # bump to a new release
git diff custom_components/opendisplay/designer/frontend/vendor/
```

Both modes verify the npm-registry-declared `sha512` integrity against the
actual downloaded bytes before writing anything to `vendor/` — a mismatch
exits non-zero and changes nothing. After bumping the pin, review the diff
of `odl-drawcustom-designer.d.ts` against the panel wrapper's own usage
(`../panel/opendisplay-designer-panel.js`) — the wrapper is hand-written
against the 3.x host contract and is not regenerated by the script.

The current pins are the designer at **3.0.0** and `js-yaml` at **4.1.0**
(`vendor/designer.lock.json`). The js-yaml hold is deliberate: npm's latest
is 5.x, a major with API changes, which belongs in its own change with its
own acceptance run rather than riding along with a designer bump.

## Known gaps

- **`rotation_degrees` still publishes only the base panel rotation**
  (`capabilities.py`'s `user_rotate_deg` is always `0` — no host seam
  exists yet to carry a live rotate choice into `build_capabilities`).
  Both Preview and Send work around this at the point of use
  (`rotateDeltaFor`, above), comparing the designer's own live canvas
  orientation against that base value, so a rotated display renders and
  sends correctly despite the gap. What is still missing is *persistence*:
  the orientation has to be re-picked per session rather than being the
  display's own stored default.
- **The rest of the drawcustom option set is still hardcoded on Send**
  (`background: white`, `refresh_type: full`). `dither` and `rotate` are
  read live off `HostActionContext` (designer 3.0.0); the remaining options
  need `HostRenderOptions` to grow, which is the rest of
  `odl-drawcustom-designer` issue #105. Until then a user who wants a
  different background or a partial refresh uses the service directly.
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
- **`resolveAsset` is font-only** (fixed this round — see "The asset
  endpoint" above; previously not wired up at all, and a payload
  referencing a host-only font showed the designer's own client-side
  render-error state in the non-preview canvas view despite rendering
  correctly through Send/Display preview). Images remain unresolved: this
  integration has no font-independent image search path
  (`_image_search_dirs` equivalent) today, so `kind=image` requests are
  rejected with a 400 rather than silently answering `null` forever. A
  payload referencing a host-only IMAGE by name still shows the designer's
  own client-side render-error state in the non-preview canvas view, the
  same contradictory-looking-but-not-actually-buggy split described above
  for fonts before this fix — filed as follow-up, not fixed here.

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
- [Access and exposure](#access-and-exposure)
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

Response: `200` with `Content-Type: image/png` and the rendered bytes.

**For a `device_id` request**, this is the DEVICE-FACING buffer (2026-08-31
ruling, reversing the tier-2 round 2 fix described below): the same
post-rotation, post-dither image a real send would hand
`upload_prepared_image`, at the panel's own native pixel grid — **not** the
logical surface the payload was authored against, whenever the two differ.
Concretely: a landscape canvas on a display mounted 90°/270° comes back as
a **portrait PNG**. Nothing about that needs special handling on the
designer side — it is just an image, and the designer's Display preview
toggle already only ever points an `<img>` at whatever bytes this endpoint
returns (`HostPreviewRenderer`, vendored `.d.ts`); it never assumes the
response is the canvas's own shape. See "One shared preparation path with
Send" below for what makes this exact.

**For a `display`-spec request** (Virtual display, no `device_id`), the
response is still the **logical drawing surface's** resolution — the same
shape the designer's own canvas is at when it made the request
(`HostDisplayGeometry`, vendored `.d.ts`: "the logical drawing surface the
payload is authored against ... never the raw physical panel size"),
already transposed for a quarter-turn rotation. There is no HA device
behind a Virtual-display request, so there is no device-facing buffer to
speak of — see the same section below. Errors:

| Status | When |
|---|---|
| `400` | Malformed JSON, a schema violation (e.g. `payload` isn't a list), neither `device_id` nor `display` supplied (`{"message": "either device_id or display (width/height) is required"}`), a payload odl-renderer can't render, or a broken template in one element (`{"message": "drawcustom payload element <index> (type '<type>') has an invalid template: <reason>"}`) |
| `401` | No valid Home Assistant auth |
| `404` | `device_id` doesn't resolve to a loaded OpenDisplay config entry |

**Implementation shares two calls with the send path**: `generate_image`
(odl-renderer) followed by `prepare_image`'s dither + quantize step
(`opendisplay`), and nothing called after it — no upload, no queue, no
entity write, no dispatched signal.

### One shared preparation path with Send

**For a `device_id` request** (2026-08-31 ruling, reversing the tier-2
round 2 fix — see the root-cause note below for that history):
`prepare_image` is reached through `_prepare_for_device` (`services.py`),
the **exact same helper** the real send (`_async_send_image`) and a
`dry-run` call share — not a lookalike call built independently in
`render.py`. That means:

- **Real device capabilities**, not a synthetic override: `prepare_image`
  derives `capabilities` from the entry's own `config`, so `target_size` is
  the panel's raw native pixel grid.
- **The request's `rotate` passed straight through**, unmodified — it
  composes with the device's own stored base rotation exactly as the send
  path's does (`effective = (base + rotate) % 360`).
- `compress` is whatever `_prepare_for_device` derives for this device
  (whether it accepts compressed uploads) rather than always `False` — a
  preview never reads the compressed half of `prepare_image`'s return
  value, so this makes no observable difference to the response, but it
  means the render endpoint no longer special-cases that argument either.

Preview, dry run and a real send therefore all produce the **identical**
device-facing buffer for identical (device, payload, background, dither,
rotate) inputs — proven byte-for-byte by
`tests/test_rotation_parity.py`'s
`test_preview_matches_the_send_paths_prepared_buffer_byte_for_byte`. A
`renderPreview` call is a rehearsal of what Send would ship, not a separate
rendering that merely resembles it — the designer's own words for what a
dry run is *("dry run should be honest of course, otherwise it won't be a
dry run")* now apply to this preview endpoint too.

**For a `display`-spec request** (Virtual display), there is no `entry` to
build device capabilities from at all, so this path is unchanged from the
original construction: `prepare_image` is called with an explicit
`DeviceCapabilities` describing the LOGICAL surface itself (`width`/`height`
= the already-transposed `generate_image` canvas, `rotation=0`),
`rotate=Rotation.ROTATE_0`, and `compress=False`. There is no stored base
rotation to compose against for a display that isn't an HA device, and the
panel wrapper always sends `rotate: 0` for this case
(`renderRequestBody`, `drawcustom-request.js`) — device-facing vs.
logical-surface is not even a distinct question when there is no device.
`config` (the synthetic `GlobalConfig`) still supplies
`panel_ic_type`/palette derivation there.

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
`test_send_without_preview_carries_the_rotate_into_the_buffer`
(asserting on the buffer
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
was the wrong property to prove. Round 2's fix (superseded by the ruling
below, kept here for the history): an explicit synthetic
`DeviceCapabilities` (logical-surface dims, rotation=0) plus
`rotate=Rotation.ROTATE_0` for the preview call, so preview targeted the
logical surface and never touched device-facing rotation at all.
`rotateDeltaFor` was unchanged and still correct throughout; it was never
the site of this bug.

**Ruling reversed (2026-08-31, real hardware, v2.9):** the maintainer
flashed the round-2 fix and reported the predictable consequence of
"preview never touches device-facing rotation" — orientation 90 and
orientation 270 previews were identical: *"didn't we want [orientation] to
be also correct so that 90 would look upside down in relation to 270?"*
They should, and by design now do: a base=0 90°/270° preview differs by
exactly a half turn, matching what the entity preview and a dry run already
showed for the identical call (tier-2 round 3, above) — this endpoint's
Display preview was the one remaining view where a wrong orientation could
not be caught before Send. See "One shared preparation path with Send"
above for the mechanism (`_prepare_for_device`, shared with Send and dry
run), and `tests/test_rotation_parity.py`'s current module docstring for
why this does not reintroduce Bug 2: the source image `generate_image`
builds is still shaped at the transposed logical surface (`gen_width`/
`gen_height`, unchanged since round 1), so `prepare_image`'s device-facing
rotate-and-fit always lands exactly on the native grid with no distortion —
`test_endpoint_matches_send_paths_prepared_buffer_pixel_for_pixel` and
`test_preview_matches_the_send_paths_prepared_buffer_byte_for_byte` prove
it across the full (base, rotate) matrix, not just by that argument.

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

**What Orientation actually means.** Orientation describes **how the panel
is mounted on your wall** — nothing else. The designer canvas always shows
your content upright, because that is what you are designing; it does not
tilt to preview the mounting. That has one consequence worth knowing before
you go looking for a bug:

> **0° and 180° look identical on the canvas, and so do 90° and 270°.**
> Each pair produces the same drawing surface and differs only in which way
> that surface is turned onto the panel. For any one physical mounting,
> exactly one member of each pair comes out upright on the wall and the
> other comes out upside down. That is correct behaviour, not a sign error:
> a panel can only be hung one way up.

So if the wall image is upside down, **pick the other member of the pair**
(90 ↔ 270, or 0 ↔ 180) and send again. Nothing else needs changing, and
nothing about the canvas will look different when you do.

Since the Home Assistant `image` entity now shows the buffer that was
actually uploaded (next paragraph), you can also tell the two apart without
walking to the display: the entity's picture turns when the Orientation
changes, even though the canvas does not. The designer's own **Display
preview** (a real device target, "The render endpoint" above) shows the
same thing **before you send anything** — it now returns the device-facing
buffer too, so a wrong Orientation choice is visible in the preview itself,
not only after a send has already gone out.

**The entity preview shows what the panel was given.** After a send, the
`image.<device>_content` entity holds the post-rotation, post-dither buffer
handed to the panel — not the pre-rotation canvas it was drawn on. It is
therefore in the panel's own native pixel grid (a 184×384 portrait panel
mounted landscape shows a portrait picture with the content lying on its
side), which is the point: that picture changes when the orientation
choice changes, and the canvas does not. Before this, a wrong orientation
was invisible everywhere in Home Assistant and only discoverable at the
display. This applies to every `opendisplay.drawcustom` and
`opendisplay.upload_image` call, not only to designer sends — **including
`dry-run: true`**, which prepares the frame exactly as a real send would
and previews that, without uploading anything. So a dry run is a safe way
to check an orientation choice before it reaches the panel.

## The asset endpoint

`GET /api/opendisplay/designer/asset?kind=font|image&name=<name>`
(`custom_components/opendisplay/designer/asset.py`) implements the LAST
tier of the designer's own asset resolution (`resolveAsset`/
`HostAssetResolver`, issue #138, ADR-002 amendment): asked only for a
reference the designer could not resolve itself (its local content map,
then its own bundled assets). Maintainer ruling (tier-2, real hardware):
"if the server renderer can use it, the client must get it mapped" —
before this endpoint, a payload referencing a font by bare name (the way a
hand-written `drawcustom` payload does) rendered correctly on send but
showed the designer's own explicit render-error state in the canvas, since
the designer had no way to reach this integration's font directories at
all. `kind=image` closes the same gap for images (real hardware again: a
display's payload referenced `/media/pohl89-480h.png`, the server render
resolved it, the designer showed it missing).

**Fonts** (`kind=font`) resolve against `_font_search_dirs`
(`services.py`: `www/fonts`, `media/fonts`, `/media/fonts`) with the exact
same bare-name `.ttf` auto-append `odl_renderer.fonts.FontManager` applies,
so a name the designer resolves through this endpoint is always the
identical file a real render/send would load for that same reference —
never a font the server renders with but the designer substitutes or errors
on, and never a different file behind the same name. Path-traversal-guarded
the same way as `OpenDisplayDesignerStaticView` (`panel.py`): resolve the
candidate, then require it stay under the search directory it came from.

**Images** (`kind=image`) work differently, because the renderer treats
them differently: `odl_renderer.media_loader.load_image` takes an
**absolute path** and opens it directly — there is no bare-name image
search path the way there is for fonts. So the reference reaching this
endpoint is whatever path the payload carries, and the endpoint is
deliberately **stricter than the renderer**:

| | Rule |
|---|---|
| Permitted roots | `hass.config.allowlist_external_dirs` — Home Assistant's own set, which core composes as `{<config>/www} ∪ media_dirs.values() ∪ your own allowlist`. On Home Assistant OS that is `/config/www` and `/media`. |
| Containment | Re-checked **after** `resolve()`, so `..` is collapsed and symlinks followed first — a symlink inside a permitted root that points outside it is refused. |
| Remote sources | `http(s)://` is refused outright. The render path does fetch remote sources server-side; that is a property of the service and is not widened into a browser-facing fetch. |
| File types | Only what PIL can identify as an image, served with PIL's own content type for that format — so the endpoint cannot be used to read the non-image files that also live in a media directory. |
| Size | Capped at 32 MiB per request. |

Every refusal that is about a path — outside the roots, escaping symlink,
not an image, too large, or simply absent — answers the same `404`, so the
endpoint is not an existence oracle for the rest of the filesystem.

The consequence, stated plainly: the renderer accepts absolute paths this
endpoint refuses, so an image kept outside the permitted directories
renders on Send but shows the designer's explicit missing-asset state in
the canvas. That is the safe direction of the mismatch, and the fix is to
move the image under `/media` or `/config/www` (or allowlist its
directory), not to widen the endpoint.

Authenticated (`requires_auth = True`) — this is the integration's second
authenticated HTTP view, after the render endpoint. Any other `kind` is a
400 rather than a silent `null` forever (which is indistinguishable from
"not found" to the designer).

Not cache-busted like the static view's own `?v=` token (font files in the
search directories can change without this integration knowing) — served
with `Cache-Control: no-cache, must-revalidate` instead, acceptable since
the designer resolves and caches an asset once per session rather than
re-requesting it on every render.

## Access and exposure

**The designer is available to every authenticated Home Assistant user.**
That is deliberate. The sidebar panel registers with `require_admin=False`,
and the two data endpoints (`/api/opendisplay/designer/render`,
`/api/opendisplay/designer/asset`) require authentication and nothing more.

**Why, and why the three match.** The render endpoint fronts
`opendisplay.drawcustom`, which any authenticated user can already call, and
it renders the same payload templates through the same shared helper
(`render_payload_templates`, `services.py`). So the endpoint grants no
capability a caller does not already have. Panel visibility, endpoint
authorization and the exposure of the service being fronted are kept
consistent with each other — a panel offered to everyone whose endpoints
reject most of them, or gated endpoints behind an ungated service, would be
the inconsistency worth avoiding.

**What a user of the designer can see.** Payload field values are expanded
with the full Home Assistant template context, so a user composing a payload
can read any entity's state and attributes and perform registry lookups —
`device_attr`, `area_name`, `integration_entities` and friends. The last of
those the Home Assistant frontend normally surfaces only on admin config
pages, so it is worth stating plainly. This is **information disclosure
only**: Home Assistant templates are read-only. They cannot call services and
they cannot execute code. There is no privilege escalation here. Note also
that Home Assistant has no practical per-entity ACL to begin with — any
authenticated user already reads every entity's state through the frontend
and the WebSocket API.

**If you want this restricted, restrict it at the Home Assistant user
level.** The integration deliberately does not invent its own permission
model on top of Home Assistant's.

**The static view is unauthenticated by necessity.** It serves the panel JS
and the vendored designer bundle, which the browser pulls in as ES modules —
a `<script type="module">` or a bare `import` sends no `Authorization` header
and no Home Assistant auth cookie, so requiring auth there would make the
panel unloadable for everyone. It serves only this integration's own bundled
frontend files from a traversal-guarded path under `designer/frontend/`, and
exposes no Home Assistant data, no configuration and no entity state.

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

The current pins are the designer at **3.3.0** and `js-yaml` at **4.1.0**
(`vendor/designer.lock.json`). The js-yaml hold is deliberate: npm's latest
is 5.x, a major with API changes, which belongs in its own change with its
own acceptance run rather than riding along with a designer bump.

While this integration's designer work is still in development the designer
pin is moved forward at every opportunity, so the panel wrapper is always
tested against the newest published contract rather than against a snapshot
that quietly ages. 3.0.0 → 3.2.0 → 3.2.1 → 3.3.0 changed the bundle only:
the published `odl-drawcustom-designer.d.ts` is byte-identical across all
four releases, and every type and member the wrapper reads or writes has
been re-checked against it at each bump
(`mount`, all five `MountHandle` methods used, all ten `MountOptions` keys
passed, `HostTarget`, all ten `HostDisplaySpec` keys written,
`HostActionContext`/`HostPreviewContext` and their `display`/`render`
members, `HostAction` including `severity: 'caution'`, `DesignerStatus`'s
`yamlValid`/`yamlErrorSummary`, and `EmbedTheme`). No wrapper change was
needed.

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
- ~~**The `dry-run` field on `opendisplay.drawcustom` ignores `dither`**~~ —
  fixed. A dry run now runs the same `_prepare_for_device` call a real send
  does (same rotate, dither, tone and measured-palette values, same device
  config) and previews that buffer, so it honours `dither` and shows the
  orientation the panel would actually get. It still uploads nothing,
  queues nothing, and returns only a `dry_run` receipt. One consequence
  worth knowing: if preparation fails for a device, the dry run now
  reports that failure instead of succeeding — which is the answer a dry
  run should give, since the real send would fail the same way.
- **`generate_image` still runs on the event loop**, inside the render
  endpoint exactly like it already does in `_drawcustom_for_device`'s own
  send path. Its *blocking file I/O* no longer does: a `dlimg` element
  pointing at a local file used to be opened by PIL inside the loop (Home
  Assistant's own detector reported it on real hardware), and both call
  sites now decode those sources in an executor first
  (`preload_local_image_sources`, `services.py`). What remains on the loop
  is the CPU-bound rasterising, which the detector does not flag and the
  element cap below bounds. Moving the whole coroutine off the loop would
  mean handing a loop-bound
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
- **`resolveAsset` resolves images only inside Home Assistant's permitted
  directories.** Both `AssetKind` values are wired up now (see "The asset
  endpoint" above), but the renderer will load an image from *any* absolute
  path while the endpoint serves only what is under
  `hass.config.allowlist_external_dirs`. So an image kept outside those
  directories still renders on Send and still shows the designer's own
  render-error state in the canvas. That asymmetry is deliberate — the
  endpoint hands file bytes to a browser, the renderer does not — and the
  fix for an affected payload is to move the image under `/media` or
  `/config/www` (or to allowlist its directory), not to widen the
  endpoint.

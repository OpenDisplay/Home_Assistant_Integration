/**
 * HA panel host for vendored odl-drawcustom-designer 3.x
 * (mount + drawcustom send via the designer's targets/actions/preview seams).
 *
 * The designer owns ALL chrome now (ADR-018 upstream): no host toolbar, no
 * device picker built here, no Copy YAML button, no Save button — those are
 * the designer's `targets` picker, built-in Copy YAML, and the `send` host
 * action registered below. This file only supplies data in and reacts to
 * callbacks out; see ../vendor/README.md for the vendoring procedure and
 * https://github.com/schlomo/odl-drawcustom-designer/blob/main/docs/embedding.md
 * for the full host contract this panel implements.
 *
 * Preview isolation (maintainer ruling 2026-08-30): `renderPreview` POSTs to
 * this integration's own /api/opendisplay/designer/render endpoint, which
 * renders through the exact same generate_image + prepare_image pipeline a
 * real send uses but never touches the image entity, never dispatches
 * SIGNAL_IMAGE_UPDATED, and never delivers to the device — designer play can
 * never show up on a live display's own dashboard. This replaces an earlier
 * draft's dry-run/poll/cache-bust approach entirely; there is no dry-run
 * involved here at all.
 */
import { mount } from '../vendor/odl-drawcustom-designer.js';
import yaml from '../vendor/js-yaml.mjs';
import { containKeyEvents } from './key-containment.js';
import { installUnsavedWorkWarning } from './unsaved-work.js';
import { renderRequestBody, sendCallData } from './drawcustom-request.js';
import { assetRequestUrl } from './asset-request.js';

const TAG = 'opendisplay-designer-panel';
const RENDER_URL = '/api/opendisplay/designer/render';

const CSS = `
:host{display:block!important;position:absolute;inset:0;width:100%;height:100%;max-width:none;overflow:hidden;box-sizing:border-box;font-family:var(--ha-font-family-body,system-ui,sans-serif);color:var(--primary-text-color,#1c1917);background:var(--primary-background-color,#fafaf9)}
.od-host{display:flex;flex-direction:column;width:100%;height:100%;min-height:0;min-width:0;overflow:hidden;box-sizing:border-box}
.od-mount{flex:1 1 0;min-height:0;min-width:0;width:100%;overflow:hidden;position:relative}
.od-mount>*{width:100%!important;height:100%!important;max-width:none!important;box-sizing:border-box}
.od-error{padding:16px;color:var(--error-color,#db4437);background:var(--error-state-color,rgba(219,68,55,0.1));border-bottom:1px solid var(--error-color,#db4437)}
.od-error[hidden]{display:none}
`;

function errMsg(err) {
  if (!err || typeof err !== 'object') return String(err);
  const message = Reflect.get(err, 'message');
  const body = Reflect.get(err, 'body');
  const bodyStr =
    body && typeof body === 'object' && Reflect.get(body, 'message')
      ? String(Reflect.get(body, 'message'))
      : '';
  return [typeof message === 'string' ? message : '', bodyStr].filter(Boolean).join(' — ') || 'Error';
}

/** Every HA device backed by the opendisplay platform (host devices, not the designer's own "Virtual display" — that stays the designer's built-in picker entry). */
function listOpenDisplayDevices(hass) {
  const devices = hass?.devices;
  if (!devices || typeof devices !== 'object') return [];
  const out = [];
  for (const [id, d] of Object.entries(devices)) {
    if (!d || typeof d !== 'object') continue;
    const hit =
      !!hass?.entities &&
      Object.values(hass.entities).some(
        (e) => e && e.device_id === id && e.platform === 'opendisplay'
      );
    if (!hit) continue;
    const name = String(d.name_by_user || d.name || d.original_name || id).trim();
    out.push({ id, name });
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

function imageEntity(hass, deviceId) {
  const reg = hass?.entities;
  if (!reg) return null;
  const imgs = [];
  for (const [key, ent] of Object.entries(reg)) {
    if (!ent || ent.device_id !== deviceId) continue;
    const eid = String(ent.entity_id || key);
    if (eid.startsWith('image.')) imgs.push(eid);
  }
  return (
    imgs.find((eid) => Number(hass.states?.[eid]?.attributes?.pixel_width) > 0) ||
    imgs[0] ||
    null
  );
}

/**
 * Translate one image entity's HA attributes into the designer's
 * `HostDisplaySpec` (vendored `.d.ts`).
 *
 * THIS IS THE TRANSLATION LAYER, and the only one. HA entity attributes are
 * snake_case because that is HA's own convention, and `capabilities.py`
 * keeps emitting them that way; `HostDisplaySpec`'s keys are camelCase
 * because that is the designer's published contract (3.0.0 made it the last
 * published interface to stop being an exception). The two vocabularies meet
 * here and nowhere else — do not "align" either side to the other.
 */
function displaySpecFromAttrs(attrs) {
  const pw = Number(attrs.pixel_width) || 296;
  const ph = Number(attrs.pixel_height) || 128;
  let colorScheme = attrs.color_scheme;
  if (typeof colorScheme !== 'number' || Number.isNaN(colorScheme)) colorScheme = 0x01;
  return {
    pixelWidth: pw,
    pixelHeight: ph,
    // KNOWN GAP (PR body's open questions): capabilities.py publishes the
    // BASE rotation while render_width/render_height are already swapped
    // for the EFFECTIVE (base + user_rotate) orientation. The contract
    // requires rotationDegrees to describe the orientation render* is
    // already in — pass the attribute through as-is; do not silently "fix"
    // it here.
    rotationDegrees: Number(attrs.rotation_degrees) || 0,
    renderWidth: Number(attrs.render_width) || pw,
    renderHeight: Number(attrs.render_height) || ph,
    colorScheme,
    accentColor: String(attrs.accent_color || 'red'),
    availableColors: Array.isArray(attrs.available_colors)
      ? attrs.available_colors.map(String)
      : ['black', 'white', 'red'],
    colorMap:
      attrs.color_map && typeof attrs.color_map === 'object'
        ? attrs.color_map
        : { black: '#000000', white: '#ffffff', red: '#c53929' },
    paletteMeasured: Boolean(attrs.palette_measured),
  };
}

/** Every real OpenDisplay device with published display attributes, as designer `targets`. */
function buildTargets(hass) {
  const targets = [];
  for (const d of listOpenDisplayDevices(hass)) {
    const eid = imageEntity(hass, d.id);
    const attrs = eid ? hass?.states?.[eid]?.attributes : null;
    if (!attrs || typeof attrs !== 'object') continue; // capability attrs not published yet
    // Gate on a REAL capability key, not just "an attributes dict exists" —
    // real HA always gives an image entity an attributes dict (possibly
    // `{}`), and image_entity.py itself returns `{}` on a capability-build
    // exception. Without this check, a device whose capabilities failed to
    // build (or haven't been written yet) becomes a fabricated 296x128 BWR
    // target and — because it may be the only device — auto-adopts and
    // locks the canvas to a size/palette that isn't real. `pixel_width` is
    // the cheapest reliable "capabilities actually published" signal
    // (capabilities.py always sets it > 0); the 250ms re-push adds the
    // device once real attributes land.
    if (!(Number(attrs.pixel_width) > 0)) continue;
    targets.push({ id: d.id, label: d.name, display: displaySpecFromAttrs(attrs) });
  }
  return targets;
}

/** Host state catalog (docs/embedding.md `states`) — friendly names from `attributes.friendly_name`. */
function collectStates(hass) {
  const out = {};
  const states = hass?.states;
  if (!states) return out;
  for (const [eid, st] of Object.entries(states)) {
    if (!st || typeof st !== 'object') continue;
    const attributes =
      st.attributes && typeof st.attributes === 'object' ? { ...st.attributes } : undefined;
    const name =
      typeof attributes?.friendly_name === 'string' ? attributes.friendly_name.trim() : '';
    out[eid] = {
      state: String(st.state ?? ''),
      ...(attributes ? { attributes } : {}),
      ...(name ? { name } : {}),
    };
  }
  return out;
}

function parsePayload(text) {
  // YAML 1.2 CORE_SCHEMA keeps key `y` (1.1 would booleanize it).
  const doc = yaml.load(String(text || '').trim() || '[]', { schema: yaml.CORE_SCHEMA });
  if (!Array.isArray(doc)) throw new Error('Payload must be a YAML list');
  return doc;
}

function theme(hass) {
  // hass.themes.darkMode is HA's OWN already-resolved effective choice
  // (explicit user pick, or its own system-preference fallback when the
  // user picked "auto") -- trust it whenever it's a real boolean. Falling
  // through to matchMedia() unconditionally on `false` (the previous `||`
  // form) would override an explicit LIGHT theme pick with dark whenever
  // the OS itself prefers dark, which is backwards: matchMedia is only a
  // fallback for the (should not happen) case HA hasn't resolved it at all.
  const dm = hass?.themes?.darkMode;
  if (typeof dm === 'boolean') return dm ? 'dark' : 'light';
  const dark = typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches;
  return dark ? 'dark' : 'light';
}

class OpenDisplayDesignerPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._resetMountState();
  }

  /**
   * Everything that describes ONE mount's state, as opposed to the custom
   * element's own lifetime (which can outlive several mounts — HA reuses
   * the element across a navigate-away-and-back, and disconnectedCallback
   * destroys the designer handle without the browser ever discarding this
   * object). Called from the constructor and again from
   * disconnectedCallback, so a fresh mount always starts clean instead of
   * carrying over the previous mount's selection/sending/YAML-validity
   * state (a stale `_selectedTargetId` surviving a remount could show Send
   * enabled before the fresh designer instance has a selection at all).
   */
  _resetMountState() {
    this._handle = null;
    this._pushTimer = null;
    this._pushDebounceStartedAt = 0;
    this._selectedTargetId = null;
    this._yamlValid = true;
    this._yamlErrorSummary = undefined;
    // In-flight guard (send): prevents a multi-click Send from firing
    // several drawcustom calls at physical hardware.
    this._sending = false;
    // Stale-selection tracking: the most recent non-null target id the
    // designer reported, and the target ids we most recently pushed —
    // together these tell `onTargetSelected(null)` apart from a genuine
    // "no display chosen yet" (never had a selection, or the user picked
    // Virtual display while their device is still available) versus a
    // previously-selected display that dropped out of our own targets list
    // (the designer's "keep and mark stale" case, docs/embedding.md).
    this._lastSelectedTargetId = null;
    this._lastTargetIds = new Set();
    // Every currently-pushed target's own HostDisplaySpec, by id -- so both
    // _renderPreview and _send can recover the selected target's BASE
    // rotation (display.rotationDegrees) to compare against the designer's
    // live, possibly user-rotated canvas orientation (context.display).
    this._targetDisplaySpecs = new Map();
    this._staleSelection = false;
  }

  set hass(value) {
    this._hass = value;
    if (!this.isConnected) return;
    // Debounce with a max-wait (like the designer's own status-change
    // debounce, docs/embedding.md HostStatusChangeHandler): a busy HA
    // instance can push hass updates faster than every 250ms indefinitely,
    // which would otherwise reset this timer forever and starve
    // _pushHostData() completely. Cap the total wait since the FIRST
    // pending update in a burst at 1s, same cadence the designer itself
    // uses for status delivery.
    const now = Date.now();
    if (this._pushTimer == null) this._pushDebounceStartedAt = now;
    clearTimeout(this._pushTimer);
    const elapsedSinceFirstPending = now - this._pushDebounceStartedAt;
    const delay = Math.min(250, Math.max(0, 1000 - elapsedSinceFirstPending));
    this._pushTimer = setTimeout(() => {
      this._pushTimer = null;
      this._pushHostData();
    }, delay);
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: 'open' });
    Object.assign(this.style, {
      display: 'block',
      position: 'absolute',
      inset: '0',
      width: '100%',
      height: '100%',
      maxWidth: 'none',
      overflow: 'hidden',
      boxSizing: 'border-box',
    });
    if (this.parentElement) {
      const p = this.parentElement;
      this._parentStylePatch = {
        position: p.style.position && p.style.position !== 'static' ? null : p.style.position,
        height: p.style.height || null,
      };
      if (this._parentStylePatch.position !== null) p.style.position = 'relative';
      if (this._parentStylePatch.height === null) p.style.height = '100%';
    }
    this._renderShell();
    this._mount();
    // See containKeyEvents' own doc comment for the full root-cause writeup
    // (tier-1 round 2, CRITICAL) -- registered on `this`, the shadow root's
    // host, so it runs after the shadow root's own listeners (CodeMirror's
    // included) and before the event would otherwise keep bubbling out to
    // HA's window-level quick-bar shortcuts.
    this._uncontainKeyEvents = containKeyEvents(this);
    // See unsaved-work.js's own doc comment for the full writeup, including
    // the honest limit (tier-1 round 2, finding 6, INTERIM until
    // designer#167 -- REAL PAGE UNLOAD ONLY, not HA's own in-app sidebar
    // navigation, verify with an actual reload/close, not a sidebar
    // click). `() => this._handle` (not `this._handle` itself) because the
    // handle can be reassigned across a remount after this listener is
    // registered.
    this._uninstallUnsavedWorkWarning = installUnsavedWorkWarning(window, () => this._handle);
  }

  disconnectedCallback() {
    clearTimeout(this._pushTimer);
    if (this.parentElement && this._parentStylePatch) {
      const p = this.parentElement;
      if (this._parentStylePatch.position !== null) p.style.position = this._parentStylePatch.position;
      if (this._parentStylePatch.height === null) p.style.height = '';
    }
    this._parentStylePatch = null;
    this._handle?.destroy();
    this._uncontainKeyEvents?.();
    this._uncontainKeyEvents = null;
    this._uninstallUnsavedWorkWarning?.();
    this._uninstallUnsavedWorkWarning = null;
    this._resetMountState();
  }

  _$(id) {
    return this.shadowRoot?.getElementById(id);
  }

  _renderShell() {
    const root = this.shadowRoot;
    if (!root || root.querySelector('.od-host')) return;
    root.innerHTML = `
      <style>${CSS}</style>
      <div class="od-host">
        <div class="od-error" id="od-error" hidden></div>
        <div class="od-mount" id="od-mount"></div>
      </div>`;
  }

  /** A mount failure leaves #od-mount empty forever otherwise -- the toast
   * notification fades, and a blank panel with no chrome at all gives the
   * user nothing to act on. */
  _showMountError(message) {
    const el = this._$('od-error');
    if (!el) return;
    el.textContent = `Failed to load the OpenDisplay Designer: ${message}`;
    el.hidden = false;
  }

  _notify(message) {
    this.dispatchEvent(
      new CustomEvent('hass-notification', { detail: { message }, bubbles: true, composed: true })
    );
  }

  _actionsList() {
    const disabledReason = this._sending
      ? 'Sending…'
      : !this._yamlValid
        ? this._yamlErrorSummary
          ? `Fix the YAML errors to send: ${this._yamlErrorSummary}`
          : 'Fix the YAML errors to send'
        : !this._selectedTargetId
          ? this._staleSelection
            ? 'Display no longer available' // wording aligned with the designer's own stale-target hint (docs/embedding.md "keep and mark stale")
            : 'No display selected'
          : undefined;
    return [
      { id: 'send', label: 'Send to display', icon: 'send', severity: 'caution', disabledReason },
    ];
  }

  _updateTargetsTracking(targets) {
    this._lastTargetIds = new Set(targets.map((t) => t.id));
    this._targetDisplaySpecs = new Map(targets.map((t) => [t.id, t.display]));
  }

  _pushActions() {
    try {
      this._handle?.setActions(this._actionsList());
    } catch (err) {
      console.error('opendisplay-designer-panel: setActions failed', err);
    }
  }

  _mount() {
    if (this._handle) return;
    const mountEl = this._$('od-mount');
    if (!mountEl) return;
    try {
      const initialTargets = buildTargets(this._hass);
      this._updateTargetsTracking(initialTargets);
      this._handle = mount(mountEl, {
        payload: '[]\n',
        states: collectStates(this._hass),
        theme: theme(this._hass),
        targets: initialTargets,
        onTargetSelected: (targetId) => {
          this._selectedTargetId = targetId;
          if (targetId) {
            this._lastSelectedTargetId = targetId;
            this._staleSelection = false;
          } else {
            // Stale iff we previously had a selection and it has since
            // dropped out of the targets we last pushed — not just "there
            // is no selection right now" (also true right after mount, or
            // after a deliberate Virtual-display pick).
            this._staleSelection = Boolean(
              this._lastSelectedTargetId && !this._lastTargetIds.has(this._lastSelectedTargetId)
            );
          }
          this._pushActions();
        },
        actions: this._actionsList(),
        onAction: (id, payload, context) => {
          if (id === 'send') void this._send(payload, context);
        },
        renderPreview: (payload, context) => this._renderPreview(payload, context),
        resolveAsset: (kind, name) => this._resolveAsset(kind, name),
        // Designer-local uploads land in this ONE browser's IndexedDB and
        // never reach Home Assistant -- an uploaded asset renders on the
        // canvas here and then fails the moment the design is sent, because
        // send/render load assets from this integration's own directories
        // (`designer/asset.py`), not from this browser's storage. `true`
        // alone would remove the upload affordances silently; the `hint`
        // instead points at the directories `resolveAsset` above can
        // actually serve from, so the Content tab's read-only explorer says
        // where a file needs to live instead of just "you can't upload
        // here" (docs/embedding.md `hostOwnsAssets`).
        hostOwnsAssets: {
          hint: 'Add images anywhere under /config/www or /media, and fonts in a fonts subfolder there (e.g. /media/fonts).',
        },
        onStatusChange: (status) => {
          this._yamlValid = status.yamlValid;
          this._yamlErrorSummary = status.yamlErrorSummary;
          this._pushActions();
        },
      });
    } catch (err) {
      console.error('opendisplay-designer-panel: mount failed', err);
      this._notify(`Failed to mount designer: ${errMsg(err)}`);
      this._showMountError(errMsg(err));
    }
  }

  _pushHostData() {
    if (!this._handle) return;
    try {
      const targets = buildTargets(this._hass);
      // Update tracking alongside the computed push, not after: the
      // designer's onTargetSelected(null) for a stale removal fires
      // asynchronously (not synchronously from inside setTargets() below),
      // so the ordering relative to setTargets() doesn't itself matter for
      // correctness today. Keeping this update right next to the push it
      // describes is simply the one place it can never drift from what was
      // actually last sent, and stays correct even if a future designer
      // version ever fires the callback synchronously.
      this._updateTargetsTracking(targets);
      this._handle.setTheme(theme(this._hass));
      this._handle.setStates(collectStates(this._hass));
      this._handle.setTargets(targets);
    } catch (err) {
      console.error('opendisplay-designer-panel: host push failed', err);
      this._notify(`Update from Home Assistant failed: ${errMsg(err)}`);
    }
  }

  /**
   * `send` host action (docs/embedding.md `actions`/`onAction`) — the only
   * save/send channel; the designer has no Save button of its own.
   * `background`/`refresh_type` are still hardcoded (designer issue #105
   * will expose the rest of the option set later); `dither` and `rotate`
   * are read LIVE off `HostActionContext` at the instant of the click
   * (`context.render.dither`, `context.display.rotation` — both frozen,
   * both present on every action since designer 3.0.0), so Send ships what
   * the designer's own controls show right now. No preview has to have run,
   * and nothing is remembered from one: the panel no longer keeps a
   * last-preview dither or rotate at all.
   */
  async _send(payloadYaml, context) {
    const hass = this._hass;
    const targetId = context.targetId;
    if (!hass?.callService) {
      this._notify('Home Assistant connection unavailable');
      return;
    }
    if (!targetId) {
      this._notify('Select a display to send to');
      return;
    }
    if (this._sending) return; // in-flight guard — one Send at a time
    let elements;
    try {
      elements = parsePayload(payloadYaml);
    } catch (err) {
      this._notify(`Cannot send invalid YAML: ${errMsg(err)}`);
      return;
    }
    if (!elements.length) {
      this._notify('Nothing to send — add elements first');
      return;
    }
    this._sending = true;
    this._pushActions();
    try {
      await hass.callService(
        'opendisplay',
        'drawcustom',
        sendCallData(elements, this._targetDisplaySpecs.get(targetId), context),
        // HA's separate service-call "target" (attributes the call to a
        // device in the logbook/trace UI), independent of the `device_id`
        // the service schema itself requires inside the data above.
        { device_id: targetId }
      );
      this._notify(`Sent ${elements.length} element(s) at ${new Date().toLocaleTimeString()}`);
    } catch (err) {
      this._notify(`Send failed: ${errMsg(err)}`);
    } finally {
      this._sending = false;
      this._pushActions();
    }
  }

  /**
   * `renderPreview` host seam (docs/embedding.md `renderPreview`) — POSTs to
   * this integration's own render endpoint (maintainer ruling 2026-08-30:
   * preview must never touch a live display's own state). The endpoint
   * renders through the same pipeline a real send uses and returns PNG
   * bytes directly; no image-entity write, no signal dispatch, no BLE
   * delivery happens on the backend either. The designer itself discards a
   * superseded response (docs/embedding.md `renderPreview`: "a slow answer
   * that a newer request has already superseded is discarded"), so this
   * function does not need its own request-ordering logic.
   */
  async _renderPreview(payloadYaml, context) {
    const hass = this._hass;
    if (!hass?.fetchWithAuth) throw new Error('Home Assistant connection unavailable');

    let elements;
    try {
      elements = parsePayload(payloadYaml);
    } catch (err) {
      throw new Error(`Cannot preview invalid YAML: ${errMsg(err)}`);
    }

    // Same builder module the `send` action uses, so preview and send derive
    // `dither`/`rotate` from the designer's live context identically instead
    // of two lookalike expressions that can drift.
    const requestBody = renderRequestBody(
      elements,
      this._targetDisplaySpecs.get(context.targetId),
      context
    );
    const res = await hass.fetchWithAuth(RENDER_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody),
    });
    if (!res.ok) {
      let message = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body?.message) message = body.message;
      } catch {
        // response body wasn't JSON — keep the plain HTTP status message
      }
      throw new Error(`Render failed: ${message}`);
    }
    return await res.blob();
  }

  /**
   * `resolveAsset` host seam (`HostAssetResolver`, issue #138, ADR-002
   * amendment) -- the LAST tier of asset resolution, asked only for a
   * reference the designer could not resolve itself (local content map,
   * then bundled assets). BOTH `AssetKind` values are asked for: fonts by
   * bare name against this integration's font directories, images by
   * absolute path within Home Assistant's own permitted roots (see
   * `designer/asset.py`). The earlier `kind !== 'font'` short-circuit is
   * gone -- it made a payload's `/media/...` image render on the server
   * while showing as missing in the designer (tier-2 round 3, real
   * hardware).
   *
   * Per the contract's own wording, `null`/a rejection/a timeout all settle
   * identically as "not supplied" and reach the user as the designer's own
   * explicit render-error state -- never a silent skip, never a substituted
   * font -- so every failure path here resolves `null` rather than
   * throwing: a thrown error is not a documented outcome of this seam.
   */
  async _resolveAsset(kind, name) {
    const hass = this._hass;
    const url = assetRequestUrl(kind, name);
    if (url === null || !hass?.fetchWithAuth) return null;
    try {
      const res = await hass.fetchWithAuth(url);
      if (!res.ok) return null;
      return await res.blob();
    } catch {
      return null;
    }
  }
}

if (!customElements.get(TAG)) {
  customElements.define(TAG, OpenDisplayDesignerPanel);
}

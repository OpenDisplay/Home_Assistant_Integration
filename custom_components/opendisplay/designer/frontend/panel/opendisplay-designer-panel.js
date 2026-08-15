/**
 * Home Assistant panel host for the vendored odl-drawcustom-designer embed.
 * Mounts the library, pushes device capabilities + entity states, and sends
 * drawcustom payloads via hass.callService.
 */
import { mount } from '../vendor/odl-drawcustom-designer.js';
import yaml from '../vendor/js-yaml.mjs';

const TAG = 'opendisplay-designer-panel';
const VIRTUAL_DEVICE_ID = '__virtual__';
const DEFAULT_CAPS = {
  pixel_width: 296,
  pixel_height: 128,
  rotation_degrees: 0,
  render_width: 296,
  render_height: 128,
  color_scheme: 0x01,
  accent_color: 'red',
  available_colors: ['black', 'white', 'red'],
  color_map: {
    black: '#000000',
    white: '#ffffff',
    red: '#c53929',
  },
  palette_measured: false,
};

const HOST_CSS = `
:host {
  display: block;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  box-sizing: border-box;
  font-family: var(--ha-font-family-body, system-ui, sans-serif);
  color: var(--primary-text-color, #1c1917);
  background: var(--primary-background-color, #fafaf9);
}
.od-host {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}
.od-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.75rem;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--divider-color, #d6d3d1);
  background: var(--card-background-color, #fff);
  flex: 0 0 auto;
}
.od-toolbar label {
  font-size: 0.85rem;
  opacity: 0.8;
}
.od-toolbar select,
.od-toolbar button {
  font: inherit;
  padding: 0.3rem 0.7rem;
}
.od-toolbar button {
  cursor: pointer;
  border: 1px solid var(--primary-color, #2563eb);
  background: var(--primary-color, #2563eb);
  color: var(--text-primary-color, #fff);
  border-radius: 4px;
}
.od-toolbar button.secondary {
  background: transparent;
  color: var(--primary-text-color, #1c1917);
  border-color: var(--divider-color, #a8a29e);
}
.od-toolbar button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.od-status {
  flex: 1 1 auto;
  min-width: 8rem;
  font-size: 0.85rem;
  opacity: 0.85;
}
.od-mount {
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
}
`;

/**
 * @param {any} hass
 * @returns {Array<{ id: string; name: string }>}
 */
function listOpenDisplayDevices(hass) {
  const devices = hass?.devices;
  if (!devices || typeof devices !== 'object') return [];
  /** @type {Array<{ id: string; name: string }>} */
  const out = [];
  for (const [id, d] of Object.entries(devices)) {
    if (!d || typeof d !== 'object') continue;
    let hit = false;
    if (hass?.entities) {
      hit = Object.values(hass.entities).some(
        (e) => e && e.device_id === id && e.platform === 'opendisplay'
      );
    }
    if (!hit) {
      const ids = /** @type {{ identifiers?: unknown }} */ (d).identifiers;
      hit =
        Array.isArray(ids) &&
        ids.some(
          (tuple) => Array.isArray(tuple) && tuple[0] === 'opendisplay'
        );
    }
    if (!hit) continue;
    const dn = /** @type {any} */ (d);
    const name = String(
      dn.name_by_user || dn.name || dn.original_name || id
    ).trim();
    out.push({ id, name });
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  out.push({ id: VIRTUAL_DEVICE_ID, name: 'Virtual device' });
  return out;
}

/**
 * @param {any} hass
 * @param {string} deviceId
 * @returns {string | null}
 */
function imageEntityForDevice(hass, deviceId) {
  const reg = hass?.entities;
  if (!reg || typeof reg !== 'object') return null;
  /** @type {string[]} */
  const imgs = [];
  for (const [key, ent] of Object.entries(reg)) {
    if (!ent || ent.device_id !== deviceId) continue;
    const eid = String(ent.entity_id || key);
    if (eid.startsWith('image.')) imgs.push(eid);
  }
  if (imgs.length === 0) return null;
  return (
    imgs.find(
      (eid) => Number(hass.states?.[eid]?.attributes?.pixel_width) > 0
    ) ?? imgs[0]
  );
}

/**
 * @param {Record<string, unknown>} attrs
 */
function capabilitiesFromAttrs(attrs) {
  const pw = Number(attrs.pixel_width) || DEFAULT_CAPS.pixel_width;
  const ph = Number(attrs.pixel_height) || DEFAULT_CAPS.pixel_height;
  const rw = Number(attrs.render_width) || pw;
  const rh = Number(attrs.render_height) || ph;
  let colorScheme = attrs.color_scheme;
  if (typeof colorScheme === 'string') {
    colorScheme = Number(attrs.color_scheme_value);
  }
  if (typeof colorScheme !== 'number' || Number.isNaN(colorScheme)) {
    colorScheme = DEFAULT_CAPS.color_scheme;
  }
  return {
    pixel_width: pw,
    pixel_height: ph,
    rotation_degrees: Number(attrs.rotation_degrees) || 0,
    render_width: rw,
    render_height: rh,
    color_scheme: colorScheme,
    accent_color: String(attrs.accent_color || DEFAULT_CAPS.accent_color),
    available_colors: Array.isArray(attrs.available_colors)
      ? attrs.available_colors.map(String)
      : [...DEFAULT_CAPS.available_colors],
    color_map:
      attrs.color_map && typeof attrs.color_map === 'object'
        ? /** @type {Record<string, string>} */ (attrs.color_map)
        : { ...DEFAULT_CAPS.color_map },
    palette_measured: Boolean(attrs.palette_measured),
  };
}

/**
 * @param {any} hass
 */
function collectStates(hass) {
  /** @type {Record<string, { state: string; attributes?: Record<string, unknown> }>} */
  const out = {};
  const states = hass?.states;
  if (!states || typeof states !== 'object') return out;
  for (const [eid, st] of Object.entries(states)) {
    if (!st || typeof st !== 'object') continue;
    out[eid] = {
      state: String(/** @type {any} */ (st).state ?? ''),
      attributes:
        /** @type {any} */ (st).attributes &&
        typeof /** @type {any} */ (st).attributes === 'object'
          ? { .../** @type {any} */ (st).attributes }
          : undefined,
    };
  }
  return out;
}

/**
 * @param {unknown} err
 */
function errMsg(err) {
  if (err && typeof err === 'object') {
    const message = Reflect.get(err, 'message');
    const body = Reflect.get(err, 'body');
    let bodyStr = '';
    if (body && typeof body === 'object' && Reflect.get(body, 'message')) {
      bodyStr = String(Reflect.get(body, 'message'));
    }
    return (
      [typeof message === 'string' ? message : '', bodyStr]
        .filter(Boolean)
        .join(' — ') || 'Error'
    );
  }
  return String(err);
}

/**
 * @param {string} text
 * @returns {unknown[]}
 */
function parsePayloadYaml(text) {
  const doc = yaml.load(String(text || '').trim() || '[]');
  if (!Array.isArray(doc)) {
    throw new Error('Payload must be a YAML list of draw elements');
  }
  return doc;
}

/**
 * @param {any} hass
 */
function resolveTheme(hass) {
  const dark =
    hass?.themes?.darkMode === true ||
    (typeof matchMedia === 'function' &&
      matchMedia('(prefers-color-scheme: dark)').matches);
  return dark ? 'dark' : 'light';
}

class OpenDisplayDesignerPanel extends HTMLElement {
  constructor() {
    super();
    /** @type {any} */
    this._hass = null;
    /** @type {ReturnType<typeof mount> | null} */
    this._handle = null;
    /** @type {string} */
    this._deviceId = '';
    /** @type {string} */
    this._lastPayload = '[]\n';
    /** @type {string} */
    this._lastCapsKey = '';
    /** @type {boolean} */
    this._sending = false;
    /** @type {ReturnType<typeof setTimeout> | null} */
    this._pushTimer = null;
  }

  set hass(value) {
    this._hass = value;
    if (!this.isConnected) return;
    this._syncDeviceOptions();
    if (this._pushTimer) clearTimeout(this._pushTimer);
    this._pushTimer = setTimeout(() => {
      this._pushTimer = null;
      this._pushHostData();
    }, 250);
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
    }
    this.style.display = 'block';
    this.style.height = '100%';
    this.style.minHeight = '0';
    this.style.overflow = 'hidden';
    this._renderShell();
    this._mountDesigner();
    this._syncDeviceOptions();
    this._pushHostData();
  }

  disconnectedCallback() {
    if (this._pushTimer) {
      clearTimeout(this._pushTimer);
      this._pushTimer = null;
    }
    this._handle?.destroy();
    this._handle = null;
  }

  _renderShell() {
    const root = this.shadowRoot;
    if (!root || root.querySelector('.od-host')) return;
    root.innerHTML = `
      <style>${HOST_CSS}</style>
      <div class="od-host">
        <div class="od-toolbar">
          <label for="od-device">Device</label>
          <select id="od-device" aria-label="OpenDisplay device"></select>
          <button type="button" class="secondary" id="od-copy">Copy YAML</button>
          <button type="button" id="od-send">Send to display</button>
          <span class="od-status" id="od-status"></span>
        </div>
        <div class="od-mount" id="od-mount"></div>
      </div>
    `;
    const select = /** @type {HTMLSelectElement} */ (
      root.getElementById('od-device')
    );
    select.addEventListener('change', () => {
      this._deviceId = select.value;
      this._lastCapsKey = '';
      this._pushHostData(true);
      this._updateSendEnabled();
    });
    root.getElementById('od-send')?.addEventListener('click', () => {
      void this._sendToDisplay();
    });
    root.getElementById('od-copy')?.addEventListener('click', () => {
      void this._copyPayload();
    });
  }

  _setStatus(text, isError = false) {
    const el = this.shadowRoot?.getElementById('od-status');
    if (!el) return;
    el.textContent = text;
    el.style.color = isError
      ? 'var(--error-color, #b91c1c)'
      : 'var(--primary-text-color, inherit)';
  }

  _updateSendEnabled() {
    const btn = /** @type {HTMLButtonElement | null} */ (
      this.shadowRoot?.getElementById('od-send')
    );
    if (!btn) return;
    const virtual = this._deviceId === VIRTUAL_DEVICE_ID || !this._deviceId;
    btn.disabled = this._sending || virtual;
  }

  _syncDeviceOptions() {
    const select = /** @type {HTMLSelectElement | null} */ (
      this.shadowRoot?.getElementById('od-device')
    );
    if (!select) return;
    const devices = listOpenDisplayDevices(this._hass);
    const prev = this._deviceId || select.value;
    select.replaceChildren();
    if (devices.length === 1 && devices[0].id === VIRTUAL_DEVICE_ID) {
      const opt = document.createElement('option');
      opt.value = VIRTUAL_DEVICE_ID;
      opt.textContent = 'No OpenDisplay devices — virtual display';
      select.append(opt);
    } else {
      for (const d of devices) {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = d.name;
        select.append(opt);
      }
    }
    const ids = devices.map((d) => d.id);
    if (prev && ids.includes(prev)) {
      select.value = prev;
    } else {
      const firstReal = ids.find((id) => id !== VIRTUAL_DEVICE_ID);
      select.value = firstReal || VIRTUAL_DEVICE_ID;
    }
    this._deviceId = select.value;
    this._updateSendEnabled();
  }

  _mountDesigner() {
    if (this._handle) return;
    const mountEl = this.shadowRoot?.getElementById('od-mount');
    if (!mountEl) return;
    try {
      this._handle = mount(mountEl, {
        payload: this._lastPayload,
        states: collectStates(this._hass),
        capabilities: { ...DEFAULT_CAPS },
        lock: false,
        theme: resolveTheme(this._hass),
        onSaveRequest: (payload) => {
          this._lastPayload = String(payload ?? '[]\n');
          this._setStatus('Payload saved — use Send to push to the display');
          void this._copyPayload(true);
        },
      });
      this._setStatus(
        `Designer ${this._handle.version || 'loaded'}`
      );
    } catch (err) {
      this._setStatus(`Failed to mount designer: ${errMsg(err)}`, true);
    }
  }

  /**
   * @param {boolean} [forceCaps]
   */
  _pushHostData(forceCaps = false) {
    if (!this._handle) return;
    try {
      this._handle.setTheme(resolveTheme(this._hass));
      this._handle.setStates(collectStates(this._hass));
    } catch (err) {
      this._setStatus(`State push failed: ${errMsg(err)}`, true);
      return;
    }

    const deviceId = this._deviceId;
    if (!deviceId || deviceId === VIRTUAL_DEVICE_ID) {
      const key = 'virtual';
      if (forceCaps || this._lastCapsKey !== key) {
        this._lastCapsKey = key;
        try {
          this._handle.setCapabilities({ ...DEFAULT_CAPS }, { lock: false });
        } catch (err) {
          this._setStatus(`Capabilities push failed: ${errMsg(err)}`, true);
        }
      }
      return;
    }

    const eid = imageEntityForDevice(this._hass, deviceId);
    const attrs = eid ? this._hass?.states?.[eid]?.attributes : null;
    if (!attrs || typeof attrs !== 'object') {
      if (forceCaps) {
        this._setStatus('Waiting for display capability attributes…');
      }
      return;
    }
    const caps = capabilitiesFromAttrs(attrs);
    const key = JSON.stringify(caps);
    if (!forceCaps && key === this._lastCapsKey) return;
    this._lastCapsKey = key;
    try {
      this._handle.setCapabilities(caps, { lock: true });
      this._setStatus(
        `${caps.render_width}×${caps.render_height} · ${caps.accent_color}`
      );
    } catch (err) {
      this._setStatus(`Capabilities push failed: ${errMsg(err)}`, true);
    }
  }

  /**
   * @param {boolean} [quiet]
   */
  async _copyPayload(quiet = false) {
    const text = this._lastPayload || '[]\n';
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.append(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      if (!quiet) this._setStatus('YAML copied to clipboard');
    } catch (err) {
      this._setStatus(`Copy failed: ${errMsg(err)}`, true);
    }
  }

  async _sendToDisplay() {
    const hass = this._hass;
    const deviceId = this._deviceId;
    if (!hass?.callService) {
      this._setStatus('Home Assistant connection unavailable', true);
      return;
    }
    if (!deviceId || deviceId === VIRTUAL_DEVICE_ID) {
      this._setStatus('Select a real OpenDisplay device to send', true);
      return;
    }
    let payload;
    try {
      payload = parsePayloadYaml(this._lastPayload);
    } catch (err) {
      this._setStatus(errMsg(err), true);
      return;
    }
    this._sending = true;
    this._updateSendEnabled();
    this._setStatus('Sending drawcustom…');
    try {
      await hass.callService(
        'opendisplay',
        'drawcustom',
        { payload, background: 'white', dither: 'ordered', refresh_type: 'full' },
        { device_id: deviceId }
      );
      this._setStatus(`Sent at ${new Date().toLocaleTimeString()}`);
    } catch (err) {
      this._setStatus(`Send failed: ${errMsg(err)}`, true);
    } finally {
      this._sending = false;
      this._updateSendEnabled();
    }
  }
}

if (!customElements.get(TAG)) {
  customElements.define(TAG, OpenDisplayDesignerPanel);
}

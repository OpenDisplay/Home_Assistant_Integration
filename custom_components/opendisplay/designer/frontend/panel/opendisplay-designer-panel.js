/**
 * HA panel host for vendored odl-drawcustom-designer (mount + drawcustom send).
 */
import { mount } from '../vendor/odl-drawcustom-designer.js';
import yaml from '../vendor/js-yaml.mjs';

const TAG = 'opendisplay-designer-panel';
const VIRTUAL = '__virtual__';
const DEFAULT_CAPS = {
  pixel_width: 296,
  pixel_height: 128,
  rotation_degrees: 0,
  render_width: 296,
  render_height: 128,
  color_scheme: 0x01,
  accent_color: 'red',
  available_colors: ['black', 'white', 'red'],
  color_map: { black: '#000000', white: '#ffffff', red: '#c53929' },
  palette_measured: false,
};

const CSS = `
:host{display:block!important;position:absolute;inset:0;width:100%;height:100%;max-width:none;overflow:hidden;box-sizing:border-box;font-family:var(--ha-font-family-body,system-ui,sans-serif);color:var(--primary-text-color,#1c1917);background:var(--primary-background-color,#fafaf9)}
.od-host{display:flex;flex-direction:column;width:100%;height:100%;min-height:0;min-width:0;overflow:hidden;box-sizing:border-box}
.od-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem .75rem;padding:.5rem .75rem;border-bottom:1px solid var(--divider-color,#d6d3d1);background:var(--card-background-color,#fff);flex:0 0 auto;width:100%;box-sizing:border-box}
.od-toolbar label{font-size:.85rem;opacity:.8}
.od-toolbar select,.od-toolbar button{font:inherit;padding:.3rem .7rem}
.od-toolbar button{cursor:pointer;border:1px solid var(--primary-color,#2563eb);background:var(--primary-color,#2563eb);color:var(--text-primary-color,#fff);border-radius:4px}
.od-toolbar button.secondary{background:transparent;color:var(--primary-text-color,#1c1917);border-color:var(--divider-color,#a8a29e)}
.od-toolbar button:disabled{opacity:.5;cursor:not-allowed}
.od-last-seen,.od-status{font-size:.85rem;opacity:.85}
.od-status{flex:1 1 auto;min-width:8rem}
.od-last-seen{white-space:nowrap}
.od-mount{flex:1 1 0;min-height:0;min-width:0;width:100%;overflow:hidden;position:relative}
.od-mount>*{width:100%!important;height:100%!important;max-width:none!important;box-sizing:border-box}
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

function listDevices(hass) {
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
  out.push({ id: VIRTUAL, name: 'Virtual device' });
  return out;
}

function entityForDevice(hass, deviceId, prefix, pred) {
  const reg = hass?.entities;
  if (!reg) return null;
  const ids = [];
  for (const [key, ent] of Object.entries(reg)) {
    if (!ent || ent.device_id !== deviceId) continue;
    if (ent.platform && ent.platform !== 'opendisplay') continue;
    const eid = String(ent.entity_id || key);
    if (!eid.startsWith(prefix)) continue;
    if (pred && !pred(eid, ent)) continue;
    ids.push(eid);
  }
  return ids[0] || null;
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

function lastSeenIso(hass, deviceId) {
  const eid = entityForDevice(
    hass,
    deviceId,
    'sensor.',
    (id, ent) =>
      id.includes('last_seen') ||
      String(ent.unique_id || '').endsWith('last_seen') ||
      ent.translation_key === 'last_seen'
  );
  const st = eid ? hass.states?.[eid] : null;
  return st ? String(st.state ?? '') : null;
}

function formatLastSeen(iso) {
  if (!iso || iso === 'unknown' || iso === 'unavailable' || iso === 'none') {
    return 'Last seen: —';
  }
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return `Last seen: ${iso}`;
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `Last seen: ${sec}s ago`;
  if (sec < 3600) return `Last seen: ${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `Last seen: ${Math.floor(sec / 3600)}h ago`;
  return `Last seen: ${Math.floor(sec / 86400)}d ago`;
}

function capsFromAttrs(attrs) {
  const pw = Number(attrs.pixel_width) || DEFAULT_CAPS.pixel_width;
  const ph = Number(attrs.pixel_height) || DEFAULT_CAPS.pixel_height;
  let colorScheme = attrs.color_scheme;
  if (typeof colorScheme !== 'number' || Number.isNaN(colorScheme)) {
    colorScheme = DEFAULT_CAPS.color_scheme;
  }
  return {
    pixel_width: pw,
    pixel_height: ph,
    rotation_degrees: Number(attrs.rotation_degrees) || 0,
    render_width: Number(attrs.render_width) || pw,
    render_height: Number(attrs.render_height) || ph,
    color_scheme: colorScheme,
    accent_color: String(attrs.accent_color || DEFAULT_CAPS.accent_color),
    available_colors: Array.isArray(attrs.available_colors)
      ? attrs.available_colors.map(String)
      : [...DEFAULT_CAPS.available_colors],
    color_map:
      attrs.color_map && typeof attrs.color_map === 'object'
        ? attrs.color_map
        : { ...DEFAULT_CAPS.color_map },
    palette_measured: Boolean(attrs.palette_measured),
  };
}

function collectStates(hass) {
  const out = {};
  const states = hass?.states;
  if (!states) return out;
  for (const [eid, st] of Object.entries(states)) {
    if (!st || typeof st !== 'object') continue;
    out[eid] = {
      state: String(st.state ?? ''),
      attributes:
        st.attributes && typeof st.attributes === 'object'
          ? { ...st.attributes }
          : undefined,
    };
  }
  return out;
}

function parsePayload(text) {
  // YAML 1.2 CORE_SCHEMA keeps key `y` (1.1 would booleanize it).
  const doc = yaml.load(String(text || '').trim() || '[]', {
    schema: yaml.CORE_SCHEMA,
  });
  if (!Array.isArray(doc)) throw new Error('Payload must be a YAML list');
  return doc;
}

function dumpPayload(elements) {
  return yaml
    .dump(elements, { schema: yaml.CORE_SCHEMA, lineWidth: -1, quotingType: '"' })
    .replace(/^(\s*)y:/gm, '$1"y":');
}

function theme(hass) {
  const dark =
    hass?.themes?.darkMode === true ||
    (typeof matchMedia === 'function' &&
      matchMedia('(prefers-color-scheme: dark)').matches);
  return dark ? 'dark' : 'light';
}

class OpenDisplayDesignerPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._handle = null;
    this._deviceId = '';
    this._lastPayload = '[]\n';
    this._lastCapsKey = '';
    this._sending = false;
    this._pushTimer = null;
    this._refreshType = 'full';
  }

  set hass(value) {
    this._hass = value;
    if (!this.isConnected) return;
    this._syncDevices();
    this._updateLastSeen();
    clearTimeout(this._pushTimer);
    this._pushTimer = setTimeout(() => {
      this._pushTimer = null;
      this._pushHostData();
    }, 250);
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
      if (!p.style.position || p.style.position === 'static') p.style.position = 'relative';
      p.style.height = p.style.height || '100%';
    }
    this._renderShell();
    this._mount();
    this._syncDevices();
    this._updateLastSeen();
    this._pushHostData();
  }

  disconnectedCallback() {
    clearTimeout(this._pushTimer);
    this._pushTimer = null;
    this._handle?.destroy();
    this._handle = null;
  }

  _$(id) {
    return this.shadowRoot?.getElementById(id);
  }

  _status(text, isError = false) {
    const el = this._$('od-status');
    if (!el) return;
    el.textContent = text;
    el.style.color = isError
      ? 'var(--error-color, #b91c1c)'
      : 'var(--primary-text-color, inherit)';
  }

  _renderShell() {
    const root = this.shadowRoot;
    if (!root || root.querySelector('.od-host')) return;
    root.innerHTML = `
      <style>${CSS}</style>
      <div class="od-host">
        <div class="od-toolbar">
          <label for="od-device">Device</label>
          <select id="od-device" aria-label="OpenDisplay device"></select>
          <label for="od-refresh">Refresh</label>
          <select id="od-refresh" aria-label="Refresh mode">
            <option value="full" selected>full</option>
            <option value="fast">fast</option>
            <option value="partial">partial</option>
          </select>
          <span class="od-last-seen" id="od-last-seen" hidden></span>
          <button type="button" class="secondary" id="od-copy">Copy YAML</button>
          <button type="button" id="od-send" title="Save current design and send">Send to display</button>
          <span class="od-status" id="od-status"></span>
        </div>
        <div class="od-mount" id="od-mount"></div>
      </div>`;
    this._$('od-device')?.addEventListener('change', (ev) => {
      this._deviceId = /** @type {HTMLSelectElement} */ (ev.target).value;
      this._lastCapsKey = '';
      this._pushHostData(true);
      this._updateSendEnabled();
      this._updateLastSeen();
    });
    const refresh = /** @type {HTMLSelectElement} */ (this._$('od-refresh'));
    refresh.value = this._refreshType;
    refresh.addEventListener('change', () => {
      const v = refresh.value;
      this._refreshType = v === 'fast' || v === 'partial' ? v : 'full';
    });
    this._$('od-send')?.addEventListener('click', () => void this._send());
    this._$('od-copy')?.addEventListener('click', () => void this._copy());
  }

  _updateSendEnabled() {
    const btn = /** @type {HTMLButtonElement | null} */ (this._$('od-send'));
    if (btn) btn.disabled = this._sending || !this._deviceId || this._deviceId === VIRTUAL;
  }

  _syncDevices() {
    const select = /** @type {HTMLSelectElement | null} */ (this._$('od-device'));
    if (!select) return;
    const devices = listDevices(this._hass);
    const prev = this._deviceId || select.value;
    select.replaceChildren();
    for (const d of devices) {
      const opt = document.createElement('option');
      opt.value = d.id;
      opt.textContent =
        devices.length === 1 && d.id === VIRTUAL
          ? 'No OpenDisplay devices — virtual display'
          : d.name;
      select.append(opt);
    }
    const ids = devices.map((d) => d.id);
    select.value =
      (prev && ids.includes(prev) && prev) ||
      ids.find((id) => id !== VIRTUAL) ||
      VIRTUAL;
    this._deviceId = select.value;
    this._updateSendEnabled();
    this._updateLastSeen();
  }

  _updateLastSeen() {
    const el = this._$('od-last-seen');
    if (!el) return;
    if (!this._deviceId || this._deviceId === VIRTUAL) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    const iso = lastSeenIso(this._hass, this._deviceId);
    el.hidden = false;
    el.textContent = formatLastSeen(iso);
    el.title = iso && iso !== 'unknown' && iso !== 'unavailable' ? iso : '';
  }

  _mount() {
    if (this._handle) return;
    const mountEl = this._$('od-mount');
    if (!mountEl) return;
    try {
      this._handle = mount(mountEl, {
        payload: this._lastPayload,
        states: collectStates(this._hass),
        capabilities: { ...DEFAULT_CAPS },
        lock: false,
        theme: theme(this._hass),
        onSaveRequest: (payload) => {
          this._lastPayload = String(payload ?? '[]\n');
          void this._send({ fromSave: true });
        },
      });
      this._status(`Designer ${this._handle.version || 'loaded'} — Save or Send`);
    } catch (err) {
      this._status(`Failed to mount designer: ${errMsg(err)}`, true);
    }
  }

  _pushHostData(forceCaps = false) {
    if (!this._handle) return;
    try {
      this._handle.setTheme(theme(this._hass));
      this._handle.setStates(collectStates(this._hass));
    } catch (err) {
      this._status(`State push failed: ${errMsg(err)}`, true);
      return;
    }
    if (!this._deviceId || this._deviceId === VIRTUAL) {
      if (forceCaps || this._lastCapsKey !== 'virtual') {
        this._lastCapsKey = 'virtual';
        try {
          this._handle.setCapabilities({ ...DEFAULT_CAPS }, { lock: false });
        } catch (err) {
          this._status(`Capabilities push failed: ${errMsg(err)}`, true);
        }
      }
      return;
    }
    const eid = imageEntity(this._hass, this._deviceId);
    const attrs = eid ? this._hass?.states?.[eid]?.attributes : null;
    if (!attrs || typeof attrs !== 'object') {
      if (forceCaps) this._status('Waiting for display capability attributes…');
      return;
    }
    const caps = capsFromAttrs(attrs);
    const key = JSON.stringify(caps);
    if (!forceCaps && key === this._lastCapsKey) return;
    this._lastCapsKey = key;
    try {
      this._handle.setCapabilities(caps, { lock: true });
      this._status(`${caps.render_width}×${caps.render_height} · ${caps.accent_color}`);
    } catch (err) {
      this._status(`Capabilities push failed: ${errMsg(err)}`, true);
    }
  }

  async _copy() {
    let text = this._lastPayload || '[]\n';
    try {
      text = dumpPayload(parsePayload(text));
    } catch {
      /* keep raw */
    }
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.append(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
      }
      this._status('YAML copied to clipboard');
    } catch (err) {
      this._status(`Copy failed: ${errMsg(err)}`, true);
    }
  }

  _clickSave() {
    const mountEl = this._$('od-mount');
    if (!mountEl) return false;
    const search = (root) => {
      for (const btn of root.querySelectorAll?.('button') ?? []) {
        if (/^save$/i.test((btn.textContent || '').replace(/\s+/g, ' ').trim())) {
          return btn;
        }
      }
      for (const el of root.querySelectorAll?.('*') ?? []) {
        if (el.shadowRoot) {
          const hit = search(el.shadowRoot);
          if (hit) return hit;
        }
      }
      return null;
    };
    const btn = search(mountEl.shadowRoot || mountEl);
    if (!btn) return false;
    btn.click();
    return true;
  }

  async _send(opts = {}) {
    const hass = this._hass;
    const deviceId = this._deviceId;
    if (!hass?.callService) {
      this._status('Home Assistant connection unavailable', true);
      return;
    }
    if (!deviceId || deviceId === VIRTUAL) {
      this._status(
        opts.fromSave
          ? 'Saved locally — pick a real device, then Save/Send again'
          : 'Select a real OpenDisplay device to send',
        true
      );
      return;
    }
    if (!opts.fromSave) {
      this._status('Saving current design…');
      if (!this._clickSave()) {
        this._status('Could not trigger designer Save — click Save in the designer', true);
      }
      return;
    }
    let payload;
    try {
      payload = parsePayload(this._lastPayload);
    } catch (err) {
      this._status(errMsg(err), true);
      return;
    }
    if (!payload.length) {
      this._status('Nothing to send — add elements, then Save/Send', true);
      return;
    }
    this._sending = true;
    this._updateSendEnabled();
    this._status(`Sending ${payload.length} element(s)…`);
    try {
      await hass.callService(
        'opendisplay',
        'drawcustom',
        {
          payload,
          background: 'white',
          dither: 'ordered',
          refresh_type: this._refreshType || 'full',
          device_id: [deviceId],
        },
        { device_id: deviceId }
      );
      this._status(`Sent ${payload.length} element(s) at ${new Date().toLocaleTimeString()}`);
    } catch (err) {
      this._status(`Send failed: ${errMsg(err)}`, true);
    } finally {
      this._sending = false;
      this._updateSendEnabled();
    }
  }
}

if (!customElements.get(TAG)) {
  customElements.define(TAG, OpenDisplayDesignerPanel);
}

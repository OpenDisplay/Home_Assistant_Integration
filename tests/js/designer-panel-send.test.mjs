// Wrapper-level tests for opendisplay-designer-panel.js's `send` action and
// `renderPreview` seam: the REAL panel module, with the vendored designer
// bundle replaced by a stub `mount` through a `node:module` load hook and
// the few DOM globals the module touches at import time faked. What these
// prove: with `context.variables` present, the `opendisplay.drawcustom`
// service call and the render-endpoint POST both receive LITERAL field
// values -- every template rendered through HA's `render_template`
// websocket command with those variables -- and a template failure aborts
// the send/preview with an error that names the element and field; with
// `context.variables` ABSENT (a designer build older than 3.7.0, which
// introduced the field; the pinned 3.8.0 always sends it), the payload
// passes through untouched and no render_template is ever sent, exactly as
// before pre-rendering existed.
//
// Plain `node --test`, zero dependencies -- see key-containment.test.mjs's
// own header comment for why this repo tests panel JS this way.
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { test } from 'node:test';

// The 5.6MB vendored designer expects a browser; the panel only needs its
// `mount` export to resolve. Only that one import is replaced -- js-yaml
// and every panel module under test load for real.
registerHooks({
  load(url, context, nextLoad) {
    if (url.endsWith('/vendor/odl-drawcustom-designer.js')) {
      return {
        format: 'module',
        source: 'export function mount() { throw new Error("stub mount: not for these tests"); }',
        shortCircuit: true,
      };
    }
    return nextLoad(url, context);
  },
});

class FakeElement {
  constructor() {
    this.style = {};
    this.isConnected = false;
    this.events = [];
  }
  dispatchEvent(event) {
    this.events.push(event);
    return true;
  }
}
globalThis.HTMLElement = FakeElement;
globalThis.CustomEvent = class {
  constructor(type, init) {
    this.type = type;
    this.detail = init?.detail;
  }
};
let PanelClass;
globalThis.customElements = {
  get: () => undefined,
  define: (_tag, cls) => {
    PanelClass = cls;
  },
};
await import('../../custom_components/opendisplay/designer/frontend/panel/opendisplay-designer-panel.js');
assert.ok(PanelClass, 'the panel registered its custom element');

const DEMO_CIRCLE_FILL = "{{ 'red' if alert else 'white' }}";
const PAYLOAD_YAML = `
- type: circle
  x: 20
  y: 20
  radius: 10
  fill: ${JSON.stringify(DEMO_CIRCLE_FILL)}
- type: text
  value: hello
  color: "{{ accent_color }}"
`;
const PARSED_PAYLOAD = [
  { type: 'circle', x: 20, y: 20, radius: 10, fill: DEMO_CIRCLE_FILL },
  { type: 'text', value: 'hello', color: '{{ accent_color }}' },
];

/**
 * What a strict Jinja render of the two demo templates does: each entry
 * names the variables the template references and computes its value from
 * them. The fake below enforces `message.strict` and `message.variables`
 * exactly like HA -- a referenced name missing from the variables is an
 * `error` event under strict (HA's `UndefinedError`), '' otherwise -- so a
 * test cannot go green by handing out a canned answer for a variable that
 * was never sent.
 */
const JINJA = {
  [DEMO_CIRCLE_FILL]: { needs: ['alert'], value: (v) => (v.alert ? 'red' : 'white') },
  '{{ accent_color }}': { needs: ['accent_color'], value: (v) => v.accent_color },
};

/** A frozen designer context; `variables` only when given (designer >= 3.7.0, as the pinned 3.8.0 is). */
function context(variables, targetId = 'dev1') {
  return Object.freeze({
    targetId,
    display: Object.freeze({ width: 296, height: 128, rotation: 0 }),
    render: Object.freeze({ dither: 0 }),
    ...(variables === undefined ? {} : { variables: Object.freeze(variables) }),
  });
}

/** `hass.connection.subscribeMessage` rendering `render_template` with JINJA above, strictly. */
function fakeConnection() {
  const messages = [];
  return {
    messages,
    subscribeMessage(callback, message) {
      messages.push(message);
      const entry = JINJA[message.template];
      assert.ok(entry, `unknown template in test: ${message.template}`);
      const variables = message.variables ?? {};
      const missing = entry.needs.find((name) => !(name in variables));
      queueMicrotask(() => {
        if (missing && message.strict) {
          callback({ error: `UndefinedError: '${missing}' is undefined`, level: 'ERROR' });
        } else {
          callback({ result: missing ? '' : entry.value(variables), listeners: {} });
        }
      });
      return Promise.resolve(() => {});
    },
  };
}

function panelWith({ fetchWithAuth } = {}) {
  const panel = new PanelClass();
  const serviceCalls = [];
  panel._hass = {
    callService: async (...args) => {
      serviceCalls.push(args);
    },
    fetchWithAuth,
    connection: fakeConnection(),
  };
  panel._targetDisplaySpecs = new Map([['dev1', { rotationDegrees: 0 }]]);
  return { panel, serviceCalls };
}

function notifications(panel) {
  return panel.events.filter((e) => e.type === 'hass-notification').map((e) => e.detail.message);
}

function recordingFetch(posts) {
  return async (url, init) => {
    posts.push([url, JSON.parse(init.body)]);
    return { ok: true, blob: async () => 'png-bytes' };
  };
}

test('send: the service receives rendered literals, with context.variables as the template context', async () => {
  const { panel, serviceCalls } = panelWith();
  const variables = { alert: 'true', accent_color: 'red' };

  await panel._send(PAYLOAD_YAML, context(variables));

  assert.equal(serviceCalls.length, 1, `one service call; notifications: ${notifications(panel)}`);
  const [domain, service, data, target] = serviceCalls[0];
  assert.equal(domain, 'opendisplay');
  assert.equal(service, 'drawcustom');
  assert.deepEqual(target, { device_id: 'dev1' });
  assert.deepEqual(data.payload, [
    { type: 'circle', x: 20, y: 20, radius: 10, fill: 'red' },
    { type: 'text', value: 'hello', color: 'red' },
  ]);
  assert.deepEqual(data.device_id, ['dev1']);
  // Every render_template went out with the designer's variables, strict.
  assert.equal(panel._hass.connection.messages.length, 2);
  for (const message of panel._hass.connection.messages) {
    assert.equal(message.type, 'render_template');
    assert.deepEqual(message.variables, variables);
    assert.equal(message.strict, true);
  }
  assert.match(notifications(panel).at(-1), /^Sent 2 element\(s\)/);
});

test('send: typed variable values (as designer >= 3.7.0 coerces them) reach render_template as JSON natives', async () => {
  // The vendored designer's report builder coerces each stored string the
  // way its own preview does: "false" -> false, "5" -> 5, "none"/"" ->
  // null, "[1, 2]" -> a list, "{...}" -> an object, a referenced-but-unset
  // name -> "". The panel hands that map to HA untouched, so `alert: false`
  // is falsy in Jinja (circle white), not the truthy string "false".
  const { panel, serviceCalls } = panelWith();
  const variables = Object.freeze({
    alert: false,
    accent_color: 'red',
    count: 5,
    nothing: null,
    unset: '',
    list: Object.freeze([1, 2]),
    obj: Object.freeze({ a: 1 }),
  });

  await panel._send(PAYLOAD_YAML, context(variables));

  assert.equal(serviceCalls.length, 1, `notifications: ${notifications(panel)}`);
  assert.deepEqual(serviceCalls[0][2].payload, [
    { type: 'circle', x: 20, y: 20, radius: 10, fill: 'white' },
    { type: 'text', value: 'hello', color: 'red' },
  ]);
  for (const message of panel._hass.connection.messages) {
    assert.equal(message.variables, variables, 'the designer map goes out as-is, no copy, no stringifying');
    assert.deepEqual(JSON.parse(JSON.stringify(message.variables)), {
      alert: false,
      accent_color: 'red',
      count: 5,
      nothing: null,
      unset: '',
      list: [1, 2],
      obj: { a: 1 },
    });
  }
});

test('send: a variable the payload references but the designer did not define aborts the call, naming element and field', async () => {
  const { panel, serviceCalls } = panelWith();

  await panel._send(PAYLOAD_YAML, context({ alert: 'true' }));

  assert.equal(serviceCalls.length, 0, 'the service must not be called with a half-rendered payload');
  assert.deepEqual(notifications(panel), [
    "Send failed: Element 1 (type 'text') field 'color' has an invalid template:" +
      " UndefinedError: 'accent_color' is undefined",
  ]);
  assert.equal(panel._sending, false, 'the in-flight guard is released');
});

test('send: a designer WITHOUT context.variables sends the payload untouched and renders nothing client-side', async () => {
  // A designer older than 3.7.0 never hands over `variables` (the pinned
  // 3.8.0 always does). Pre-rendering strictly with {} would fail every
  // variable reference; instead the panel behaves exactly as before: HA
  // renders server-side.
  const { panel, serviceCalls } = panelWith();

  await panel._send(PAYLOAD_YAML, context(undefined));

  assert.equal(serviceCalls.length, 1, `notifications: ${notifications(panel)}`);
  assert.deepEqual(serviceCalls[0][2].payload, PARSED_PAYLOAD);
  assert.equal(panel._hass.connection.messages.length, 0, 'no render_template subscription at all');
});

test('preview: the render endpoint receives the same rendered literals', async () => {
  const posts = [];
  const { panel } = panelWith({ fetchWithAuth: recordingFetch(posts) });

  const blob = await panel._renderPreview(PAYLOAD_YAML, context({ alert: 'true', accent_color: 'red' }));

  assert.equal(blob, 'png-bytes');
  assert.equal(posts.length, 1);
  const [url, body] = posts[0];
  assert.equal(url, '/api/opendisplay/designer/render');
  assert.deepEqual(body.payload, [
    { type: 'circle', x: 20, y: 20, radius: 10, fill: 'red' },
    { type: 'text', value: 'hello', color: 'red' },
  ]);
  assert.equal(body.device_id, 'dev1');
});

test('preview: a failing template rejects before any POST, naming element and field', async () => {
  const posts = [];
  const { panel } = panelWith({ fetchWithAuth: recordingFetch(posts) });

  await assert.rejects(panel._renderPreview(PAYLOAD_YAML, context({ accent_color: 'red' })), {
    message:
      "Element 0 (type 'circle') field 'fill' has an invalid template:" +
      " UndefinedError: 'alert' is undefined",
  });
  assert.equal(posts.length, 0);
});

test('preview: a designer WITHOUT context.variables POSTs the payload untouched and renders nothing client-side', async () => {
  const posts = [];
  const { panel } = panelWith({ fetchWithAuth: recordingFetch(posts) });

  await panel._renderPreview(PAYLOAD_YAML, context(undefined));

  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0][1].payload, PARSED_PAYLOAD);
  assert.equal(panel._hass.connection.messages.length, 0);
});

test('send and preview with variables but no websocket connection report the connection as unavailable', async () => {
  const { panel, serviceCalls } = panelWith({ fetchWithAuth: recordingFetch([]) });
  delete panel._hass.connection;

  await panel._send(PAYLOAD_YAML, context({}));
  assert.equal(serviceCalls.length, 0);
  assert.deepEqual(notifications(panel), ['Send failed: Home Assistant connection unavailable']);
  await assert.rejects(panel._renderPreview(PAYLOAD_YAML, context({})), {
    message: 'Home Assistant connection unavailable',
  });
});

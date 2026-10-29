// Unit tests for loadHostDemoOptions / hostDemoUrls (host-demo.js): the
// panel hands the designer this integration's own Load Demo (frontend/demo/
// demo.yml + demo.json) through the `demo` mount option, and a failure to
// fetch it must fall back to the designer's bundled demo without breaking
// the mount. Plain `node --test`, zero dependencies -- see
// key-containment.test.mjs's own header comment for why this repo tests
// panel JS this way (the panel class itself imports the ~5.6MB vendored
// designer bundle and cannot be imported under `node --test`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  DEFAULT_TIMEOUT_MS,
  hostDemoUrls,
  loadHostDemoOptions,
} from '../../custom_components/opendisplay/designer/frontend/panel/host-demo.js';

const MODULE_URL =
  'https://ha.example/api/opendisplay/designer/static/panel/opendisplay-designer-panel.js?v=20260903a-123';
const PAYLOAD = '- type: text\n  value: hi\n';
const VARIABLES = { accent_color: 'red', alert: 'true' };

function okFetch(overrides = {}) {
  const bodies = {
    'demo.yml': { ok: true, status: 200, text: async () => PAYLOAD },
    'demo.json': {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ variables: VARIABLES }),
    },
    ...overrides,
  };
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    const name = String(url).split('/').pop();
    const res = bodies[name];
    if (res instanceof Error) throw res;
    if (res === 'hang') {
      // A fetch that never settles on its own -- only the abort signal the
      // module passes can end it, exactly like a stalled network request.
      return new Promise((_, reject) => {
        init.signal.addEventListener('abort', () => reject(init.signal.reason));
      });
    }
    return res;
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

function fallbackSpy() {
  const spy = (...args) => spy.calls.push(args);
  spy.calls = [];
  return spy;
}

async function load(fetchImpl, onFallback, extra = {}) {
  return await loadHostDemoOptions(MODULE_URL, { fetchImpl, onFallback, ...extra });
}

test('the demo files live next to the panel module, served by the same static view', () => {
  const urls = hostDemoUrls(MODULE_URL);
  assert.equal(
    urls.payload.href,
    'https://ha.example/api/opendisplay/designer/static/demo/demo.yml'
  );
  assert.equal(
    urls.variables.href,
    'https://ha.example/api/opendisplay/designer/static/demo/demo.json'
  );
});

test('the loaded demo becomes the designer `demo` mount option, payload and variables', async () => {
  const onFallback = fallbackSpy();
  const options = await load(okFetch(), onFallback);
  assert.deepEqual(options, { demo: { payload: PAYLOAD, variables: VARIABLES } });
  assert.deepEqual(onFallback.calls, []);
});

test('every fetch carries an abort signal with the default timeout', async () => {
  const fetchImpl = okFetch();
  await load(fetchImpl, fallbackSpy());
  assert.equal(fetchImpl.calls.length, 2);
  for (const { init } of fetchImpl.calls) {
    assert.ok(init.signal instanceof AbortSignal, 'fetch was given an AbortSignal');
  }
  assert.ok(DEFAULT_TIMEOUT_MS >= 1000, 'the default timeout is not absurdly short');
});

test('a fetch that never settles is aborted after the timeout and falls back, so the mount never waits forever', async () => {
  const onFallback = fallbackSpy();
  const fetchImpl = okFetch({ 'demo.yml': 'hang' });
  const started = Date.now();
  const options = await load(fetchImpl, onFallback, { timeoutMs: 50 });
  assert.deepEqual(options, {});
  assert.ok(Date.now() - started < 5000, 'resolved by the timeout, not by luck');
  assert.equal(onFallback.calls.length, 1);
  assert.match(onFallback.calls[0][0], /timed out|TimeoutError/i);
});

test('a fetch that rejects falls back to the bundled demo: no option, one user-facing message, no throw', async () => {
  const onFallback = fallbackSpy();
  const fetchImpl = okFetch({ 'demo.yml': new Error('offline') });
  const options = await load(fetchImpl, onFallback);
  assert.deepEqual(options, {});
  assert.equal(onFallback.calls.length, 1);
  const [message] = onFallback.calls[0];
  assert.equal(typeof message, 'string');
  assert.match(message, /offline/);
  // The message is what the panel shows the user, so it says what Load Demo
  // will do instead -- not just that something failed.
  assert.match(message, /bundled demo/);
});

test('a non-2xx answer for either file falls back the same way', async () => {
  for (const name of ['demo.yml', 'demo.json']) {
    const onFallback = fallbackSpy();
    const fetchImpl = okFetch({ [name]: { ok: false, status: 404, text: async () => '' } });
    assert.deepEqual(await load(fetchImpl, onFallback), {});
    assert.equal(onFallback.calls.length, 1, name);
    assert.match(onFallback.calls[0][0], /404/);
  }
});

test('a malformed variables file falls back rather than mounting a half demo', async () => {
  const cases = [
    'not json',
    JSON.stringify({ variables: 'red' }),
    JSON.stringify({ variables: { alert: true } }),
  ];
  for (const body of cases) {
    const onFallback = fallbackSpy();
    const fetchImpl = okFetch({
      'demo.json': { ok: true, status: 200, text: async () => body },
    });
    assert.deepEqual(await load(fetchImpl, onFallback), {}, body);
    assert.equal(onFallback.calls.length, 1, body);
  }
});

test('a payload the host parser rejects falls back: malformed demo YAML must not throw out of mount()', async () => {
  // HostDemo.payload is validated like MountOptions.payload -- malformed
  // YAML throws out of mount() (vendored .d.ts) -- so the module runs the
  // host's own parse first and treats a throw as any other failure.
  const onFallback = fallbackSpy();
  const parsePayload = (text) => {
    if (text === PAYLOAD) return [];
    throw new Error('bad indentation at line 2');
  };
  assert.deepEqual(
    await load(okFetch(), onFallback, { parsePayload }),
    { demo: { payload: PAYLOAD, variables: VARIABLES } },
    'a parseable payload still loads'
  );
  const fetchImpl = okFetch({ 'demo.yml': { ok: true, status: 200, text: async () => '- :\n  x' } });
  assert.deepEqual(await load(fetchImpl, onFallback, { parsePayload }), {});
  assert.equal(onFallback.calls.length, 1);
  assert.match(onFallback.calls[0][0], /not valid YAML.*bad indentation/);
});

test('an empty payload file falls back rather than offering an empty demo', async () => {
  const onFallback = fallbackSpy();
  const fetchImpl = okFetch({ 'demo.yml': { ok: true, status: 200, text: async () => '  \n' } });
  assert.deepEqual(await load(fetchImpl, onFallback), {});
  assert.equal(onFallback.calls.length, 1);
});

test('a variables file without variables still yields the demo (variables are optional)', async () => {
  const onFallback = fallbackSpy();
  const fetchImpl = okFetch({
    'demo.json': { ok: true, status: 200, text: async () => '{}' },
  });
  assert.deepEqual(await load(fetchImpl, onFallback), {
    demo: { payload: PAYLOAD },
  });
  assert.deepEqual(onFallback.calls, []);
});

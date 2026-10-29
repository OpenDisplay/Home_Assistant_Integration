/**
 * This integration's own Load Demo for the designer's `demo` mount option
 * (`MountOptions.demo`, a `HostDemo`: `{ payload: string; variables?:
 * Record<string, string> }`, designer 3.8.0).
 *
 * The designer's bundled showcase references entities a real Home
 * Assistant does not have (sensor.temperature, weather.forecast_home, …)
 * and a plot element that needs recorder history, so on a live install its
 * fields rendered "unknown" and Send failed. Home Assistant ships its own
 * demo instead -- `frontend/demo/demo.yml` (the payload) and `demo.json`
 * (its variables), the one copy both this panel and
 * `tests/test_designer_demo_render.py` read -- served by the same
 * unauthenticated static view as this module, so a plain `fetch` reaches
 * them. The URLs carry no `?v=` token on purpose: the static view answers
 * a bare path with `no-cache, no-store`, so the browser fetches the demo
 * afresh on every mount and a demo edit never waits for a panel JS change.
 *
 * A failure to load the demo must not break the mount: every failure path
 * -- a rejected fetch, a non-2xx answer, a malformed file, or a fetch that
 * never settles (each fetch is aborted after `timeoutMs` by a timer that
 * is cleared the moment the fetch settles, so a hang becomes a rejection
 * instead of leaving the designer unmounted forever) --
 * resolves to `{}` (no `demo` key, the designer keeps its bundled demo)
 * after exactly one `onFallback(message)` call, never a throw. The panel
 * routes that message to the console and to its notification toast, so
 * the user learns why Load Demo shows the bundled showcase.
 *
 * "Malformed file" includes the payload's YAML: `HostDemo.payload` is
 * "validated the same way [as `MountOptions.payload`]: malformed YAML
 * throws out of `mount()`" (vendored `.d.ts`), which would take the whole
 * panel down for a demo typo. The panel passes its own `parsePayload`
 * (js-yaml, the same parse a send runs) as `parsePayload` here, so a
 * payload that does not parse falls back like any other failure.
 *
 * Pure of the panel class so `node --test` can import it (the panel itself
 * pulls in the ~5.6MB vendored bundle).
 */

const DEMO_PAYLOAD_FILE = '../demo/demo.yml';
const DEMO_VARIABLES_FILE = '../demo/demo.json';
export const DEFAULT_TIMEOUT_MS = 10_000;

/** The demo files' URLs, resolved next to the panel module (the `?v=` of the module's own URL does not carry over, by design). */
export function hostDemoUrls(moduleUrl) {
  return {
    payload: new URL(DEMO_PAYLOAD_FILE, moduleUrl),
    variables: new URL(DEMO_VARIABLES_FILE, moduleUrl),
  };
}

// Not `AbortSignal.timeout()`: Node unrefs that timer, so under `node --test`
// (Node 20 on CI) a test whose only pending work is the timeout drains the
// event loop before the abort fires and the runner cancels it as "Promise
// resolution is still pending". A plain `setTimeout` holds the loop until
// it fires or is cleared, in Node and in the browser alike.
async function fetchText(fetchImpl, url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new DOMException(`timed out after ${timeoutMs} ms`, 'TimeoutError')),
    timeoutMs
  );
  try {
    const res = await fetchImpl(url, { signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
    return await res.text();
  } finally {
    clearTimeout(timer);
  }
}

function parseVariables(text, url) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`${url} is not JSON: ${err?.message ?? err}`);
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${url} must be a JSON object`);
  }
  if (parsed.variables === undefined) return undefined;
  const variables = parsed.variables;
  if (!variables || typeof variables !== 'object' || Array.isArray(variables)) {
    throw new Error(`${url}: "variables" must be an object`);
  }
  for (const [name, value] of Object.entries(variables)) {
    if (typeof value !== 'string') {
      throw new Error(`${url}: variable "${name}" must be a string`);
    }
  }
  return variables;
}

/**
 * Load the demo and return the mount-option fragment to spread into
 * `mount()`'s options: `{ demo: { payload, variables } }`, or `{}` after
 * one `onFallback(message)` call when either file cannot be fetched or
 * parsed within `timeoutMs`. `parsePayload(yamlText)` is the host's own
 * YAML parse, expected to throw on malformed input; `null` skips it.
 */
export async function loadHostDemoOptions(
  moduleUrl,
  {
    fetchImpl = globalThis.fetch,
    onFallback = console.warn,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    parsePayload = null,
  } = {}
) {
  const urls = hostDemoUrls(moduleUrl);
  try {
    const [payload, variablesText] = await Promise.all([
      fetchText(fetchImpl, urls.payload, timeoutMs),
      fetchText(fetchImpl, urls.variables, timeoutMs),
    ]);
    if (!payload.trim()) throw new Error(`${urls.payload} is empty`);
    if (parsePayload) {
      try {
        parsePayload(payload);
      } catch (err) {
        throw new Error(`${urls.payload} is not valid YAML: ${err?.message ?? err}`);
      }
    }
    const variables = parseVariables(variablesText, urls.variables);
    return { demo: variables === undefined ? { payload } : { payload, variables } };
  } catch (err) {
    const reason =
      err?.name === 'TimeoutError' ? `timed out after ${timeoutMs} ms` : (err?.message ?? String(err));
    onFallback(
      `Could not load the Home Assistant demo (${reason}); Load Demo will show the designer's bundled demo instead.`
    );
    return {};
  }
}

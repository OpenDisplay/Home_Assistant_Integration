/**
 * Client-side template pre-rendering with the designer's Variables.
 *
 * The designer's shared Variables (its State Simulator "Variables", literal
 * strings, `context.variables` on both `onAction` and `renderPreview`) are
 * the analogue of an automation's script-level `variables:`. Home
 * Assistant's script engine renders a service call's data with those as
 * Jinja context -- `template.render_complex(data, variables)` -- BEFORE the
 * service handler ever runs; a service called directly has no such step.
 * This module is that step for the panel (maintainer decision 2026-10-27):
 * every string leaf carrying template syntax is rendered through HA's own
 * `render_template` websocket command with the variables, and the UNTOUCHED
 * `opendisplay.drawcustom` service (and the render endpoint) then receive
 * literal values, which `render_payload_templates` (services.py) passes
 * through unchanged. Send and preview call this identically, so what the
 * preview shows is what the display gets. `tests/test_designer_demo_render.py`
 * models the same procedure with HA's own `Template.async_render(strict=True)`.
 */

/** Mirror of HA's `homeassistant.helpers.template.is_template_string`. */
export function isTemplateString(value) {
  return (
    value.includes('{') && (value.includes('{{') || value.includes('{%') || value.includes('{#'))
  );
}

/** Render timeout handed to HA (seconds): a template still rendering after this is an error. */
const RENDER_TIMEOUT_SECONDS = 5;
/** Client-side wait for ANY answer: HA's own timeout above plus transport slack. */
const REPLY_TIMEOUT_MS = 10_000;
/** Templates in flight at once -- `report_errors` bypasses HA's template cache and renders each in its own thread. */
const MAX_IN_FLIGHT = 6;

/**
 * Renderer backed by HA's `render_template` websocket command.
 *
 * `render_template` is a SUBSCRIPTION that keeps re-rendering on every
 * state change the template touches; the panel wants exactly one answer,
 * so the first `result` event resolves and the subscription is dropped at
 * once. `strict` makes an undefined variable or a bad expression an error
 * instead of a silent partial render, like the script engine;
 * `report_errors` makes HA deliver a render failure as an `error` event
 * (`{error, level}`) -- that is also how a template whose very first strict
 * render fails is reported (the tracker's `log_fn`, before `send_result`),
 * so an event may reach the callback before the subscribe promise has
 * settled. The subscribe itself is rejected (HA `send_error`) only for the
 * render timeout. A reply that never comes (dead socket) is cut off by a
 * client timer so no caller waits forever. The result is whatever HA
 * returns, natively typed (`parse_result`), like a service call's own
 * rendering.
 *
 * @param {{subscribeMessage: Function}} connection `hass.connection`.
 * @param {{replyTimeoutMs?: number}} [options]
 * @returns {(template: string, variables: object) => Promise<unknown>}
 */
export function haTemplateRenderer(connection, { replyTimeoutMs = REPLY_TIMEOUT_MS } = {}) {
  return (template, variables) =>
    new Promise((resolve, reject) => {
      let settled = false;
      let unsubscribe = null;
      const dropSubscription = () => {
        if (!unsubscribe) return;
        const unsub = unsubscribe;
        unsubscribe = null;
        // `unsubscribe` sends an unsubscribe_events message; a failure
        // there (connection already gone) is nothing the caller can act on.
        Promise.resolve()
          .then(unsub)
          .catch(() => {});
      };
      const finish = (settle, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        settle(value);
        dropSubscription();
      };
      const timer = setTimeout(
        () =>
          finish(
            reject,
            new Error(
              `Home Assistant did not answer the template render within ${replyTimeoutMs / 1000}s`
            )
          ),
        replyTimeoutMs
      );
      connection
        .subscribeMessage(
          (event) => {
            if (event && typeof event === 'object' && 'error' in event) {
              finish(reject, new Error(String(event.error)));
            } else {
              finish(resolve, event?.result);
            }
          },
          {
            type: 'render_template',
            template,
            variables,
            strict: true,
            report_errors: true,
            timeout: RENDER_TIMEOUT_SECONDS,
          }
        )
        .then(
          (unsub) => {
            unsubscribe = unsub;
            if (settled) dropSubscription();
          },
          (err) => finish(reject, err instanceof Error ? err : new Error(errorText(err)))
        );
    });
}

/** HA's websocket rejects with `{code, message}`; anything else is stringified. */
function errorText(err) {
  if (err && typeof err === 'object' && typeof err.message === 'string') return err.message;
  return String(err);
}

/**
 * Walk one value the way HA's `render_complex` does -- lists and dict
 * values recursively -- calling `onLeaf(path, template)` for every string
 * leaf carrying template syntax and returning the value rebuilt with
 * `onLeaf`'s return value in that leaf's place. Used twice per call: once
 * to collect the templates in document order, once to rebuild the payload
 * from the rendered results in that same order.
 */
function walk(value, path, onLeaf) {
  if (typeof value === 'string') return isTemplateString(value) ? onLeaf(path, value) : value;
  if (Array.isArray(value)) return value.map((item, i) => walk(item, `${path}[${i}]`, onLeaf));
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        walk(item, path ? `${path}.${key}` : key, onLeaf),
      ])
    );
  }
  return value;
}

/** Render each distinct template once, at most `maxInFlight` at a time; never throws. */
async function renderAll(templates, variables, render, maxInFlight) {
  const results = new Map();
  const queue = [...templates];
  const worker = async () => {
    while (queue.length) {
      const template = queue.shift();
      try {
        results.set(template, { value: await render(template, variables) });
      } catch (err) {
        results.set(template, { error: err });
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(maxInFlight, templates.length) }, worker));
  return results;
}

function elementType(element) {
  return element && typeof element === 'object' && !Array.isArray(element)
    ? (element.type ?? '?')
    : '?';
}

/**
 * Pre-render a parsed drawcustom payload: a new elements array in which
 * every string leaf containing template syntax is replaced by its rendered
 * value and everything else is untouched. Identical template strings are
 * rendered once per call (the variables are the same for the whole call).
 *
 * Every template is rendered before any verdict, and the FIRST failure in
 * document order (element index, then field order) is what rejects --
 * never whichever answer happened to arrive first. The `Error` names the
 * element index, its `type` and the field path (the server's own
 * `render_payload_templates` error names element and type but no field)
 * and aborts the whole send/preview: no half-rendered payload ever reaches
 * HA. A result that still contains template syntax is refused the same
 * way, because `render_payload_templates` would otherwise render that
 * rendered value a second time, silently.
 *
 * @param {unknown[]} elements Parsed payload (`parsePayload`).
 * @param {object} variables `context.variables`.
 * @param {(template: string, variables: object) => Promise<unknown>} render
 * @param {{maxInFlight?: number}} [options]
 * @returns {Promise<unknown[]>}
 */
export async function prerenderElements(
  elements,
  variables,
  render,
  { maxInFlight = MAX_IN_FLIGHT } = {}
) {
  const templates = new Set();
  for (const element of elements) {
    walk(element, '', (_path, template) => {
      templates.add(template);
    });
  }
  const results = await renderAll([...templates], variables, render, maxInFlight);
  return elements.map((element, index) =>
    walk(element, '', (path, template) => {
      const where = `Element ${index} (type '${elementType(element)}') field '${path}'`;
      const { value, error } = results.get(template);
      if (error !== undefined) {
        throw new Error(`${where} has an invalid template: ${errorText(error)}`);
      }
      if (typeof value === 'string' && isTemplateString(value)) {
        throw new Error(
          `${where} rendered to a value that still contains template syntax: ${JSON.stringify(value)}`
        );
      }
      return value;
    })
  );
}

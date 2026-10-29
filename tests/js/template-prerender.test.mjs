// Unit tests for template-prerender.js: the panel's client-side analogue of
// what Home Assistant's script engine does to an automation's service data
// before the handler runs (`template.render_complex(data, variables)`).
// The designer's shared Variables are the analogue of a script's
// `variables:`; the panel renders every templated field with them and then
// calls the untouched `opendisplay.drawcustom` service with literals.
//
// Plain `node --test`, zero dependencies -- see key-containment.test.mjs's
// own header comment for why this repo tests panel JS this way. The HA
// websocket is a fake `subscribeMessage` here, modelled on HA's real
// `handle_render_template` (websocket_api/commands.py): `render_template`
// is a SUBSCRIPTION that keeps re-rendering on state changes; a template
// whose first strict render fails is reported as an `error` EVENT (the
// tracker's `log_fn`), not as a rejected subscribe -- `send_error` only
// happens for the render timeout. Events can land before or after the
// subscribe promise settles (home-assistant-js-websocket delivers events
// to the callback as soon as the command id is known), and the tests pin
// that the first result wins and the subscription is dropped either way.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  haTemplateRenderer,
  isTemplateString,
  prerenderElements,
} from '../../custom_components/opendisplay/designer/frontend/panel/template-prerender.js';

/**
 * A fake `hass.connection` whose `render_template` answers from a lookup
 * table of template -> result (an Error = HA's `{error, level: 'ERROR'}`
 * event; `undefined` = never answers, like a dead socket), recording every
 * message it was sent and every unsubscribe. `deliver: 'before'` fires the
 * first event before the subscribe promise resolves, `'after'` on a later
 * microtask. `rejectWith` makes the subscribe itself fail (HA's
 * `send_error`, e.g. "Exceeded maximum execution time of 5s").
 */
function fakeConnection(table, { deliver = 'after', extraResults = 0, rejectWith } = {}) {
  const messages = [];
  const unsubscribed = [];
  const connection = {
    messages,
    unsubscribed,
    subscribeMessage(callback, message) {
      messages.push(message);
      const unsub = () => unsubscribed.push(message.template);
      if (rejectWith) return Promise.reject(rejectWith);
      const answer = table[message.template];
      const fire = () => {
        if (answer === undefined) return; // never answers
        if (answer instanceof Error) {
          callback({ error: answer.message, level: 'ERROR' });
          return;
        }
        callback({ result: answer, listeners: {} });
        for (let i = 0; i < extraResults; i += 1) {
          callback({ result: `${answer}-again-${i}`, listeners: {} });
        }
      };
      if (deliver === 'before') {
        fire();
        return Promise.resolve(unsub);
      }
      return Promise.resolve().then(() => {
        queueMicrotask(fire);
        return unsub;
      });
    },
  };
  return connection;
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A fake rendering: the template's text minus its braces, so the result is not itself a template. */
const rendered = (template) => `R(${template.replace(/[{}]/g, '')})`;

test('isTemplateString mirrors HA is_template_string: {{, {% or {# anywhere', () => {
  assert.equal(isTemplateString('{{ alert }}'), true);
  assert.equal(isTemplateString('x {% if a %}y{% endif %}'), true);
  assert.equal(isTemplateString('{# comment #}'), true);
  assert.equal(isTemplateString('plain'), false);
  assert.equal(isTemplateString('{ not: template }'), false);
  assert.equal(isTemplateString(''), false);
});

test('every templated string leaf is rendered, nested dicts and lists walked, the rest untouched', async () => {
  const seen = [];
  const render = async (template, variables) => {
    seen.push([template, variables]);
    return rendered(template);
  };
  const elements = [
    {
      type: 'circle',
      fill: "{{ 'red' if alert else 'white' }}",
      x: 10,
      visible: true,
      label: 'static',
      nested: { color: '{{ accent_color }}', depth: { items: ['a', '{{ b }}', 3] } },
    },
    'bare {{ string }}',
  ];
  const variables = { alert: 'true', accent_color: 'red', b: '1' };

  const out = await prerenderElements(elements, variables, render);

  assert.deepEqual(out, [
    {
      type: 'circle',
      fill: "R( 'red' if alert else 'white' )",
      x: 10,
      visible: true,
      label: 'static',
      nested: { color: 'R( accent_color )', depth: { items: ['a', 'R( b )', 3] } },
    },
    'R(bare  string )',
  ]);
  // Only template strings reached the renderer, each with the variables.
  assert.deepEqual(
    seen.map(([t]) => t).sort(),
    ["{{ 'red' if alert else 'white' }}", '{{ accent_color }}', '{{ b }}', 'bare {{ string }}'].sort()
  );
  for (const [, vars] of seen) assert.equal(vars, variables);
  // Input is never mutated: the designer hands over its own parsed payload.
  assert.equal(elements[0].fill, "{{ 'red' if alert else 'white' }}");
  assert.equal(elements[0].nested.depth.items[1], '{{ b }}');
});

test('a payload with no templates comes back equal, with no renderer call at all', async () => {
  let calls = 0;
  const elements = [{ type: 'text', value: 'hi', size: 12, visible: true }];
  const out = await prerenderElements(elements, {}, async () => {
    calls += 1;
    return 'never';
  });
  assert.deepEqual(out, elements);
  assert.equal(calls, 0);
});

test('identical template strings render ONCE per call (one subscription), every leaf gets the value', async () => {
  // `report_errors: true` makes HA skip its template cache and spawn a
  // render per subscription, so the panel dedupes on its side.
  const connection = fakeConnection({ '{{ accent_color }}': 'red' });
  const elements = [
    { type: 'icon', fill: '{{ accent_color }}' },
    { type: 'text', color: '{{ accent_color }}', outline: '{{ accent_color }}' },
  ];
  const out = await prerenderElements(elements, { accent_color: 'red' }, haTemplateRenderer(connection));
  assert.equal(connection.messages.length, 1);
  assert.deepEqual(out, [
    { type: 'icon', fill: 'red' },
    { type: 'text', color: 'red', outline: 'red' },
  ]);
});

test('at most maxInFlight templates render at once', async () => {
  let inFlight = 0;
  let peak = 0;
  const render = (template) =>
    new Promise((resolve) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      setTimeout(() => {
        inFlight -= 1;
        resolve(rendered(template));
      }, 1);
    });
  const elements = Array.from({ length: 10 }, (_, i) => ({ type: 'text', value: `{{ v${i} }}` }));

  const out = await prerenderElements(elements, {}, render, { maxInFlight: 3 });

  assert.equal(peak, 3);
  assert.equal(out[9].value, 'R( v9 )');
});

test('a renderer failure surfaces as an Error naming element index, type and field path', async () => {
  const elements = [
    { type: 'text', value: 'fine' },
    { type: 'progress_bar', progress: "{{ now().strftime('%S') / 60 }}", style: { color: '{{ ok }}' } },
  ];
  const render = async (template) => {
    if (template.includes('strftime')) {
      throw new Error("TypeError: unsupported operand type(s) for /: 'str' and 'int'");
    }
    return 'red';
  };
  await assert.rejects(prerenderElements(elements, {}, render), (err) => {
    assert.ok(err instanceof Error);
    assert.equal(
      err.message,
      "Element 1 (type 'progress_bar') field 'progress' has an invalid template:" +
        " TypeError: unsupported operand type(s) for /: 'str' and 'int'"
    );
    return true;
  });
});

test('a nested field failure names the full path into the element', async () => {
  const elements = [{ type: 'icon', style: { fills: ['{{ a }}', '{{ broken }}'] } }];
  const render = async (template) => {
    if (template === '{{ broken }}') throw new Error("UndefinedError: 'broken' is undefined");
    return 'x';
  };
  await assert.rejects(prerenderElements(elements, {}, render), {
    message:
      "Element 0 (type 'icon') field 'style.fills[1]' has an invalid template:" +
      " UndefinedError: 'broken' is undefined",
  });
});

test('with several failures the FIRST in traversal order is reported, whatever answers first', async () => {
  // Leaves render in parallel; the last one to be asked answers (fails)
  // first. The user still gets element 0's field, not element 2's.
  const pending = [];
  const render = (template) =>
    new Promise((_resolve, reject) => {
      pending.push(() => reject(new Error(`bad: ${template}`)));
    });
  const elements = [
    { type: 'text', color: '{{ first }}' },
    { type: 'text', value: 'plain' },
    { type: 'icon', fill: '{{ second }}', outline: '{{ third }}' },
  ];
  const run = prerenderElements(elements, {}, render);
  await tick();
  assert.equal(pending.length, 3);
  for (const fail of pending.reverse()) fail();
  await assert.rejects(run, {
    message: "Element 0 (type 'text') field 'color' has an invalid template: bad: {{ first }}",
  });
});

test('a result that still contains template syntax is refused, naming element and field', async () => {
  // services.py's render_payload_templates renders ANY string that still
  // looks like a template; the panel refuses rather than letting HA render
  // a rendered value a second time, silently.
  const render = async (template) => (template === '{{ raw }}' ? '{{ states.sensor.x.state }}' : 'ok');
  const elements = [{ type: 'text', value: '{{ fine }}', label: { text: '{{ raw }}' } }];
  await assert.rejects(prerenderElements(elements, {}, render), {
    message:
      "Element 0 (type 'text') field 'label.text' rendered to a value that still contains" +
      ' template syntax: "{{ states.sensor.x.state }}"',
  });
});

test('haTemplateRenderer sends a strict, error-reporting render_template with the variables', async () => {
  const connection = fakeConnection({ '{{ alert }}': 'true' });
  const render = haTemplateRenderer(connection);
  const variables = { alert: 'true' };

  const result = await render('{{ alert }}', variables);

  assert.equal(result, 'true');
  assert.equal(connection.messages.length, 1);
  const message = connection.messages[0];
  assert.equal(message.type, 'render_template');
  assert.equal(message.template, '{{ alert }}');
  assert.equal(message.variables, variables);
  assert.equal(message.strict, true, 'strict: undefined variable = error, like the script engine');
  assert.equal(message.report_errors, true, 'report_errors: failures arrive as error events');
  assert.ok(Number.isFinite(message.timeout) && message.timeout > 0, 'a render timeout is set');
});

test('haTemplateRenderer forwards typed variable values (boolean, number, null, list, object) untouched', async () => {
  // Designer >= 3.7.0 reports variables as JSON natives, coerced as its own
  // preview injects them; `false` must stay `false` on the wire (the string
  // "false" is truthy in Jinja).
  const connection = fakeConnection({ '{{ alert }}': false });
  const variables = { alert: false, n: 5, nothing: null, list: [1, 2], obj: { a: 1 }, unset: '' };

  const result = await haTemplateRenderer(connection)('{{ alert }}', variables);

  assert.equal(result, false);
  assert.equal(connection.messages[0].variables, variables);
  assert.deepEqual(connection.messages[0].variables, {
    alert: false,
    n: 5,
    nothing: null,
    list: [1, 2],
    obj: { a: 1 },
    unset: '',
  });
});

test('haTemplateRenderer keeps the native-typed result HA returns', async () => {
  const connection = fakeConnection({ '{{ 1 + 1 }}': 2, '{{ false }}': false, '{{ [1,2] }}': [1, 2] });
  const render = haTemplateRenderer(connection);
  assert.equal(await render('{{ 1 + 1 }}', {}), 2);
  assert.equal(await render('{{ false }}', {}), false);
  assert.deepEqual(await render('{{ [1,2] }}', {}), [1, 2]);
});

for (const deliver of ['before', 'after']) {
  test(`haTemplateRenderer resolves with the FIRST result and unsubscribes (event ${deliver} subscribe settles)`, async () => {
    const connection = fakeConnection({ '{{ x }}': 'first' }, { deliver, extraResults: 2 });
    const render = haTemplateRenderer(connection);

    const result = await render('{{ x }}', {});
    await tick();

    assert.equal(result, 'first');
    assert.deepEqual(connection.unsubscribed, ['{{ x }}'], 'the subscription is dropped exactly once');
  });

  test(`haTemplateRenderer rejects on an error event -- HA's shape for a strict first-render failure (event ${deliver} subscribe settles)`, async () => {
    const connection = fakeConnection(
      { '{{ nope }}': new Error("UndefinedError: 'nope' is undefined") },
      { deliver }
    );
    const render = haTemplateRenderer(connection);
    await assert.rejects(render('{{ nope }}', {}), { message: "UndefinedError: 'nope' is undefined" });
    await tick();
    assert.deepEqual(connection.unsubscribed, ['{{ nope }}']);
  });
}

test('haTemplateRenderer rejects when the subscribe itself fails (HA send_error: render timeout)', async () => {
  const connection = fakeConnection({}, { rejectWith: { code: 'template_error', message: 'Exceeded maximum execution time of 5s' } });
  const render = haTemplateRenderer(connection);
  await assert.rejects(render('{{ slow }}', {}), { message: 'Exceeded maximum execution time of 5s' });
  assert.deepEqual(connection.unsubscribed, []);
});

test('haTemplateRenderer gives up on a reply that never comes, with a clear message, and unsubscribes', async () => {
  // A dead socket must not leave the send guard stuck or a preview
  // promise unsettled forever.
  const connection = fakeConnection({ '{{ silent }}': undefined });
  const render = haTemplateRenderer(connection, { replyTimeoutMs: 20 });
  await assert.rejects(render('{{ silent }}', {}), {
    message: 'Home Assistant did not answer the template render within 0.02s',
  });
  await tick();
  assert.deepEqual(connection.unsubscribed, ['{{ silent }}']);
});

test('end to end: the Load Demo circle takes its truthy branch with alert = "true"', async () => {
  // The bug this module exists for: on a real display the demo's circle
  // rendered white because the designer's variables never reached HA.
  const connection = fakeConnection({ "{{ 'red' if alert else 'white' }}": 'red' });
  const out = await prerenderElements(
    [{ type: 'circle', fill: "{{ 'red' if alert else 'white' }}", x: 1, y: 1, radius: 5 }],
    { alert: 'true' },
    haTemplateRenderer(connection)
  );
  assert.equal(out[0].fill, 'red');
  assert.deepEqual(connection.messages[0].variables, { alert: 'true' });
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readEvents, RequestScope } = require('../app/web/chat.js');
const vm = require('node:vm');
const fs = require('node:fs');

function page(fetch) {
  class Node {
    constructor() {
      this.children = []; this.listeners = {}; this.value = ''; this.textContent = '';
      this.classList = { toggle() {} }; this.hidden = false;
    }
    append(...nodes) { this.children.push(...nodes); }
    replaceChildren(...nodes) { this.children = nodes; }
    setAttribute() {}
    addEventListener(name, handler) { this.listeners[name] = handler; }
    querySelectorAll() { return this.children; }
    focus() {}
  }
  const nodes = {};
  const listeners = {};
  const document = { cookie: 'business_csrf=csrf',
    getElementById: (id) => nodes[id] ||= new Node(),
    createElement: () => new Node(),
    addEventListener: (name, handler) => { listeners[name] = handler; },
  };
  const window = { BusinessAuth: { user: { id: 'alice' }, handleUnauthorized() {} } };
  vm.runInNewContext(fs.readFileSync(require.resolve('../app/web/chat.js'), 'utf8'),
    { document, window, fetch, AbortController, DOMException, Headers, TextDecoder });
  listeners.DOMContentLoaded();
  return { nodes, listeners, window };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const jsonResponse = (data) => ({ ok: true, status: 200, json: async () => data });

function response(parts) {
  return { body: new ReadableStream({ start(controller) {
    for (const part of parts) controller.enqueue(new TextEncoder().encode(part));
    controller.close();
  } }) };
}

test('SSE handles fragmented frames, CRLF and multiple events', async () => {
  const events = [];
  await readEvents(response(['event: delta\r', '\ndata: {"text":"订',
    '单"}\r\n\r', '\nevent: done\ndata: {}\n\n']), (kind, data) => events.push([kind, data]));
  assert.deepEqual(events, [['delta', { text: '订单' }], ['done', {}]]);
});

test('EOF without done cannot pretend the answer completed', async () => {
  await assert.rejects(readEvents(response(['event: delta\ndata: {"text":"一半"}\n\n']),
    () => {}), /中断/);
});

test('an error event never becomes a completed answer', async () => {
  await assert.rejects(readEvents(response(['event: error\ndata: {"message":"模型不可用"}\n\n']),
    () => {}), /模型不可用/);
});

test('account or conversation switch invalidates old responses and aborts their request', () => {
  const scope = new RequestScope();
  const previous = scope.capture();
  scope.reset();
  assert.equal(previous.signal.aborted, true);
  assert.equal(scope.current(previous), false);
  assert.equal(scope.current(scope.capture()), true);
});

test('actual page ignores previous account conversation list after a new login', async () => {
  let resolveOld;
  let count = 0;
  const ui = page(() => ++count === 1 ? new Promise((resolve) => { resolveOld = resolve; }) :
    Promise.resolve(jsonResponse({ items: [{ id: 'b', title: 'B 会话' }], next_offset: null })));
  ui.listeners['business:ready']();
  ui.listeners['auth:logged-out']();
  ui.window.BusinessAuth.user = { id: 'bob' };
  ui.listeners['business:ready']();
  await tick();
  resolveOld(jsonResponse({ items: [{ id: 'a', title: 'A 私密会话' }], next_offset: null }));
  await tick();
  assert.deepEqual(ui.nodes['chat-conversations'].children.map((n) => n.textContent), ['B 会话']);
});

test('account switch during automatic conversation creation cannot send old text as new user', async () => {
  let resolveCreate;
  const calls = [];
  const ui = page((url, options = {}) => {
    calls.push([url, options.method]);
    if (options.method === 'POST') return new Promise((resolve) => { resolveCreate = resolve; });
    return Promise.resolve(jsonResponse({ items: [], next_offset: null }));
  });
  ui.listeners['business:ready']();
  await tick();
  ui.nodes['chat-input'].value = 'A 的私密问题';
  const pending = ui.nodes['chat-form'].listeners.submit({ preventDefault() {} });
  ui.listeners['auth:logged-out']();
  ui.window.BusinessAuth.user = { id: 'bob' };
  ui.listeners['business:ready']();
  resolveCreate(jsonResponse({ id: 'old-conversation', title: 'A' }));
  await pending;
  assert.equal(calls.filter(([url, method]) => method === 'POST' && url.endsWith('/messages')).length, 0);
  assert.equal(ui.nodes['chat-input'].value, '');
});

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readEvents, RequestScope } = require('../app/web/chat.js');
const vm = require('node:vm');
const fs = require('node:fs');

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.last = this;
  }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  close() { this.closed = true; }
  emit(name, data, id) {
    for (const handler of this.listeners[name] || []) {
      handler({ data: JSON.stringify(data), lastEventId: id === undefined ? '' : String(id) });
    }
  }
}

function page(fetch) {
  class Node {
    constructor() {
      this.children = []; this.listeners = {}; this.value = ''; this.textContent = '';
      this.classList = { toggle() {} }; this.hidden = false; this.disabled = false;
      this.parentNode = null; this.scrollTop = 0; this.scrollHeight = 0;
    }
    append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
    replaceChildren(...nodes) { this.children = nodes; for (const node of nodes) node.parentNode = this; }
    setAttribute() {}
    addEventListener(name, handler) { this.listeners[name] = handler; }
    querySelectorAll() { return this.children; }
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((n) => n !== this); }
    focus() {}
  }
  const nodes = {};
  const listeners = {};
  const document = {
    cookie: 'business_csrf=csrf',
    getElementById: (id) => (nodes[id] ||= new Node()),
    createElement: () => new Node(),
    addEventListener: (name, handler) => { listeners[name] = handler; },
  };
  const window = {
    BusinessAuth: { user: { id: 'alice' }, handleUnauthorized() {} },
    addEventListener() {},
    setTimeout,
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../app/web/chat.js'), 'utf8'), {
    document, window, fetch, EventSource: FakeEventSource, crypto: { randomUUID: () => 'key-1' },
    AbortController, DOMException, Headers, TextDecoder, setTimeout,
  });
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

test('POST 202 subscribes to the run events URL and terminal events unlock input', async () => {
  const ui = page((url, options = {}) => {
    if (url.endsWith('/conversations') && options.method === 'POST') {
      return Promise.resolve(jsonResponse({ id: 'c1', title: '会话' }));
    }
    if (url.endsWith('/messages') && options.method === 'POST') {
      return Promise.resolve({
        ok: true, status: 202,
        json: async () => ({
          conversation_id: 'c1', turn_id: 't1', run_id: 'r1', status: 'queued',
          events_url: '/api/v1/chat/runs/r1/events', last_event_seq: 0,
        }),
      });
    }
    if (url.includes('/turns?limit=1')) {
      return Promise.resolve(jsonResponse({
        items: [{
          turn_id: 't1',
          status: 'completed',
          latest_run: {
            run_id: 'r1', status: 'completed', attempt_no: 1, last_event_seq: 4,
            events_url: '/api/v1/chat/runs/r1/events',
          },
        }],
      }));
    }
    if (url.includes('/messages')) {
      return Promise.resolve(jsonResponse({
        conversation: { id: 'c1', title: '会话', busy: false }, items: [], next_before: null,
      }));
    }
    return Promise.resolve(jsonResponse({ items: [], next_offset: null }));
  });

  ui.listeners['business:ready']();
  await tick();
  ui.nodes['chat-input'].value = '查订单';
  await ui.nodes['chat-form'].listeners.submit({ preventDefault() {} });
  await tick();

  const source = FakeEventSource.last;
  assert.equal(source.url, '/api/v1/chat/runs/r1/events');
  assert.equal(source.closed, false);

  source.emit('run.started', { run_id: 'r1', status: 'running' }, 2);
  source.emit('text.delta', { delta: '正在查询' }, 3);
  assert.match(ui.nodes['chat-messages'].children.at(-1).textContent, /正在查询/);

  source.emit('run.completed', { run_id: 'r1', status: 'completed' }, 4);
  await tick();
  assert.equal(source.closed, true);
  assert.match(ui.nodes['chat-status'].textContent, /已完成|回复已保存/);
  assert.equal(ui.nodes['chat-input'].disabled, false);
});

test('stop button posts cancel for the active run', async () => {
  const calls = [];
  const ui = page((url, options = {}) => {
    calls.push([url, options.method || 'GET']);
    if (url.endsWith('/conversations') && options.method === 'POST') {
      return Promise.resolve(jsonResponse({ id: 'c1', title: '会话' }));
    }
    if (url.endsWith('/messages') && options.method === 'POST') {
      return Promise.resolve({
        ok: true, status: 202,
        json: async () => ({
          conversation_id: 'c1', turn_id: 't1', run_id: 'r1', status: 'queued',
          events_url: '/api/v1/chat/runs/r1/events', last_event_seq: 0,
        }),
      });
    }
    if (url.includes('/runs/r1/cancel')) {
      return Promise.resolve({ ok: true, status: 202, json: async () => ({ status: 'cancelling' }) });
    }
    if (url.includes('/turns?limit=1')) return Promise.resolve(jsonResponse({ items: [] }));
    if (url.includes('/messages')) {
      return Promise.resolve(jsonResponse({
        conversation: { id: 'c1', title: '会话', busy: true }, items: [], next_before: null,
      }));
    }
    return Promise.resolve(jsonResponse({ items: [], next_offset: null }));
  });
  ui.listeners['business:ready']();
  await tick();
  ui.nodes['chat-input'].value = '查订单';
  await ui.nodes['chat-form'].listeners.submit({ preventDefault() {} });
  await tick();
  assert.equal(ui.nodes['chat-stop'].hidden, false);
  await ui.nodes['chat-stop'].listeners.click();
  assert.ok(calls.some(([url, method]) => url.includes('/runs/r1/cancel') && method === 'POST'));
});

test('text.reset removes the partial segment instead of concatenating it', async () => {
  const ui = page((url, options = {}) => {
    if (url.endsWith('/conversations') && options.method === 'POST') {
      return Promise.resolve(jsonResponse({ id: 'c1', title: '会话' }));
    }
    if (url.endsWith('/messages') && options.method === 'POST') {
      return Promise.resolve({
        ok: true, status: 202,
        json: async () => ({
          conversation_id: 'c1', turn_id: 't1', run_id: 'r1', status: 'queued',
          events_url: '/api/v1/chat/runs/r1/events', last_event_seq: 0,
        }),
      });
    }
    if (url.includes('/turns?limit=1')) return Promise.resolve(jsonResponse({ items: [] }));
    if (url.includes('/messages')) {
      return Promise.resolve(jsonResponse({
        conversation: { id: 'c1', title: '会话', busy: true }, items: [], next_before: null,
      }));
    }
    return Promise.resolve(jsonResponse({ items: [], next_offset: null }));
  });
  ui.listeners['business:ready']();
  await tick();
  ui.nodes['chat-input'].value = '查订单';
  await ui.nodes['chat-form'].listeners.submit({ preventDefault() {} });
  await tick();

  const source = FakeEventSource.last;
  source.emit('text.delta', { delta: '半截错误文本' }, 2);
  assert.match(ui.nodes['chat-messages'].children.at(-1).textContent, /半截错误文本/);

  source.emit('response.restarted', { response_segment_id: 's2' }, 3);
  source.emit('text.reset', { response_segment_id: 's2' }, 4);
  source.emit('text.delta', { delta: '完整正确文本' }, 5);
  const rendered = ui.nodes['chat-messages'].children.at(-1).textContent;
  assert.equal(rendered, '完整正确文本');
  assert.doesNotMatch(rendered, /半截/);
});

test('reloading an active run resumes EventSource from its last event seq', async () => {
  const ui = page((url) => {
    if (url.includes('/conversations?offset=')) {
      return Promise.resolve(jsonResponse({
        items: [{ id: 'c1', title: '会话', busy: true }], next_offset: null,
      }));
    }
    if (url.endsWith('/messages')) {
      return Promise.resolve(jsonResponse({
        conversation: { id: 'c1', title: '会话', busy: true }, items: [], next_before: null,
      }));
    }
    if (url.includes('/turns?limit=1')) {
      return Promise.resolve(jsonResponse({
        items: [{
          turn_id: 't1',
          status: 'active',
          latest_run: {
            run_id: 'r1', turn_id: 't1', status: 'running', attempt_no: 1,
            last_event_seq: 5, events_url: '/api/v1/chat/runs/r1/events',
          },
        }],
      }));
    }
    return Promise.resolve(jsonResponse({}));
  });
  ui.listeners['business:ready']();
  await tick();
  const conversation = ui.nodes['chat-conversations'].children[0];
  await conversation.listeners.click();
  await tick();
  assert.equal(
    FakeEventSource.last.url,
    '/api/v1/chat/runs/r1/events?after=5',
  );
});
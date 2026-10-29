/* Cookie-authenticated chat. No client-supplied identity or conversation history. */
(() => {
  class RequestScope {
    constructor() { this.controller = new AbortController(); }
    capture() { return this.controller; }
    current(token) { return token === this.controller && !token.signal.aborted; }
    reset() { this.controller.abort(); this.controller = new AbortController(); }
  }

  async function readEvents(response, onEvent) {
    if (!response.body) throw new Error('浏览器不支持流式读取');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let completed = false;
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          const lines = frame.split(/\r?\n/);
          const kind = lines.find((line) => line.startsWith('event:'))?.slice(6).trim();
          const content = lines.filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trimStart()).join('\n');
          if (!kind || !content) continue;
          const data = JSON.parse(content);
          onEvent(kind, data);
          if (kind === 'error') throw new Error(data.message || '本轮回复失败');
          if (kind === 'done') completed = true;
        }
        if (done) break;
      }
      if (!completed) throw new Error('连接中断，本轮未确认完成；请刷新历史查看已保存的消息。');
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { RequestScope, readEvents };
    return;
  }

  const $ = (id) => document.getElementById(id);
  const scope = new RequestScope();
  let selected = null;
  let records = [];
  let conversations = [];
  let before = null;
  let nextOffset = null;
  let sending = false;
  let busy = false;
  let provisional = null;

  function status(text, error = false) {
    $('chat-status').textContent = text;
    $('chat-status').classList.toggle('error', error);
  }

  function controls() {
    $('chat-send').disabled = sending || busy;
    $('chat-input').disabled = sending || busy;
    $('chat-new').disabled = sending;
    $('chat-older').hidden = before === null;
    $('chat-older').disabled = sending;
    $('chat-more').hidden = nextOffset === null;
    $('chat-more').disabled = sending;
    $('chat-refresh').disabled = sending;
    $('chat-conversations').querySelectorAll('button').forEach((b) => { b.disabled = sending; });
  }

  async function request(url, options = {}, token = scope.capture()) {
    const headers = new Headers(options.headers || {});
    if (options.method === 'POST') {
      const csrf = document.cookie.split(';').map((part) => part.trim())
        .find((part) => part.startsWith('business_csrf='))?.slice(14) || '';
      headers.set('X-CSRF-Token', csrf);
      headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(url, { ...options, headers, credentials: 'same-origin',
      signal: token.signal });
    // Check before even reacting to 401: a previous account must not log out the new one.
    if (!scope.current(token)) throw new DOMException('Stale response', 'AbortError');
    if (response.status === 401) window.BusinessAuth.handleUnauthorized();
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      if (!scope.current(token)) throw new DOMException('Stale response', 'AbortError');
      throw new Error(body?.detail?.message || `请求失败（${response.status}）`);
    }
    return response;
  }

  function renderMessages() {
    const root = $('chat-messages');
    root.replaceChildren();
    if (!records.length) {
      const empty = document.createElement('p');
      empty.className = 'chat-empty';
      empty.textContent = '可以试着问：“查一下我的订单”。回复会使用当前账号可访问的业务数据。';
      root.append(empty);
    }
    for (const row of records) {
      const tool = row.role === 'tool' || row.tool_calls?.length;
      const node = document.createElement(tool ? 'details' : 'article');
      node.className = `chat-message ${row.role}`;
      const label = document.createElement(tool ? 'summary' : 'strong');
      label.textContent = tool ? (row.role === 'tool' ? `工具结果 · ${row.tool_name}` :
        `调用工具 · ${row.tool_calls.map((call) => call.name).join('、')}`) :
        row.role === 'user' ? '你' : '售后助手';
      node.append(label);
      const content = document.createElement(tool ? 'pre' : 'div');
      content.textContent = row.tool_calls?.length ? JSON.stringify(row.tool_calls, null, 2) : row.content;
      node.append(content);
      if (row.status !== 'complete') {
        const badge = document.createElement('small');
        badge.textContent = row.status === 'failed' ? '本轮未完成' : '本轮进行中 / 尚未确认完成';
        node.append(badge);
      }
      root.append(node);
    }
  }

  function renderConversations() {
    const root = $('chat-conversations');
    root.replaceChildren();
    for (const conv of conversations) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'conversation';
      button.classList.toggle('active', conv.id === selected);
      button.setAttribute('aria-current', conv.id === selected ? 'true' : 'false');
      button.textContent = conv.title;
      button.title = conv.title;
      button.addEventListener('click', () => openConversation(conv.id).catch(showError));
      root.append(button);
    }
    controls();
  }

  function showError(error) {
    if (error.name !== 'AbortError') status(error.message || '网络请求失败', true);
  }

  async function loadConversations(append = false) {
    const token = scope.capture();
    const offset = append ? nextOffset : 0;
    const response = await request(`/api/v1/chat/conversations?offset=${offset || 0}`, {}, token);
    const data = await response.json();
    if (!scope.current(token)) return;
    conversations = append ? [...conversations, ...data.items] : data.items;
    nextOffset = data.next_offset;
    renderConversations();
  }

  async function loadMessages(older = false) {
    if (!selected) return;
    const token = scope.capture();
    const url = `/api/v1/chat/conversations/${encodeURIComponent(selected)}/messages`;
    const response = await request(url + (older ? `?before=${before}` : ''), {}, token);
    const data = await response.json();
    if (!scope.current(token)) return;
    records = older ? [...data.items, ...records] : data.items;
    before = data.next_before;
    busy = data.conversation.busy;
    $('chat-title').textContent = data.conversation.title;
    status(busy ? '会话有未完成的执行，可刷新查看状态或新建会话。' : '已读取保存的消息');
    renderMessages();
    controls();
  }

  async function openConversation(id) {
    scope.reset();
    selected = id;
    records = [];
    before = null;
    busy = false;
    renderMessages();
    renderConversations();
    status('正在读取会话…');
    await loadMessages();
  }

  async function newConversation() {
    const token = scope.capture();
    const response = await request('/api/v1/chat/conversations', { method: 'POST' }, token);
    const conv = await response.json();
    if (!scope.current(token)) return;
    await openConversation(conv.id);
    await loadConversations();
    $('chat-input').focus();
  }

  async function send(event) {
    event.preventDefault();
    const content = $('chat-input').value.trim();
    if (!content || sending || busy) return;
    const initialToken = scope.capture();
    sending = true;
    controls();
    let token = initialToken;
    try {
      if (!selected) {
        const response = await request('/api/v1/chat/conversations', { method: 'POST' }, token);
        const conv = await response.json();
        if (!scope.current(token)) return;
        selected = conv.id;
        conversations.unshift(conv);
        renderConversations();
      }
      if (!scope.current(token)) return;
      $('chat-input').value = '';
      records.push({ role: 'user', content, status: 'pending' });
      renderMessages();
      status('正在生成回复…');
      const response = await request(`/api/v1/chat/conversations/${encodeURIComponent(selected)}/messages`,
        { method: 'POST', body: JSON.stringify({ content }) }, token);
      await readEvents(response, (kind, data) => {
        if (!scope.current(token)) return;
        if (kind === 'delta') {
          if (!provisional) {
            provisional = document.createElement('article');
            provisional.className = 'chat-message assistant streaming';
            provisional.setAttribute('aria-label', '助手正在回复');
            $('chat-messages').append(provisional);
          }
          provisional.textContent += data.text;
        } else if (kind === 'message') {
          provisional = null;
          records.push(data);
          renderMessages();
          status(data.role === 'tool' ? '已收到工具结果，正在继续回答…' : '正在处理…');
        } else if (kind === 'done') {
          records.forEach((row) => { if (row.status === 'pending') row.status = 'complete'; });
          renderMessages();
          status('回复已保存');
        }
        const root = $('chat-messages');
        root.scrollTop = root.scrollHeight;
      });
      if (scope.current(token)) {
        await loadMessages();
        await loadConversations();
        if (scope.current(token)) status('回复已保存');
      }
    } catch (error) {
      if (scope.current(token)) {
        records.forEach((r) => { if (r.status === 'pending') r.status = 'failed'; });
        renderMessages();
        showError(error);
      }
    } finally {
      if (scope.current(token)) {
        sending = false;
        provisional = null;
        controls();
      }
    }
  }

  function reset() {
    scope.reset();
    selected = null; records = []; conversations = []; before = null; nextOffset = null;
    sending = false; busy = false; provisional = null;
    $('chat-input').value = '';
    $('chat-title').textContent = '售后助手';
    status('新建或选择会话开始');
    renderMessages(); renderConversations();
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('chat-form').addEventListener('submit', send);
    $('chat-new').addEventListener('click', () => newConversation().catch(showError));
    $('chat-older').addEventListener('click', () => loadMessages(true).catch(showError));
    $('chat-more').addEventListener('click', () => loadConversations(true).catch(showError));
    $('chat-refresh').addEventListener('click', () => loadMessages().catch(showError));
    reset();
  });
  document.addEventListener('business:ready', () => { reset(); loadConversations().catch(showError); });
  document.addEventListener('auth:logged-out', reset);
  document.addEventListener('auth:unauthorized', reset);
})();

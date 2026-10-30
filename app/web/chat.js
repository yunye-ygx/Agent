/* Cookie-authenticated chat. POST creates a durable Run; EventSource only observes. */
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

  const EVENT_TYPES = [
    'run.queued', 'run.started', 'run.recovering', 'run.cancelling',
    'run.completed', 'run.failed', 'run.cancelled', 'run.requires_attention',
    'response.started', 'response.restarted', 'response.completed',
    'text.delta', 'text.reset',
    'tool.started', 'tool.completed', 'tool.failed', 'tool.requires_attention',
  ];
  const ACTIVE = new Set(['queued', 'running', 'recovering', 'cancelling']);
  const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'requires_attention']);

  let selected = null;
  let records = [];
  let conversations = [];
  let before = null;
  let nextOffset = null;
  let sending = false;
  let busy = false;
  let provisional = null;
  let activeRun = null;
  let lastTurn = null;
  let source = null;
  let lastSeq = 0;
  let pendingKey = null;
  let pendingContent = null;

  function status(text, error = false) {
    $('chat-status').textContent = text;
    $('chat-status').classList.toggle('error', error);
  }

  function isActive(run) {
    return Boolean(run && ACTIVE.has(run.status));
  }

  function canRetry() {
    return Boolean(
      lastTurn && lastTurn.latest_run && TERMINAL.has(lastTurn.latest_run.status) && !busy,
    );
  }

  function controls() {
    const locked = sending || busy;
    $('chat-send').disabled = locked;
    $('chat-input').disabled = locked;
    $('chat-new').disabled = sending;
    $('chat-older').hidden = before === null;
    $('chat-older').disabled = locked;
    $('chat-more').hidden = nextOffset === null;
    $('chat-more').disabled = locked;
    $('chat-refresh').disabled = locked;
    $('chat-stop').hidden = !isActive(activeRun);
    $('chat-stop').disabled = !isActive(activeRun);
    $('chat-retry').hidden = !canRetry();
    $('chat-retry').disabled = !canRetry();
    $('chat-retry').textContent = lastTurn?.latest_run?.status === 'completed'
      ? '重新生成' : '重试';
    $('chat-conversations').querySelectorAll('button').forEach((b) => { b.disabled = locked; });
  }

  function newKey() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
      return globalThis.crypto.randomUUID();
    }
    return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
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
      if (row.status && row.status !== 'complete') {
        const badge = document.createElement('small');
        badge.textContent = row.status === 'failed' ? '本轮未完成' : '发送中 / 尚未确认';
        node.append(badge);
      }
      root.append(node);
    }
    scrollToBottom();
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

  function scrollToBottom() {
    const root = $('chat-messages');
    root.scrollTop = root.scrollHeight;
  }

  function showError(error) {
    if (error.name !== 'AbortError') status(error.message || '网络请求失败', true);
  }

  function clearProvisional() {
    if (provisional && provisional.parentNode) provisional.remove();
    provisional = null;
  }

  function ensureProvisional() {
    if (!provisional) {
      provisional = document.createElement('article');
      provisional.className = 'chat-message assistant streaming';
      provisional.setAttribute('aria-label', '助手正在回复');
      $('chat-messages').append(provisional);
    }
    return provisional;
  }

  function closeSource() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function setRun(run) {
    activeRun = run;
    busy = isActive(run);
    controls();
  }

  function subscribe(run, after = 0) {
    closeSource();
    if (typeof EventSource === 'undefined') {
      status('当前浏览器不支持事件流', true);
      return;
    }
    setRun(run);
    lastSeq = after || 0;
    const url = run.events_url + (lastSeq ? `?after=${lastSeq}` : '');
    const es = new EventSource(url, { withCredentials: true });
    source = es;
    es.onopen = () => { if (source === es) status('已连接，正在接收执行事件…'); };
    es.onerror = () => {
      if (source !== es) return;
      status('观察连接中断，正在自动重连；执行不会因此停止…', true);
      window.setTimeout(() => checkRun(es), 2500);
    };
    for (const type of EVENT_TYPES) {
      es.addEventListener(type, (message) => {
        if (source !== es) return;
        let data = {};
        try { data = JSON.parse(message.data); } catch { data = {}; }
        if (message.lastEventId) lastSeq = Number(message.lastEventId) || lastSeq;
        handleEvent(type, data);
      });
    }
  }

  async function checkRun(expectedSource) {
    if (!activeRun || source !== expectedSource) return;
    const token = scope.capture();
    try {
      const response = await request(
        `/api/v1/chat/runs/${encodeURIComponent(activeRun.run_id)}`, {}, token,
      );
      const run = await response.json();
      if (!scope.current(token) || source !== expectedSource) return;
      if (!isActive(run)) finishRun(`run.${run.status}`, run);
    } catch (error) {
      showError(error);
    }
  }

  function handleEvent(type, data) {
    if (type === 'text.delta') {
      ensureProvisional().textContent += data.delta || '';
      scrollToBottom();
      return;
    }
    if (type === 'text.reset') {
      clearProvisional();
      return;
    }
    if (type === 'response.completed') {
      if (data.content) {
        ensureProvisional().textContent = data.content;
      } else {
        clearProvisional();
      }
      status(data.tool_calls ? '模型准备调用工具…' : '正在生成回答…');
      return;
    }
    if (type === 'response.started') {
      clearProvisional();
      status('正在生成回答…');
      return;
    }
    if (type === 'response.restarted') {
      clearProvisional();
      status('执行已恢复，重新生成回答…');
      return;
    }
    if (type === 'tool.started') {
      status(`正在调用工具 ${data.tool_name || ''}…`);
      return;
    }
    if (type === 'tool.completed') {
      status(`工具 ${data.tool_name || ''} 已完成，继续回答…`);
      return;
    }
    if (type === 'tool.failed' || type === 'tool.requires_attention') {
      status(`工具 ${data.tool_name || ''} 未成功：${data.code || '需要人工核对'}`, true);
      return;
    }
    if (type === 'run.queued') {
      status('已排队，等待 Worker 领取…');
      return;
    }
    if (type === 'run.started') {
      status('Worker 已领取，正在执行…');
      return;
    }
    if (type === 'run.recovering') {
      status('执行异常中断，Worker 正在恢复同一个 Run…');
      return;
    }
    if (type === 'run.cancelling') {
      status('正在停止，等待安全边界…');
      return;
    }
    if (type.startsWith('run.') && TERMINAL.has(type.slice(4))) {
      finishRun(type, data);
    }
  }

  async function finishRun(type, data) {
    closeSource();
    const state = type.slice(4);
    clearProvisional();
    activeRun = { ...(activeRun || {}), ...data, status: data.status || state };
    busy = false;
    status(
      state === 'completed' ? '回复已保存。'
        : state === 'cancelled' ? '已停止。可以重试或继续提问。'
        : state === 'requires_attention' ? '执行需要人工核对，已停止自动重试。'
        : `本轮未完成${data.error?.message ? `：${data.error.message}` : '。'}`,
      state !== 'completed',
    );
    controls();
    await reloadConversation();
  }

  async function reloadConversation() {
    if (!selected) return;
    const token = scope.capture();
    try {
      await loadMessages();
      await loadConversations();
    } catch (error) {
      if (scope.current(token)) showError(error);
    }
    if (scope.current(token)) controls();
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

  async function loadTurnState() {
    const token = scope.capture();
    const response = await request(
      `/api/v1/chat/conversations/${encodeURIComponent(selected)}/turns?limit=1`, {}, token,
    );
    const data = await response.json();
    if (!scope.current(token)) return;
    lastTurn = data.items[0] || null;
    const run = lastTurn?.latest_run || null;
    if (isActive(run)) {
      subscribe(run, run.last_event_seq || 0);
      status(`执行中 · ${run.status}${run.attempt_no > 1 ? ` · 第 ${run.attempt_no} 次执行` : ''}`);
    } else {
      closeSource();
      setRun(run);
      if (run) {
        status(
          run.status === 'completed' ? '上一轮已完成。'
            : run.status === 'cancelled' ? '上一轮已停止，可以重试。'
            : run.status === 'requires_attention' ? '上一轮需要人工核对。'
            : `上一轮未完成${run.error?.message ? `：${run.error.message}` : '。'}`,
          run.status !== 'completed',
        );
      } else {
        status(busy ? '会话有未完成的执行。' : '已读取保存的消息');
      }
    }
    controls();
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
    $('chat-title').textContent = data.conversation.title;
    renderMessages();
    if (!older) await loadTurnState();
    controls();
  }

  async function openConversation(id) {
    scope.reset();
    closeSource();
    selected = id;
    records = [];
    before = null;
    busy = false;
    activeRun = null;
    lastTurn = null;
    clearProvisional();
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
    if (!content || sending || busy || isActive(activeRun)) return;
    const token = scope.capture();
    sending = true;
    controls();
    const key = pendingKey && pendingContent === content ? pendingKey : newKey();
    pendingKey = key;
    pendingContent = content;
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
      status('已提交，正在创建这一轮执行…');
      const response = await request(
        `/api/v1/chat/conversations/${encodeURIComponent(selected)}/messages`,
        {
          method: 'POST',
          headers: { 'Idempotency-Key': key },
          body: JSON.stringify({ content }),
        },
        token,
      );
      const run = await response.json();
      if (!scope.current(token)) return;
      pendingKey = null;
      pendingContent = null;
      sending = false;
      records.forEach((row) => { if (row.status === 'pending') row.status = 'complete'; });
      activeRun = run;
      busy = isActive(run);
      status('已排队，等待 Worker 领取…');
      controls();
      subscribe(run, 0);
      await loadConversations();
    } catch (error) {
      if (scope.current(token)) {
        records.forEach((row) => { if (row.status === 'pending') row.status = 'failed'; });
        renderMessages();
        showError(error);
      }
    } finally {
      if (scope.current(token)) {
        sending = false;
        controls();
      }
    }
  }

  async function stopRun() {
    if (!activeRun) return;
    const token = scope.capture();
    status('正在发送停止请求…');
    try {
      await request(
        `/api/v1/chat/runs/${encodeURIComponent(activeRun.run_id)}/cancel`,
        { method: 'POST' },
        token,
      );
      if (!scope.current(token)) return;
      status('已请求停止，等待安全边界…');
    } catch (error) {
      showError(error);
    }
  }

  async function retryRun() {
    if (!lastTurn || !lastTurn.latest_run) return;
    const previous = lastTurn.latest_run.status;
    const reason = previous === 'cancelled' ? 'cancel_retry'
      : previous === 'completed' ? 'regenerate' : 'failure_retry';
    const token = scope.capture();
    try {
      const response = await request(
        `/api/v1/chat/turns/${encodeURIComponent(lastTurn.turn_id)}/runs`,
        { method: 'POST', body: JSON.stringify({ trigger_reason: reason }) },
        token,
      );
      const run = await response.json();
      if (!scope.current(token)) return;
      clearProvisional();
      status('已重新排队…');
      subscribe(run, 0);
      await loadMessages();
    } catch (error) {
      showError(error);
    }
  }

  function reset() {
    scope.reset();
    closeSource();
    selected = null;
    records = [];
    conversations = [];
    before = null;
    nextOffset = null;
    sending = false;
    busy = false;
    provisional = null;
    activeRun = null;
    lastTurn = null;
    pendingKey = null;
    pendingContent = null;
    $('chat-input').value = '';
    $('chat-title').textContent = '售后助手';
    status('新建或选择会话开始');
    renderMessages();
    renderConversations();
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('chat-form').addEventListener('submit', send);
    $('chat-new').addEventListener('click', () => newConversation().catch(showError));
    $('chat-older').addEventListener('click', () => loadMessages(true).catch(showError));
    $('chat-more').addEventListener('click', () => loadConversations(true).catch(showError));
    $('chat-refresh').addEventListener('click', () => loadMessages().catch(showError));
    $('chat-stop').addEventListener('click', () => stopRun().catch(showError));
    $('chat-retry').addEventListener('click', () => retryRun().catch(showError));
    reset();
  });
  document.addEventListener('business:ready', () => {
    reset();
    loadConversations().catch(showError);
  });
  document.addEventListener('auth:logged-out', reset);
  document.addEventListener('auth:unauthorized', reset);
  window.addEventListener('beforeunload', closeSource);
})();
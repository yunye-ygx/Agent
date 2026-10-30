/* Tool debugging page. Executes the real runtime and shows observations, timing and attempts. */
(() => {
  const $ = (id) => document.getElementById(id);
  const api = (url, options) => window.apiRequest(url, options);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[c]));
  const state = { tools: [], current: null, pending: null, loaded: false };

  function setStatus(text, failed = false) {
    const node = $('tool-status');
    node.textContent = text || '';
    node.classList.toggle('error', Boolean(failed));
  }

  function operationSignature(arguments) {
    return JSON.stringify(arguments);
  }

  function renderPending() {
    const node = $('tool-operation-id');
    node.textContent = state.pending ? state.pending.id : '尚未预留';
    $('tool-clear-operation').hidden = !state.pending;
  }

  function typeOf(schema) {
    if (schema.enum) return 'enum';
    if (schema.anyOf) {
      const branch = schema.anyOf.find((item) => item && item.type !== 'null');
      if (branch?.enum) return 'enum';
      if (branch?.type) return branch.type;
    }
    return schema.type || 'string';
  }

  function renderParameters() {
    const tool = state.current;
    const container = $('tool-parameters');
    container.replaceChildren();
    if (!tool) return;
    const properties = tool.parameters.properties || {};
    const required = new Set(tool.parameters.required || []);
    Object.entries(properties).forEach(([name, schema]) => {
      const label = document.createElement('label');
      label.textContent = name;
      label.title = schema.description || '';
      const type = typeOf(schema);
      let input;
      if (type === 'enum') {
        input = document.createElement('select');
        const values = schema.enum || (schema.anyOf || []).find((item) => item.enum)?.enum || [];
        if (!required.has(name)) input.add(new Option('（不指定）', ''));
        values.forEach((value) => input.add(new Option(String(value), String(value))));
        if (schema.default !== undefined) input.value = String(schema.default);
      } else if (type === 'boolean') {
        input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = Boolean(schema.default);
      } else if (type === 'integer' || type === 'number') {
        input = document.createElement('input');
        input.type = 'number';
        input.step = '1';
        if (schema.minimum !== undefined) input.min = schema.minimum;
        if (schema.maximum !== undefined) input.max = schema.maximum;
        if (schema.default !== undefined) input.value = schema.default;
      } else {
        input = document.createElement('input');
        input.type = 'text';
        if (schema.maxLength) input.maxLength = schema.maxLength;
        if (schema.default !== undefined) input.value = schema.default;
      }
      input.id = `tool-param-${name}`;
      input.dataset.param = name;
      input.dataset.paramType = type;
      input.required = required.has(name) && type !== 'boolean';
      if (schema.default !== undefined && type !== 'enum') input.value = schema.default;
      label.append(input);
      if (schema.description) {
        const hint = document.createElement('small');
        hint.className = 'field-hint';
        hint.textContent = schema.description;
        label.append(hint);
      }
      container.append(label);
    });
  }

  function collectArguments() {
    const values = {};
    document.querySelectorAll('#tool-parameters [data-param]').forEach((input) => {
      const name = input.dataset.param;
      const type = input.dataset.paramType;
      if (type === 'boolean') values[name] = input.checked;
      else if (type === 'integer' || type === 'number') {
        if (input.value === '') return;
        const number = Number(input.value);
        values[name] = type === 'integer' ? Math.trunc(number) : number;
      } else if (type === 'enum') {
        if (input.value === '') return;
        values[name] = input.value;
      } else if (input.value !== '') values[name] = input.value;
    });
    return values;
  }

  function renderTools() {
    const node = $('tool-list');
    node.innerHTML = state.tools.map((tool) => `
      <button type="button" class="tool-item ${tool.name === state.current?.name ? 'active' : ''}" data-tool="${escapeHtml(tool.name)}">
        <strong>${escapeHtml(tool.name)}</strong>
        <small>${escapeHtml(tool.effect)} · ${escapeHtml(tool.permission)} · ${tool.timeout_seconds}s / ${tool.max_attempts} 次</small>
        <span>${escapeHtml(tool.description || '')}</span>
      </button>`).join('');
    node.querySelectorAll('[data-tool]').forEach((button) => button.addEventListener('click', () => {
      state.pending = null;
      state.current = state.tools.find((tool) => tool.name === button.dataset.tool);
      renderPending();
      renderTools();
      renderParameters();
      setStatus('');
      $('tool-result').textContent = '';
      if (state.current?.name === 'create_ticket') {
        setStatus('创建工单是真实写入。执行前会自动预留操作键，重复执行同一参数会复用该键。');
      }
    }));
  }

  async function loadTools() {
    setStatus('正在读取工具目录…');
    try {
      const payload = await api('/api/v1/tool-lab/tools');
      state.tools = payload.tools;
      state.loaded = true;
      const admin = payload.identity.role === 'admin';
      $('tool-identity').textContent = `当前登录：${payload.identity.display_name}（${payload.identity.username}）· ${admin ? '管理员' : '客户'} · 身份由后端会话提供，调试页不能切换执行用户`;
      if (!state.tools.length) {
        $('tool-list').innerHTML = '<div class="empty-state">当前身份没有可执行的工具。</div>';
        state.current = null;
        renderParameters();
        setStatus('');
        return;
      }
      if (!state.current || !state.tools.some((tool) => tool.name === state.current.name)) {
        state.current = state.tools[0];
      }
      renderTools();
      renderParameters();
      setStatus('工具目录已加载。参数由服务端 JSON Schema 生成。');
    } catch (error) {
      $('tool-list').innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`;
      setStatus(error.message, true);
    }
  }

  async function reserveForCreateTicket(arguments) {
    const signature = operationSignature(arguments);
    if (state.pending && state.pending.signature === signature) return state.pending.id;
    const operation = await api('/api/v1/operations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'create_ticket', parameters: arguments }),
    });
    state.pending = { id: operation.id, signature, created_at: Date.now() };
    renderPending();
    return operation.id;
  }

  async function execute(event) {
    event.preventDefault();
    if (!state.current) return;
    const argumentsValue = collectArguments();
    const submit = $('tool-submit');
    submit.disabled = true;
    setStatus('正在执行工具…');
    try {
      let operationId = null;
      if (state.current.name === 'create_ticket') {
        operationId = await reserveForCreateTicket(argumentsValue);
      }
      const result = await api('/api/v1/tool-lab/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          call_id: (crypto.randomUUID ? crypto.randomUUID() : `call-${Date.now()}`),
          name: state.current.name,
          arguments: argumentsValue,
          operation_id: operationId,
        }),
      });
      const observation = result.observation;
      renderPending();
      $('tool-observation-meta').textContent = [
        `状态：${observation.status}`,
        `代码：${observation.code}`,
        `尝试：${result.attempts}`,
        `耗时：${result.elapsed_ms} ms`,
        `操作键：${observation.operation_id || '—'}`,
        `下一步：${observation.next_action}`,
        `request_id：${result.request_id || '—'}`,
      ].join(' · ');
      $('tool-result').textContent = JSON.stringify(result.observation, null, 2);
      setStatus(`${observation.message}${observation.status === 'unknown' ? ' 写操作结果不确定，请查询原操作键，不要换键重试。' : ''}`, observation.status !== 'success');
      if (observation.status === 'unknown' && state.pending) {
        setStatus(`${observation.message} 请点击「查询操作结果」，使用原操作键核对后再决定。`, true);
      }
    } catch (error) {
      setStatus(error.message, true);
      $('tool-result').textContent = JSON.stringify(error.detail || { code: error.code, message: error.message }, null, 2);
    } finally { submit.disabled = false; }
  }

  async function reconcile() {
    if (!state.pending) return;
    try {
      const operation = await api(`/api/v1/operations/${encodeURIComponent(state.pending.id)}`);
      $('tool-result').textContent = JSON.stringify(operation, null, 2);
      $('tool-observation-meta').textContent = `操作 ${operation.id} · 状态 ${operation.status} · 过期 ${operation.expired} · 结果可用 ${operation.resource_available}`;
      if (operation.status === 'succeeded' && operation.result_resource_id) {
        const ticket = await api(`/api/v1/tickets/${encodeURIComponent(operation.result_resource_id)}`);
        setStatus(`操作已成功，工单 ${ticket.ticket_no} 已存在；没有重复创建。`);
      } else if (operation.expired) {
        setStatus('操作键已过期且没有成功记录。可以清除操作键后重新发起。', true);
      } else {
        setStatus('操作仍在待执行状态（可能未提交或事务未完成）。请稍后查询，或用原键重试。');
      }
    } catch (error) { setStatus(error.message, true); }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('tool-execute-form').addEventListener('submit', execute);
    $('refresh-tools').addEventListener('click', loadTools);
    $('tool-reconcile').addEventListener('click', reconcile);
    $('tool-clear-operation').addEventListener('click', () => {
      state.pending = null;
      renderPending();
      setStatus('已清除本地操作键；下一次创建工单会预留新键。');
    });
    renderPending();
  });

  document.addEventListener('view:changed', (event) => {
    if (event.detail?.view === 'tool-lab' && window.BusinessAuth?.user) loadTools();
  });
  document.addEventListener('business:ready', () => { if (!state.loaded) loadTools(); });
  document.addEventListener('auth:unauthorized', () => {
    state.tools = [];
    state.current = null;
    state.pending = null;
    state.loaded = false;
    renderPending();
    $('tool-list').replaceChildren();
    $('tool-result').textContent = '';
    $('tool-observation-meta').textContent = '';
    setStatus('');
  });
})();

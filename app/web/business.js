/* After-sales business pages: orders, devices, tickets and admin tasks. */
(() => {
  const $ = (id) => document.getElementById(id);
  const state = { customers: [], pendingOperation: null };
  const PENDING_KEY = 'after_sales_pending_operation_v1';

  const api = (url, options) => window.apiRequest(url, options);
  const isAdmin = () => Boolean(window.BusinessAuth?.isAdmin());
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  }[c]));
  const formatDate = (value) => value ? new Date(value).toLocaleString('zh-CN') : '—';
  const statusText = {
    recorded: '已记录', completed: '已完成', cancelled: '已取消',
    active: '使用中', retired: '已退役',
    open: '待处理', in_progress: '处理中', resolved: '已解决', closed: '已关闭',
    in_warranty: '保修内', out_of_warranty: '已过保',
  };
  const statusPill = (value) => `<span class="status-pill status-${escapeHtml(value)}">${escapeHtml(statusText[value] || value)}</span>`;
  const eventText = { created: '创建', status_change: '状态变更', comment: '处理说明' };

  function toast(message) {
    const node = $('toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('show');
    setTimeout(() => node.classList.remove('show'), 2800);
  }

  function message(id, text, failed = false) {
    const node = $(id);
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('error', Boolean(failed));
  }

  function loadPending() {
    try {
      const value = JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null');
      if (!value?.id || !value?.signature) return null;
      if (Date.now() - Number(value.created_at) > 30 * 60 * 1000) return null;
      return value;
    } catch { return null; }
  }
  function savePending(value) {
    state.pendingOperation = value;
    try { sessionStorage.setItem(PENDING_KEY, JSON.stringify(value)); } catch {}
    renderOperationState();
  }
  function clearPending() {
    state.pendingOperation = null;
    try { sessionStorage.removeItem(PENDING_KEY); } catch {}
    renderOperationState();
  }
  function renderOperationState() {
    const node = $('ticket-operation-state');
    const button = $('ticket-reconcile');
    if (!node || !button) return;
    const pending = state.pendingOperation;
    node.textContent = pending
      ? `已预留操作键 ${pending.id.slice(0, 8)}…，再次提交会复用它。结果未知时先查询，不要换新键。`
      : '';
    button.hidden = !pending;
  }

  async function loadDashboard() {
    const node = $('business-stats');
    if (!node) return;
    node.innerHTML = '<div class="loading-state">正在读取业务数据…</div>';
    const safe = (promise) => promise.catch(() => ({ total: null }));
    const [orders, devices, tickets] = await Promise.all([
      safe(api('/api/v1/orders?page=1&page_size=1')),
      safe(api('/api/v1/devices?page=1&page_size=1')),
      safe(api('/api/v1/tickets?page=1&page_size=1')),
    ]);
    const admin = isAdmin();
    node.innerHTML = `
      <article class="stat-card"><span>订单</span><strong>${orders.total ?? '—'}</strong><small>${admin ? '全部客户' : '仅本人'}</small></article>
      <article class="stat-card"><span>设备</span><strong>${devices.total ?? '—'}</strong><small>${admin ? '全部客户' : '仅本人'}</small></article>
      <article class="stat-card"><span>工单</span><strong>${tickets.total ?? '—'}</strong><small>可查看处理记录</small></article>`;
    $('business-welcome').textContent = `${window.BusinessAuth.user?.display_name || ''}，这里可以查看订单、设备、工单并检索售后资料。`;
  }

  // Orders -----------------------------------------------------------------
  function orderItemRow() {
    const row = document.createElement('div');
    row.className = 'order-item-row';
    row.innerHTML = `
      <label>产品名称<input class="item-name" required maxlength="200" placeholder="例如：澄印 NX100 标签打印机"></label>
      <label>型号<select class="item-model"><option value="NX100">NX100</option><option value="NX200">NX200</option></select></label>
      <label>数量<input class="item-quantity" type="number" min="1" max="1000" value="1" required></label>
      <label>单价（元）<input class="item-price" inputmode="decimal" value="0.00" required></label>
      <button type="button" class="ghost-button" data-remove-item>移除</button>`;
    row.querySelector('[data-remove-item]').addEventListener('click', () => row.remove());
    return row;
  }
  function addOrderItemRow() { $('order-item-rows').append(orderItemRow()); }

  async function searchCustomers() {
    const query = $('order-customer-query').value.trim();
    try {
      const result = await api(`/api/v1/admin/customers?q=${encodeURIComponent(query)}&page=1&page_size=20`);
      state.customers = result.items;
      $('order-customer').innerHTML = result.items.length
        ? result.items.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name)}（${escapeHtml(item.username)}）</option>`).join('')
        : '<option value="">没有匹配的客户</option>';
      message('order-form-message', result.items.length ? `找到 ${result.items.length} 位客户。` : '没有匹配客户，请先确认客户已注册。', !result.items.length);
    } catch (error) { message('order-form-message', error.message, true); }
  }

  async function submitOrder(event) {
    event.preventDefault();
    const submit = $('order-submit');
    const items = [...document.querySelectorAll('#order-item-rows .order-item-row')].map((row) => ({
      product_name: row.querySelector('.item-name').value.trim(),
      product_model: row.querySelector('.item-model').value,
      quantity: Number(row.querySelector('.item-quantity').value),
      unit_price: row.querySelector('.item-price').value.trim(),
    }));
    if (!items.length) { message('order-form-message', '至少需要一条订单明细。', true); return; }
    submit.disabled = true;
    try {
      const order = await api('/api/v1/orders', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          order_no: $('order-no').value.trim(),
          user_id: $('order-customer').value,
          items,
        }),
      });
      message('order-form-message', `订单 ${order.order_no} 已保存，总额 ${order.total_amount} 元。`);
      toast('订单已录入');
      $('order-form').reset();
      $('order-item-rows').replaceChildren();
      addOrderItemRow();
      await loadOrders();
      await loadOrderDetail(order.id);
    } catch (error) { message('order-form-message', error.message, true); }
    finally { submit.disabled = false; }
  }

  async function loadOrders() {
    const node = $('order-list');
    if (!node) return;
    node.innerHTML = '<div class="loading-state">正在读取订单…</div>';
    try {
      const result = await api('/api/v1/orders?page=1&page_size=50');
      if (!result.items.length) { node.innerHTML = '<div class="empty-state">还没有订单。管理员录入后会显示在这里。</div>'; return; }
      node.innerHTML = `<table class="data-table"><thead><tr><th>订单号</th><th>状态</th><th>金额</th><th>明细</th>${isAdmin() ? '<th>客户</th>' : ''}<th>创建时间</th><th></th></tr></thead><tbody>${result.items.map((order) => `
        <tr><td>${escapeHtml(order.order_no)}</td><td>${statusPill(order.status)}</td><td>${escapeHtml(order.total_amount)} ${escapeHtml(order.currency)}</td><td>${order.item_count}</td>
        ${isAdmin() ? `<td>${escapeHtml(order.customer_display_name || '')}</td>` : ''}
        <td>${formatDate(order.created_at)}</td><td><button class="ghost-button" data-open-order="${escapeHtml(order.id)}">详情</button></td></tr>`).join('')}</tbody></table>`;
      node.querySelectorAll('[data-open-order]').forEach((button) => button.addEventListener('click', () => loadOrderDetail(button.dataset.openOrder)));
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  async function loadOrderDetail(id) {
    const node = $('order-detail');
    window.BusinessAuth.showView('order-detail');
    node.innerHTML = '<div class="loading-state">正在读取订单详情…</div>';
    try {
      const order = await api(`/api/v1/orders/${encodeURIComponent(id)}`);
      const actions = isAdmin() && order.status !== 'cancelled'
        ? `<div class="action-row">${order.status === 'recorded' ? '<button class="primary-button" data-order-status="completed">标记完成</button>' : ''}<button class="danger-button" data-order-status="cancelled">取消订单</button></div>`
        : '';
      node.innerHTML = `
        <div class="detail-heading"><div><p class="eyebrow">ORDER</p><h2>${escapeHtml(order.order_no)}</h2><p class="muted">客户 ${escapeHtml(order.customer_display_name || '')} · ${formatDate(order.created_at)}</p></div><div class="action-row">${statusPill(order.status)}${actions}</div></div>
        <table class="data-table"><thead><tr><th>产品</th><th>型号</th><th>数量</th><th>单价</th><th>小计</th></tr></thead><tbody>${order.items.map((item) => `<tr><td>${escapeHtml(item.product_name)}</td><td>${escapeHtml(item.product_model)}</td><td>${item.quantity}</td><td>${escapeHtml(item.unit_price)}</td><td>${escapeHtml(item.line_amount)}</td></tr>`).join('')}</tbody></table>
        <p class="amount-line">合计：<strong>${escapeHtml(order.total_amount)} ${escapeHtml(order.currency)}</strong></p>`;
      node.querySelectorAll('[data-order-status]').forEach((button) => button.addEventListener('click', async () => {
        if (!window.confirm(button.dataset.orderStatus === 'cancelled' ? '取消后该订单不能继续登记设备，确定吗？' : '确认标记为已完成？')) return;
        try {
          await api(`/api/v1/orders/${encodeURIComponent(order.id)}`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: button.dataset.orderStatus }),
          });
          toast('订单状态已更新');
          await loadOrderDetail(order.id);
          await loadOrders();
        } catch (error) { message('order-form-message', error.message, true); }
      }));
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  // Devices ----------------------------------------------------------------
  function defaultDates() {
    const today = new Date();
    const next = new Date(today);
    next.setFullYear(next.getFullYear() + 1);
    return [today.toISOString().slice(0, 10), next.toISOString().slice(0, 10)];
  }

  async function loadOrderItemsForDevice() {
    const orderNo = $('device-order-no').value.trim();
    if (!orderNo) { message('device-form-message', '请先输入订单号。', true); return; }
    try {
      const order = await api(`/api/v1/orders/by-no/${encodeURIComponent(orderNo)}`);
      if (order.status === 'cancelled') { message('device-form-message', '已取消订单不能登记设备。', true); }
      $('device-order-item').innerHTML = order.items.map((item) => `<option value="${escapeHtml(item.id)}" data-model="${escapeHtml(item.product_model)}">${escapeHtml(item.product_name)} · ${escapeHtml(item.product_model)} · 数量 ${item.quantity}</option>`).join('');
      if (order.items.length) $('device-model').value = order.items[0].product_model;
      message('device-form-message', `已找到订单 ${order.order_no} 的 ${order.items.length} 条明细。`);
    } catch (error) { message('device-form-message', error.message, true); }
  }

  async function submitDevice(event) {
    event.preventDefault();
    const button = $('device-submit');
    button.disabled = true;
    try {
      const device = await api('/api/v1/devices', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          serial_no: $('device-serial').value.trim(),
          order_item_id: $('device-order-item').value,
          model_code: $('device-model').value,
          firmware_version: $('device-firmware').value.trim() || null,
          purchased_on: $('device-purchased').value,
          warranty_until: $('device-warranty').value,
        }),
      });
      message('device-form-message', `设备 ${device.serial_no} 已登记到客户 ${device.customer_display_name}。`);
      toast('设备已登记');
      $('device-form').reset();
      const [purchased, warranty] = defaultDates();
      $('device-purchased').value = purchased;
      $('device-warranty').value = warranty;
      await loadDevices();
      await loadDeviceDetail(device.id);
    } catch (error) { message('device-form-message', error.message, true); }
    finally { button.disabled = false; }
  }

  async function loadDevices() {
    const node = $('device-list');
    if (!node) return;
    node.innerHTML = '<div class="loading-state">正在读取设备…</div>';
    try {
      const result = await api('/api/v1/devices?page=1&page_size=50');
      if (!result.items.length) { node.innerHTML = '<div class="empty-state">还没有设备。管理员登记后会显示在这里。</div>'; return; }
      node.innerHTML = `<table class="data-table"><thead><tr><th>设备编号</th><th>型号</th><th>固件</th><th>保修</th>${isAdmin() ? '<th>客户</th>' : ''}<th>订单</th><th></th></tr></thead><tbody>${result.items.map((device) => `
        <tr><td>${escapeHtml(device.serial_no)}</td><td>${escapeHtml(device.model_code)}</td><td>${escapeHtml(device.firmware_version || '—')}</td>
        <td>${statusPill(device.warranty_status)}<small class="muted"> 至 ${escapeHtml(device.warranty_until)}</small></td>
        ${isAdmin() ? `<td>${escapeHtml(device.customer_display_name || '')}</td>` : ''}
        <td>${escapeHtml(device.order_no)}</td><td><button class="ghost-button" data-open-device="${escapeHtml(device.id)}">详情</button></td></tr>`).join('')}</tbody></table>`;
      node.querySelectorAll('[data-open-device]').forEach((button) => button.addEventListener('click', () => loadDeviceDetail(button.dataset.openDevice)));
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  async function loadDeviceDetail(id) {
    const node = $('device-detail');
    window.BusinessAuth.showView('device-detail');
    try {
      const device = await api(`/api/v1/devices/${encodeURIComponent(id)}`);
      node.innerHTML = `
        <div class="detail-heading"><div><p class="eyebrow">DEVICE</p><h2>${escapeHtml(device.serial_no)}</h2><p class="muted">${escapeHtml(device.model_code)} · 固件 ${escapeHtml(device.firmware_version || '未知')}</p></div><div>${statusPill(device.warranty_status)}</div></div>
        <dl class="detail-grid"><div><dt>购买日期</dt><dd>${escapeHtml(device.purchased_on)}</dd></div><div><dt>保修截止</dt><dd>${escapeHtml(device.warranty_until)}（含当天）</dd></div><div><dt>所属客户</dt><dd>${escapeHtml(device.customer_display_name || '')}</dd></div><div><dt>关联订单</dt><dd>${escapeHtml(device.order_no)}</dd></div><div><dt>产品</dt><dd>${escapeHtml(device.product_name)}</dd></div><div><dt>设备状态</dt><dd>${statusPill(device.status)}</dd></div></dl>`;
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  // Tickets ----------------------------------------------------------------
  async function loadTicketDevices() {
    try {
      const result = await api('/api/v1/devices?page=1&page_size=100');
      const select = $('ticket-device');
      select.innerHTML = result.items.length
        ? result.items.map((device) => `<option value="${escapeHtml(device.serial_no)}">${escapeHtml(device.serial_no)} · ${escapeHtml(device.model_code)}${device.warranty_active ? '' : ' · 已过保'}</option>`).join('')
        : '<option value="">没有可用设备</option>';
      message('ticket-form-message', result.items.length ? '' : '还没有登记设备，无法创建工单。', !result.items.length);
    } catch (error) { message('ticket-form-message', error.message, true); }
  }

  function ticketParameters() {
    return {
      serial_no: $('ticket-device').value,
      subject: $('ticket-subject').value.trim(),
      description: $('ticket-description').value.trim(),
    };
  }

  async function reserveTicketOperation(parameters) {
    return api('/api/v1/operations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'create_ticket', parameters }),
    });
  }

  async function submitTicket(event) {
    event.preventDefault();
    const button = $('ticket-submit');
    const parameters = ticketParameters();
    const signature = JSON.stringify(parameters);
    if (!parameters.serial_no || !parameters.subject || !parameters.description) {
      message('ticket-form-message', '请选择设备并填写主题和故障描述。', true);
      return;
    }
    button.disabled = true;
    try {
      let pending = state.pendingOperation;
      if (!pending || pending.signature !== signature) {
        const operation = await reserveTicketOperation(parameters);
        pending = { id: operation.id, signature, created_at: Date.now() };
        savePending(pending);
      }
      message('ticket-form-message', '正在提交真实工单…');
      const ticket = await api('/api/v1/tickets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operation_id: pending.id, ...parameters }),
      });
      clearPending();
      message('ticket-form-message', `工单 ${ticket.ticket_no} 已创建。`);
      toast('工单已创建');
      $('ticket-form').reset();
      await loadTickets();
      await loadTicketDetail(ticket.id);
    } catch (error) {
      if (error.status >= 500 || error.code === 'OPERATION_RESULT_UNKNOWN') {
        message('ticket-form-message', `${error.message} 已保留操作键，请先查询操作结果或使用原键重试。`, true);
        renderOperationState();
      } else {
        message('ticket-form-message', error.message, true);
        if (error.code === 'OPERATION_PARAMETER_MISMATCH' || error.code === 'OPERATION_EXPIRED') {
          clearPending();
        }
      }
    } finally { button.disabled = false; }
  }

  async function reconcileTicketOperation() {
    const pending = state.pendingOperation;
    if (!pending) return;
    try {
      const operation = await api(`/api/v1/operations/${encodeURIComponent(pending.id)}`);
      if (operation.status === 'succeeded' && operation.result_resource_id) {
        if (!operation.resource_available) {
          message('ticket-form-message', '操作已成功，但当前身份已无法查看对应工单。', true);
          return;
        }
        const ticket = await api(`/api/v1/tickets/${encodeURIComponent(operation.result_resource_id)}`);
        clearPending();
        message('ticket-form-message', `操作已成功，工单 ${ticket.ticket_no} 已存在。`);
        await loadTickets();
        await loadTicketDetail(ticket.id);
      } else if (operation.expired) {
        message('ticket-form-message', '该操作键已过期且未记录成功。可以清除后重新发起。', true);
      } else {
        message('ticket-form-message', '该操作尚未记录成功（可能仍在事务中）。可以稍后再查，或用原操作键安全重试。', false);
      }
    } catch (error) { message('ticket-form-message', error.message, true); }
  }

  async function loadTickets() {
    const node = $('ticket-list');
    if (!node) return;
    const status = $('ticket-status-filter').value;
    node.innerHTML = '<div class="loading-state">正在读取工单…</div>';
    try {
      const result = await api(`/api/v1/tickets?page=1&page_size=50${status ? `&status=${encodeURIComponent(status)}` : ''}`);
      if (!result.items.length) { node.innerHTML = '<div class="empty-state">没有符合条件的工单。</div>'; return; }
      node.innerHTML = `<table class="data-table"><thead><tr><th>工单号</th><th>主题</th><th>状态</th><th>版本</th><th>设备</th>${isAdmin() ? '<th>客户</th>' : ''}<th>更新时间</th><th></th></tr></thead><tbody>${result.items.map((ticket) => `
        <tr><td>${escapeHtml(ticket.ticket_no)}</td><td>${escapeHtml(ticket.subject)}</td><td>${statusPill(ticket.status)}</td><td>${ticket.version}</td>
        <td>${escapeHtml(ticket.device_serial_no)}</td>${isAdmin() ? `<td>${escapeHtml(ticket.customer_display_name)}</td>` : ''}
        <td>${formatDate(ticket.updated_at)}</td><td><button class="ghost-button" data-open-ticket="${escapeHtml(ticket.id)}">详情</button></td></tr>`).join('')}</tbody></table>`;
      node.querySelectorAll('[data-open-ticket]').forEach((button) => button.addEventListener('click', () => loadTicketDetail(button.dataset.openTicket)));
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  function nextTicketStatuses(status) {
    return {
      open: ['in_progress'],
      in_progress: ['open', 'resolved'],
      resolved: ['in_progress', 'closed'],
      closed: [],
    }[status] || [];
  }

  async function loadTicketDetail(id) {
    const node = $('ticket-detail');
    window.BusinessAuth.showView('ticket-detail');
    node.innerHTML = '<div class="loading-state">正在读取工单详情…</div>';
    try {
      const ticket = await api(`/api/v1/tickets/${encodeURIComponent(id)}?events_page=1&events_page_size=50`);
      const adminForm = isAdmin() ? `
        <form id="ticket-event-form" class="card nested-card">
          <h3>管理员处理</h3>
          <p class="field-hint">当前版本 ${ticket.version}。状态和说明会写入同一条处理记录；版本冲突时请刷新。</p>
          <label>处理说明<textarea id="ticket-event-message" required maxlength="2000" rows="3"></textarea></label>
          <label>状态变更<select id="ticket-event-status">
            <option value="">仅添加说明</option>
            ${nextTicketStatuses(ticket.status).map((value) => `<option value="${value}">${escapeHtml(statusText[value])}</option>`).join('')}
          </select></label>
          <input type="hidden" id="ticket-event-version" value="${ticket.version}">
          <button class="primary-button" type="submit">提交处理</button>
          <p id="ticket-event-message-state" class="field-hint" role="status"></p>
        </form>` : '';
      node.innerHTML = `
        <div class="detail-heading"><div><p class="eyebrow">TICKET</p><h2>${escapeHtml(ticket.ticket_no)}</h2><p class="muted">${escapeHtml(ticket.subject)}</p></div><div>${statusPill(ticket.status)}<span class="muted"> 版本 ${ticket.version}</span></div></div>
        <dl class="detail-grid"><div><dt>设备</dt><dd>${escapeHtml(ticket.device_serial_no)} · ${escapeHtml(ticket.device_model)}</dd></div><div><dt>客户</dt><dd>${escapeHtml(ticket.customer_display_name)}</dd></div><div><dt>创建</dt><dd>${formatDate(ticket.created_at)}</dd></div><div><dt>更新</dt><dd>${formatDate(ticket.updated_at)}</dd></div></dl>
        <section class="ticket-description"><h3>故障描述</h3><p>${escapeHtml(ticket.description)}</p></section>
        <section><h3>处理记录（${ticket.events_total}）</h3><ol class="event-list">${ticket.events.map((event) => `<li><div><strong>${escapeHtml(event.actor_display_name)}</strong> · ${escapeHtml(eventText[event.event_type] || event.event_type)}</div><p>${escapeHtml(event.message)}</p><small class="muted">${escapeHtml(statusText[event.from_status] || '—')} → ${escapeHtml(statusText[event.to_status] || '—')} · ${formatDate(event.created_at)}</small></li>`).join('') || '<li>暂无处理记录</li>'}</ol></section>
        ${adminForm}`;
      const form = $('ticket-event-form');
      if (form) form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
          await api(`/api/v1/tickets/${encodeURIComponent(ticket.id)}/events`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              expected_version: Number($('ticket-event-version').value),
              message: $('ticket-event-message').value,
              to_status: $('ticket-event-status').value || null,
            }),
          });
          toast('处理记录已保存');
          await loadTicketDetail(ticket.id);
          await loadTickets();
        } catch (error) {
          message('ticket-event-message-state', error.status === 409 ? `${error.message} 已刷新最新版本。` : error.message, true);
          if (error.status === 409) {
            await loadTicketDetail(ticket.id);
            await loadTickets();
          }
        } finally { button.disabled = false; }
      });
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  // Admin: customers -------------------------------------------------------
  async function loadCustomers() {
    const node = $('customer-list');
    const query = $('customer-query').value.trim();
    node.innerHTML = '<div class="loading-state">正在查询客户…</div>';
    try {
      const result = await api(`/api/v1/admin/customers?q=${encodeURIComponent(query)}&page=1&page_size=50`);
      node.innerHTML = result.items.length
        ? `<table class="data-table"><thead><tr><th>用户名</th><th>显示名</th><th>状态</th><th>注册时间</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(item.username)}</td><td>${escapeHtml(item.display_name)}</td><td>${statusPill(item.status)}</td><td>${formatDate(item.created_at)}</td></tr>`).join('')}</tbody></table>`
        : '<div class="empty-state">没有匹配客户。</div>';
    } catch (error) { node.innerHTML = `<div class="empty-state error">${escapeHtml(error.message)}</div>`; }
  }

  function resetBusinessState() {
    state.customers = [];
    state.pendingOperation = null;
    try { sessionStorage.removeItem(PENDING_KEY); } catch {}
    renderOperationState();
  }

  document.addEventListener('DOMContentLoaded', () => {
    addOrderItemRow();
    const [purchased, warranty] = defaultDates();
    $('device-purchased').value = purchased;
    $('device-warranty').value = warranty;
    state.pendingOperation = loadPending();
    renderOperationState();

    $('toggle-order-form').addEventListener('click', () => $('order-create-card').classList.toggle('hidden'));
    $('toggle-device-form').addEventListener('click', () => $('device-create-card').classList.toggle('hidden'));
    $('search-order-customers').addEventListener('click', searchCustomers);
    $('order-customer-query').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); searchCustomers(); }
    });
    $('add-order-item').addEventListener('click', addOrderItemRow);
    $('order-form').addEventListener('submit', submitOrder);

    $('load-order-items').addEventListener('click', loadOrderItemsForDevice);
    $('device-order-item').addEventListener('change', (event) => {
      const option = event.target.selectedOptions[0];
      if (option?.dataset.model) $('device-model').value = option.dataset.model;
    });
    $('device-form').addEventListener('submit', submitDevice);

    $('ticket-form').addEventListener('submit', submitTicket);
    $('ticket-reconcile').addEventListener('click', reconcileTicketOperation);
    $('ticket-status-filter').addEventListener('change', loadTickets);

    $('customer-search-form').addEventListener('submit', (event) => { event.preventDefault(); loadCustomers(); });

    $('back-to-orders').addEventListener('click', () => {
      window.BusinessAuth.showView('orders');
      loadOrders();
    });
    $('back-to-devices').addEventListener('click', () => {
      window.BusinessAuth.showView('devices');
      loadDevices();
    });
    $('back-to-tickets').addEventListener('click', () => {
      window.BusinessAuth.showView('tickets');
      loadTickets();
    });
  });

  document.addEventListener('business:ready', async () => {
    resetBusinessState();
    await loadDashboard();
    await loadTicketDevices();
  });

  document.addEventListener('auth:unauthorized', resetBusinessState);
  document.addEventListener('auth:logged-out', resetBusinessState);

  document.addEventListener('view:changed', async (event) => {
    const view = event.detail?.view;
    if (!window.BusinessAuth?.user) return;
    try {
      if (view === 'business') await loadDashboard();
      if (view === 'orders') await loadOrders();
      if (view === 'devices') { await loadDevices(); }
      if (view === 'tickets') { await loadTickets(); await loadTicketDevices(); }
      if (view === 'customers' && isAdmin()) await loadCustomers();
    } catch (error) { toast(error.message); }
  });
})();

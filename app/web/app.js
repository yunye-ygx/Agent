const state = {
  knowledgeBases: [], knowledgeBase: null, document: null, chunks: [], documents: [],
  modelCatalog: [], modelConfigurations: [], editingModel: null, modelChoices: { chat_models: [], embedding_models: [] }, modelDiscoverySeq: 0, editingKnowledgeBase: null, poller: null, selectedKnowledgeBaseIds: new Set(),
};
const $ = (id) => document.getElementById(id);
const request = (url, options = {}) => window.apiRequest(url, options);
const toast = (message) => { const node = $('toast'); node.textContent = message; node.classList.add('show'); setTimeout(() => node.classList.remove('show'), 2600); };
const statusClass = (value) => `status-pill status-${String(value).toLowerCase()}`;

function showView(name) {
  if (window.BusinessAuth?.showView) { window.BusinessAuth.showView(name); return; }
  document.querySelectorAll('.view').forEach((node) => node.classList.toggle('active-view', node.id === `${name}-view`));
  document.querySelectorAll('.nav-item').forEach((node) => node.classList.toggle('active', node.dataset.view === name));
}
async function loadKnowledgeBases() { state.knowledgeBases = await request('/api/v1/knowledge-bases'); renderKnowledgeBases(); }
function renderKnowledgeBases() {
  const node = $('kb-list');
  state.selectedKnowledgeBaseIds = new Set([...state.selectedKnowledgeBaseIds].filter((id) => state.knowledgeBases.some((kb) => kb.id === id)));
  if (!state.knowledgeBases.length) { node.innerHTML = '<div class="empty-state">还没有知识库，先创建一个吧。</div>'; updateKnowledgeBaseSelection(); return; }
  node.innerHTML = `<table class="data-table"><thead><tr><th><input type="checkbox" data-select-all-kbs aria-label="全选知识库"></th><th>名称</th><th>描述</th><th>文档数</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${state.knowledgeBases.map((kb) => `<tr><td><input type="checkbox" data-select-kb="${escapeHtml(kb.id)}" aria-label="选择 ${escapeHtml(kb.name)}" ${state.selectedKnowledgeBaseIds.has(kb.id) ? 'checked' : ''}></td><td><button class="link-button" data-open-kb="${escapeHtml(kb.id)}">${escapeHtml(kb.name)}</button></td><td>${escapeHtml(kb.description || '—')}</td><td>${kb.document_count}</td><td>${formatDate(kb.updated_at)}</td><td><div class="kb-actions"><button class="ghost-button" data-rename-kb="${escapeHtml(kb.id)}">修改名称</button><button class="danger-button" data-delete-kb="${escapeHtml(kb.id)}">删除</button><button class="ghost-button" data-open-kb="${escapeHtml(kb.id)}">进入</button></div></td></tr>`).join('')}</tbody></table>`;
  node.querySelectorAll('[data-open-kb]').forEach((button) => button.addEventListener('click', () => openKnowledgeBase(button.dataset.openKb)));
  node.querySelectorAll('[data-rename-kb]').forEach((button) => button.addEventListener('click', () => openKnowledgeBaseRename(button.dataset.renameKb)));
  node.querySelectorAll('[data-delete-kb]').forEach((button) => button.addEventListener('click', () => deleteKnowledgeBases([button.dataset.deleteKb])));
  node.querySelectorAll('[data-select-kb]').forEach((checkbox) => checkbox.addEventListener('change', () => {
    if (checkbox.checked) state.selectedKnowledgeBaseIds.add(checkbox.dataset.selectKb);
    else state.selectedKnowledgeBaseIds.delete(checkbox.dataset.selectKb);
    updateKnowledgeBaseSelection();
  }));
  node.querySelector('[data-select-all-kbs]').addEventListener('change', (event) => {
    state.selectedKnowledgeBaseIds = new Set(event.target.checked ? state.knowledgeBases.map((kb) => kb.id) : []);
    node.querySelectorAll('[data-select-kb]').forEach((checkbox) => { checkbox.checked = event.target.checked; });
    updateKnowledgeBaseSelection();
  });
  updateKnowledgeBaseSelection();
}
function updateKnowledgeBaseSelection() {
  const button = $('delete-selected-kbs');
  button.disabled = state.selectedKnowledgeBaseIds.size === 0;
  button.textContent = `删除所选（${state.selectedKnowledgeBaseIds.size}）`;
  const selectAll = document.querySelector('[data-select-all-kbs]');
  if (selectAll) {
    selectAll.checked = state.knowledgeBases.length > 0 && state.selectedKnowledgeBaseIds.size === state.knowledgeBases.length;
    selectAll.indeterminate = state.selectedKnowledgeBaseIds.size > 0 && !selectAll.checked;
  }
}
function openKnowledgeBaseRename(id) {
  const kb = state.knowledgeBases.find((item) => item.id === id);
  if (!kb || kb.status !== 'ACTIVE') return;
  state.editingKnowledgeBase = kb;
  $('rename-kb-name').value = kb.name;
  $('rename-kb-dialog').showModal();
  $('rename-kb-name').focus();
  $('rename-kb-name').select();
}
async function deleteKnowledgeBases(ids, confirmed = false) {
  const selected = state.knowledgeBases.filter((item) => ids.includes(item.id));
  if (!selected.length) return;
  try {
    const response = await fetch('/api/v1/knowledge-bases/batch-delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids, confirm_with_documents: confirmed }),
    });
    if (response.status === 409) {
      const body = await response.json();
      const detail = body.detail;
      if (!confirmed && detail?.code === 'CONFIRMATION_REQUIRED') {
        const items = detail.knowledge_bases || selected.map((kb) => ({ name: kb.name, document_count: kb.document_count }));
        const summary = items.map((item) => `“${item.name}”（${item.document_count} 个文件）`).join('、');
        const skipped = detail.skipped?.length ? `\n\n${detail.skipped.length} 个知识库因正在解析会被跳过。` : '';
        const accepted = window.confirm(`确认永久删除以下知识库及其文件、解析记录、Chunk 和检索实验数据吗？\n${summary}${skipped}`);
        if (accepted) await deleteKnowledgeBases(ids, true);
        return;
      }
      throw new Error(typeof detail === 'string' ? detail : '知识库正在解析或当前状态不允许删除');
    }
    if (!response.ok) {
      let detail = '删除知识库失败';
      try { detail = (await response.json()).detail || detail; } catch {}
      throw new Error(typeof detail === 'string' ? detail : '删除知识库失败');
    }
    const result = await response.json();
    state.selectedKnowledgeBaseIds.clear();
    const skipped = result.skipped || [];
    const acceptedCount = result.accepted?.length || 0;
    const skippedSummary = skipped.map((item) => `${state.knowledgeBases.find((kb) => kb.id === item.id)?.name || item.id}：${item.reason}`).join('；');
    toast(`${acceptedCount} 个知识库已加入后台删除队列${skipped.length ? `；跳过 ${skipped.length} 个：${skippedSummary}` : ''}`);
    await loadKnowledgeBases();
  } catch (error) {
    toast(error.message);
  }
}
async function openKnowledgeBase(id) {
  state.knowledgeBase = await request(`/api/v1/knowledge-bases/${id}`); $('kb-list-card').classList.add('hidden'); $('kb-detail').classList.remove('hidden'); $('kb-title').textContent = state.knowledgeBase.name; $('kb-description').textContent = state.knowledgeBase.description || '暂无描述'; await loadDocuments();
}
async function loadDocuments() {
  const docs = await request(`/api/v1/knowledge-bases/${state.knowledgeBase.id}/documents`);
  const details = await Promise.all(docs.map((doc) => request(`/api/v1/documents/${doc.id}`).catch(() => null)));
  state.documents = docs.map((doc, index) => ({ ...doc, detail: details[index] }));
  $('document-summary').textContent = `${docs.length} 个文档`;
  const node = $('document-list');
  if (!docs.length) { node.innerHTML = '<div class="empty-state">上传第一个文档开始解析。</div>'; stopDocumentPolling(); return; }
  node.innerHTML = `<table class="data-table"><thead><tr><th>文件名</th><th>状态</th><th>当前版本</th><th>构建版本</th><th>解析进度</th><th>售后开放</th><th>操作</th></tr></thead><tbody>${state.documents.map(renderDocumentRow).join('')}</tbody></table>`;
  node.querySelectorAll('[data-open-doc]').forEach((button) => button.addEventListener('click', () => openDocument(button.dataset.openDoc)));
  node.querySelectorAll('[data-parse-doc]').forEach((button) => button.addEventListener('click', () => parseDocument(button.dataset.parseDoc, button.dataset.action)));
  node.querySelectorAll('[data-cancel-doc]').forEach((button) => button.addEventListener('click', () => cancelDocument(button.dataset.cancelDoc)));
  node.querySelectorAll('[data-delete-doc]').forEach((button) => button.addEventListener('click', () => deleteDocument(button.dataset.deleteDoc)));
  node.querySelectorAll('[data-toggle-support]').forEach((button) => button.addEventListener('click', () => toggleDocumentSupport(button.dataset.toggleSupport, button.dataset.open !== 'true')));
  if (state.documents.some((doc) => doc.detail?.building_generation_id)) startDocumentPolling(); else stopDocumentPolling();
}
function generationProgress(generation) {
  if (!generation) return { percent: 0, label: '未开始' };
  const expected = Number(generation.expected_task_count || 0);
  const completed = Number(generation.completed_task_count || 0);
  const percent = generation.status === 'SUCCESS' ? 100 : expected ? Math.min(100, Math.round((completed / expected) * 100)) : 0;
  if (generation.status === 'FAILED') return { percent, label: `失败 ${generation.failed_task_count || 0} 个` };
  if (generation.status === 'CANCELLED') return { percent, label: '已取消' };
  return { percent, label: `${completed} / ${expected} 个任务 · ${percent}%` };
}
function renderDocumentRow(doc) {
  const detail = doc.detail || doc;
  const generation = detail.building_generation || detail.latest_run || detail.current_generation;
  const progress = generationProgress(generation);
  const building = Boolean(detail.building_generation_id);
  const action = detail.current_generation_id ? 'reparse' : 'parse';
  const parseButton = building
    ? `<button class="ghost-button" data-cancel-doc="${doc.id}">取消解析</button>`
    : `<button class="primary-button" data-parse-doc="${doc.id}" data-action="${action}">${action === 'reparse' ? '重新解析' : '开始解析'}</button>`;
  const supportOpen = Boolean(doc.support_open);
  const supportCell = `<div class="document-support"><span class="status-pill ${supportOpen ? 'status-success' : 'status-closed'}">${supportOpen ? '已开放' : '未开放'}</span><button class="ghost-button" data-toggle-support="${doc.id}" data-open="${supportOpen}" title="只有已发布的文档才能开放给客户检索">${supportOpen ? '停止开放' : '开放售后'}</button></div>`;
  return `<tr><td class="document-name">${escapeHtml(doc.filename)}</td><td><span class="${statusClass(doc.status)}">${doc.status}</span></td><td>${detail.current_generation?.version_no ? `v${detail.current_generation.version_no}` : '—'}</td><td>${detail.building_generation?.version_no ? `v${detail.building_generation.version_no}` : '—'}</td><td><div class="document-progress" aria-label="解析进度 ${progress.percent}%"><div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.percent}"><div class="progress-bar" style="width: ${progress.percent}%"></div></div><span>${progress.label}</span></div></td><td>${supportCell}</td><td><div class="document-actions">${parseButton}<button class="ghost-button" data-open-doc="${doc.id}">查看</button><button class="danger-button" data-delete-doc="${doc.id}">删除</button></div></td></tr>`;
}
async function toggleDocumentSupport(id, open) {
  try {
    await request(`/api/v1/documents/${id}/support`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ open }),
    });
    toast(open ? '已开放给客户检索' : '已停止开放');
    await loadDocuments();
  } catch (error) { toast(error.message); }
}
async function uploadDocument(event) {
  const file = event.target.files[0]; if (!file || !state.knowledgeBase) return; const form = new FormData(); form.append('file', file);
  try { await request(`/api/v1/knowledge-bases/${state.knowledgeBase.id}/documents`, { method: 'POST', body: form }); toast('文档上传成功'); await loadDocuments(); } catch (error) { toast(error.message); } event.target.value = '';
}
async function openDocument(id) {
  state.document = await request(`/api/v1/documents/${id}`); $('kb-detail').classList.add('hidden'); $('document-detail').classList.remove('hidden'); $('document-title').textContent = state.document.filename; $('document-preview').src = `/api/v1/documents/${id}/file`; await refreshDocument();
}
async function refreshDocument() {
  state.document = await request(`/api/v1/documents/${state.document.id}`);
  if (state.document.current_generation_id) { state.chunks = await request(`/api/v1/documents/${state.document.id}/chunks`); renderChunks(); } else { $('chunk-list').innerHTML = '<div class="empty-state">解析成功后，Chunk 会显示在这里。</div>'; $('chunk-count').textContent = '0 个'; }
}
function renderChunks() { const node = $('chunk-list'); $('chunk-count').textContent = `${state.chunks.length} 个`; if (!state.chunks.length) { node.innerHTML = '<div class="empty-state">没有 Chunk</div>'; return; } node.innerHTML = state.chunks.map((chunk, index) => `<button class="chunk-item" data-chunk-index="${index}"><strong>#${chunk.chunk_index + 1} · ${chunk.page_start || '?'}-${chunk.page_end || '?'} 页</strong><small>${escapeHtml(chunk.content.slice(0, 90))}</small></button>`).join(''); node.querySelectorAll('[data-chunk-index]').forEach((button) => button.addEventListener('click', () => showChunk(Number(button.dataset.chunkIndex)))); }
function showChunk(index) { const chunk = state.chunks[index]; document.querySelectorAll('.chunk-item').forEach((node, i) => node.classList.toggle('active', i === index)); $('chunk-content').classList.remove('empty-state'); $('chunk-content').textContent = chunk.content; }
async function parseDocument(id, action) { try { await request(`/api/v1/documents/${id}/${action}`, { method: 'POST' }); toast(action === 'reparse' ? '重新解析任务已创建' : '解析任务已创建'); await loadDocuments(); } catch (error) { toast(error.message); } }
async function cancelDocument(id) { try { await request(`/api/v1/documents/${id}/cancel`, { method: 'POST' }); toast('已请求取消解析'); await loadDocuments(); } catch (error) { toast(error.message); } }
async function deleteDocument(id) { if (!window.confirm('删除后该文档将从列表中移除，确定删除吗？')) return; try { await request(`/api/v1/documents/${id}`, { method: 'DELETE' }); toast('文档已删除'); await loadDocuments(); } catch (error) { toast(error.message); } }
function startDocumentPolling() { if (state.poller) return; state.poller = setInterval(async () => { try { await loadDocuments(); } catch (error) { toast(error.message); stopDocumentPolling(); } }, 2500); }
function stopDocumentPolling() { if (state.poller) { clearInterval(state.poller); state.poller = null; } }

async function loadModelSettings() {
  const node = $('model-provider-list'); node.setAttribute('aria-busy', 'true'); node.innerHTML = '<div class="loading-state">正在读取供应商和已保存模型…</div>';
  try { [state.modelCatalog, state.modelConfigurations] = await Promise.all([request('/api/v1/model-providers/catalog'), request('/api/v1/model-providers/configurations')]); renderModelProviders(); } catch (error) { node.innerHTML = `<div class="empty-state">模型配置读取失败：${escapeHtml(error.message)}</div>`; } finally { node.setAttribute('aria-busy', 'false'); }
}
function renderModelProviders() {
  const node = $('model-provider-list');
  if (!state.modelConfigurations.length) { node.innerHTML = '<div class="empty-state model-empty"><strong>还没有保存模型配置</strong><span>选择供应商、填写 API Key 和模型后即可保存。</span><button class="primary-button" id="empty-new-model">新增模型配置</button></div>'; $('empty-new-model').addEventListener('click', () => openModelDialog()); return; }
  node.innerHTML = state.modelConfigurations.map((config) => `<article class="provider-card"><div class="provider-card-heading"><div><p class="eyebrow">${escapeHtml(config.provider_key.toUpperCase())}</p><h2>${escapeHtml(config.display_name)}</h2><p class="muted">${escapeHtml(config.provider_name)} · 配置 ID ${escapeHtml(config.id.slice(0, 8))}</p></div><span class="status-pill ${config.is_default ? 'status-success' : 'status-neutral'}">${config.is_default ? '默认配置' : '已保存'}</span></div><div class="provider-fields"><div><small>Base URL</small><strong>${escapeHtml(config.base_url)}</strong></div><div><small>API Key</small><strong>${escapeHtml(config.api_key_masked || '未设置')}</strong></div><div><small>${config.provider_key === 'rerank' ? 'Rerank' : 'Chat'} 模型</small><strong>${escapeHtml(config.chat_model)}</strong></div><div><small>Embedding 模型</small><strong>${escapeHtml(config.embedding_model || '不使用')}</strong></div><div><small>向量维度</small><strong>${config.embedding_dimension || '—'}</strong></div><div><small>后续流程选择</small><strong>使用配置 ID：${escapeHtml(config.id)}</strong></div></div><div class="provider-capabilities">${(config.capabilities || []).map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</div><div class="provider-actions"><button class="ghost-button" data-test-config="${escapeHtml(config.id)}" data-test-provider="${escapeHtml(config.provider_key)}">测试连接</button><button class="ghost-button" data-edit-config="${escapeHtml(config.id)}">编辑</button>${config.is_default ? '' : `<button class="ghost-button" data-default-config="${escapeHtml(config.id)}">设为默认</button>`}<button class="danger-button" data-delete-config="${escapeHtml(config.id)}">删除</button><span id="config-test-${escapeHtml(config.id)}" class="provider-test-result" role="status">等待测试</span></div></article>`).join('');
  node.querySelectorAll('[data-test-config]').forEach((button) => button.addEventListener('click', () => testModelConfiguration(button.dataset.testConfig, button))); node.querySelectorAll('[data-edit-config]').forEach((button) => button.addEventListener('click', () => openModelDialog(state.modelConfigurations.find((item) => item.id === button.dataset.editConfig)))); node.querySelectorAll('[data-default-config]').forEach((button) => button.addEventListener('click', () => setDefaultModelConfiguration(button.dataset.defaultConfig))); node.querySelectorAll('[data-delete-config]').forEach((button) => button.addEventListener('click', () => deleteModelConfiguration(button.dataset.deleteConfig)));
}
async function testModelConfiguration(id, button) {
  const result = $(`config-test-${id}`); button.disabled = true; result.textContent = '测试中…'; result.className = 'provider-test-result pending';
  try { const response = await request(`/api/v1/model-providers/configurations/${id}/test`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: 'RAG Demo 模型连通性测试' }) }); result.textContent = response.success ? `${response.message} · ${response.vector_dimension || '—'} 维 · ${response.latency_ms || '—'} ms` : response.message; result.className = `provider-test-result ${response.success ? 'success' : 'failure'}`; toast(response.message); } catch (error) { result.textContent = error.message; result.className = 'provider-test-result failure'; toast(error.message); } finally { button.disabled = false; }
}
function setModelDiscoveryStatus(message, kind = '') {
  const status = $('model-discovery-status');
  status.textContent = message;
  status.className = `model-discovery-status ${kind}`.trim();
}
function renderModelChoiceSelect(select, models, selected, { allowEmpty = false, search = '' } = {}) {
  const term = search.trim().toLowerCase();
  const visible = models.filter((item) => `${item.name} ${item.id}`.toLowerCase().includes(term));
  const selectedIsVisible = visible.some((item) => item.id === selected);
  select.innerHTML = `${allowEmpty ? '<option value="">不使用 Embedding</option>' : '<option value="">请选择模型</option>'}`
    + `${selected && !selectedIsVisible && selected !== '__custom__' ? `<option value="${escapeHtml(selected)}">当前选择：${escapeHtml(selected)}</option>` : ''}`
    + visible.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name === item.id ? item.id : `${item.name} · ${item.id}`)}</option>`).join('')
    + '<option value="__custom__">手动输入模型 ID…</option>';
  select.value = selected || '';
}
function renderModelChoices(chat = $('model-chat-model').value, embedding = $('model-embedding-model').value) {
  const qwen = $('model-provider').value === 'qwen';
  renderModelChoiceSelect($('model-chat-model'), qwen ? state.modelChoices.chat_models : [], chat, {
    search: qwen ? $('model-chat-search').value : '',
  });
  renderModelChoiceSelect($('model-embedding-model'), qwen ? state.modelChoices.embedding_models : [], embedding, { allowEmpty: true });
  syncCustomModelInputs();
}
function syncCustomModelInputs() {
  const provider = $('model-provider').value;
  const isRerank = provider === 'rerank';
  const manualChat = $('model-chat-model').value === '__custom__';
  const manualEmbedding = $('model-embedding-model').value === '__custom__';
  const hasEmbedding = !isRerank && Boolean($('model-embedding-model').value);
  $('model-chat-label').textContent = isRerank ? 'Rerank 模型' : 'Chat 模型';
  $('model-chat-custom-field').classList.toggle('hidden', !manualChat);
  $('model-chat-custom').required = manualChat;
  $('model-embedding-fields').classList.toggle('hidden', isRerank);
  $('model-embedding-custom-field').classList.toggle('hidden', !manualEmbedding);
  $('model-embedding-custom').required = manualEmbedding && !isRerank;
  $('model-embedding-dimension-field').classList.toggle('hidden', !hasEmbedding);
  $('model-embedding-dimension').required = hasEmbedding;
  $('model-refresh-models').classList.toggle('hidden', provider !== 'qwen');
  $('model-chat-search-field').classList.toggle('hidden', provider !== 'qwen');
  const choice = state.modelChoices.chat_models.find((item) => item.id === $('model-chat-model').value);
  $('model-chat-detail').textContent = choice?.description || choice?.name || (manualChat ? '可输入目录中未列出的模型 ID。' : '选择模型后会显示目录说明。');
}
function applyProviderPreset(resetFields = true) {
  const provider = state.modelCatalog.find((item) => item.provider_key === $('model-provider').value);
  if (!provider) return;
  state.modelDiscoverySeq += 1;
  state.modelChoices = { chat_models: [], embedding_models: [] };
  if (resetFields) {
    $('model-base-url').value = provider.base_url || '';
    $('model-chat-custom').value = '';
    $('model-embedding-custom').value = '';
    $('model-embedding-dimension').value = '';
    $('model-chat-search').value = '';
  }
  $('model-provider-hint').textContent = provider.capabilities.join(' · ');
  renderModelChoices(resetFields ? (provider.provider_key === 'qwen' ? '' : '__custom__') : $('model-chat-model').value, resetFields ? '' : $('model-embedding-model').value);
  setModelDiscoveryStatus(provider.provider_key === 'qwen' ? '正在准备千问实时模型目录…' : '此供应商请手动输入模型 ID。');
}
async function loadDiscoveredModels() {
  if ($('model-provider').value !== 'qwen') return;
  const sequence = ++state.modelDiscoverySeq;
  const button = $('model-refresh-models');
  button.disabled = true;
  setModelDiscoveryStatus('正在从千问获取模型，请稍候…', 'pending');
  try {
    const result = await request('/api/v1/model-providers/discover-models', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider_key: 'qwen', base_url: $('model-base-url').value.trim(),
        api_key: $('model-api-key').value.trim() || null,
        configuration_id: $('model-config-id').value || null,
      }),
    });
    if (sequence !== state.modelDiscoverySeq || !$('model-config-dialog').open) return;
    state.modelChoices = result;
    renderModelChoices();
    setModelDiscoveryStatus(`已从千问读取 ${result.chat_models.length} 个文本模型、${result.embedding_models.length} 个同步 Embedding 模型。`, 'success');
  } catch (error) {
    if (sequence !== state.modelDiscoverySeq || !$('model-config-dialog').open) return;
    state.modelChoices = { chat_models: [], embedding_models: [] };
    renderModelChoices();
    setModelDiscoveryStatus(`获取失败：${error.message}。你仍可手动输入模型 ID。`, 'failure');
  } finally {
    if (sequence === state.modelDiscoverySeq) button.disabled = false;
  }
}
function markModelChoicesStale() {
  if ($('model-provider').value !== 'qwen') return;
  state.modelDiscoverySeq += 1;
  state.modelChoices = { chat_models: [], embedding_models: [] };
  renderModelChoices();
  $('model-refresh-models').disabled = false;
  setModelDiscoveryStatus('连接信息已更改，请重新获取千问模型目录。', 'pending');
}
function openModelDialog(config = null) {
  state.editingModel = config;
  $('model-dialog-title').textContent = config ? '编辑模型配置' : '新增模型配置';
  $('model-config-id').value = config?.id || '';
  $('model-provider').innerHTML = state.modelCatalog.map((item) => `<option value="${escapeHtml(item.provider_key)}">${escapeHtml(item.display_name)}</option>`).join('');
  $('model-provider').value = config?.provider_key || 'qwen';
  $('model-display-name').value = config?.display_name || '';
  $('model-base-url').value = config?.base_url || '';
  $('model-api-key').value = '';
  $('model-api-key').placeholder = config ? '留空则保留原密钥' : '输入服务商 API Key';
  $('model-set-default').checked = Boolean(config?.is_default);
  $('model-chat-search').value = '';
  applyProviderPreset(!config);
  if (config) {
    $('model-chat-custom').value = config.provider_key === 'qwen' ? '' : config.chat_model;
    $('model-embedding-custom').value = config.provider_key === 'qwen' ? '' : config.embedding_model || '';
    $('model-embedding-dimension').value = config.embedding_dimension || '';
    renderModelChoices(config.provider_key === 'qwen' ? config.chat_model : '__custom__', config.provider_key === 'qwen' ? config.embedding_model || '' : (config.embedding_model ? '__custom__' : ''));
  }
  $('model-config-dialog').showModal();
  if ($('model-provider').value === 'qwen') loadDiscoveredModels();
}
function modelValue(selectId, customId) {
  const select = $(selectId);
  return select.value === '__custom__' ? $(customId).value.trim() : select.value;
}
async function saveModelConfiguration(event) {
  event.preventDefault(); if (event.submitter?.value === 'cancel') { $('model-config-dialog').close(); return; } const id = $('model-config-id').value; const embeddingModel = $('model-provider').value === 'rerank' ? null : modelValue('model-embedding-model', 'model-embedding-custom') || null; const payload = { provider_key: $('model-provider').value, display_name: $('model-display-name').value.trim(), base_url: $('model-base-url').value.trim(), api_key: $('model-api-key').value || null, chat_model: modelValue('model-chat-model', 'model-chat-custom'), embedding_model: embeddingModel, embedding_dimension: embeddingModel ? Number($('model-embedding-dimension').value) || null : null, set_default: $('model-set-default').checked };
  if (!id && !payload.api_key) { toast('请填写 API Key'); return; }
  if (!payload.chat_model) { toast('请选择或输入模型 ID'); return; }
  try { await request(id ? `/api/v1/model-providers/configurations/${id}` : '/api/v1/model-providers/configurations', { method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }); $('model-config-dialog').close(); toast(id ? '模型配置已更新' : '模型配置已保存'); await loadModelSettings(); } catch (error) { toast(error.message); }
}
async function setDefaultModelConfiguration(id) { const config = state.modelConfigurations.find((item) => item.id === id); if (!config) return; try { await request(`/api/v1/model-providers/configurations/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_key: config.provider_key, display_name: config.display_name, base_url: config.base_url, api_key: null, chat_model: config.chat_model, embedding_model: config.embedding_model, embedding_dimension: config.embedding_dimension, set_default: true }) }); toast('默认模型已切换'); await loadModelSettings(); } catch (error) { toast(error.message); } }
async function deleteModelConfiguration(id) { if (!window.confirm('删除后该配置将不能再被后续流程选择，确定删除吗？')) return; try { await request(`/api/v1/model-providers/configurations/${id}`, { method: 'DELETE' }); toast('模型配置已删除'); await loadModelSettings(); } catch (error) { toast(error.message); } }
function formatDate(value) { return value ? new Date(value).toLocaleString() : '—'; }
function escapeHtml(value) { return String(value).replace(/[&<>'"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c])); }

document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => {
  if (button.dataset.adminOnly && !window.BusinessAuth?.isAdmin()) return;
  showView(button.dataset.view);
  document.dispatchEvent(new CustomEvent('view:changed', { detail: { view: button.dataset.view } }));
  if (button.dataset.view === 'models' && !state.modelCatalog.length) loadModelSettings();
  if (button.dataset.view === 'knowledge' && window.BusinessAuth?.isAdmin()) loadKnowledgeBases().catch((error) => toast(error.message));
}));
$('new-kb-button').addEventListener('click', () => $('new-kb-dialog').showModal()); $('refresh-kb').addEventListener('click', () => loadKnowledgeBases().catch((error) => toast(error.message))); $('refresh-models').addEventListener('click', () => loadModelSettings()); $('new-model-button').addEventListener('click', () => openModelDialog()); $('model-config-form').addEventListener('submit', saveModelConfiguration);
$('model-provider').addEventListener('change', () => { applyProviderPreset(true); if ($('model-provider').value === 'qwen') loadDiscoveredModels(); });
$('model-base-url').addEventListener('input', markModelChoicesStale);
$('model-api-key').addEventListener('input', markModelChoicesStale);
$('model-refresh-models').addEventListener('click', loadDiscoveredModels);
$('model-chat-search').addEventListener('input', () => renderModelChoices());
$('model-chat-model').addEventListener('change', syncCustomModelInputs);
$('model-embedding-model').addEventListener('change', () => { $('model-embedding-dimension').value = ''; syncCustomModelInputs(); });
$('model-config-dialog').addEventListener('close', () => { state.modelDiscoverySeq += 1; });
$('rename-kb-form').addEventListener('submit', async (event) => { event.preventDefault(); if (event.submitter?.value === 'cancel') { $('rename-kb-dialog').close(); state.editingKnowledgeBase = null; return; } const kb = state.editingKnowledgeBase; if (!kb) return; try { await request(`/api/v1/knowledge-bases/${encodeURIComponent(kb.id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('rename-kb-name').value }) }); $('rename-kb-dialog').close(); state.editingKnowledgeBase = null; toast('知识库名称已修改'); await loadKnowledgeBases(); } catch (error) { toast(error.message); } });
$('delete-selected-kbs').addEventListener('click', () => deleteKnowledgeBases([...state.selectedKnowledgeBaseIds]));
$('new-kb-form').addEventListener('submit', async (event) => { event.preventDefault(); try { await request('/api/v1/knowledge-bases', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('kb-name').value, description: $('kb-description-input').value || null }) }); $('new-kb-dialog').close(); $('kb-name').value = ''; $('kb-description-input').value = ''; toast('知识库已创建'); await loadKnowledgeBases(); } catch (error) { toast(error.message); } }); $('document-upload').addEventListener('change', uploadDocument); $('back-to-kbs').addEventListener('click', () => { $('kb-detail').classList.add('hidden'); $('kb-list-card').classList.remove('hidden'); state.knowledgeBase = null; }); $('back-to-documents').addEventListener('click', () => { $('document-detail').classList.add('hidden'); $('kb-detail').classList.remove('hidden'); loadDocuments(); });
document.addEventListener('auth:unauthorized', () => { stopDocumentPolling(); state.knowledgeBases = []; state.documents = []; state.chunks = []; });
window.AdminViews = { loadKnowledgeBases, loadModelSettings, stopDocumentPolling };

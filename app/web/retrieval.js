/* Independent developer experiment UI. Provider output is always rendered as text. */
(() => {
  const el = (id) => document.getElementById(`retrieval-${id}`);
  const api = '/api/v1/retrieval-experiments';
  let current = null;
  let busy = false;
  let documentLoad = 0;
  const status = (text, failed = false) => {
    el('status').textContent = text;
    el('status').classList.toggle('retrieval-error', failed);
  };
  async function fetchJSON(url, options) {
    return window.apiRequest(url, options);
  }
  const post = (url, body) => fetchJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  function setOptions(select, items, placeholder) {
    select.replaceChildren(new Option(placeholder, ''));
    items.forEach(([value, label]) => select.add(new Option(label, value)));
  }
  async function loadDocuments() {
    const epoch = ++documentLoad;
    const kb = el('kb').value;
    setOptions(el('doc'), [], '全部已发布文档');
    if (!kb) return;
    const docs = await fetchJSON(`/api/v1/knowledge-bases/${encodeURIComponent(kb)}/documents`);
    if (epoch !== documentLoad) return;
    setOptions(el('doc'), docs.map((d) => [d.id, d.filename]), '全部已发布文档');
  }
  async function load() {
    if (busy) return;
    el('submit').disabled = true;
    try {
      await fetchJSON(`${api}/session`);
      const [kbs, models] = await Promise.all([
        fetchJSON('/api/v1/knowledge-bases'), fetchJSON('/api/v1/model-providers/configurations'),
      ]);
      const previous = el('kb').value;
      setOptions(el('kb'), kbs.map((k) => [k.id, k.name]), '请选择知识库');
      if (kbs.some((k) => k.id === previous)) el('kb').value = previous;
      else if (kbs.length === 1) el('kb').value = kbs[0].id;
      setOptions(el('rerank'), models.filter((m) => m.capabilities.includes('Rerank')).map((m) => [m.id, m.display_name]), '自动选择可用配置');
      await loadDocuments();
      if (!current) status(kbs.length ? '配置已读取，可以开始检索。' : '还没有知识库，请先上传文档并完成离线建库。');
    } catch (error) { status(error.message, true); }
    finally { el('submit').disabled = false; }
  }
  const num = (id) => Number(el(id).value);
  function payload() {
    return { query: el('query').value, knowledge_base_id: el('kb').value, document_id: el('doc').value || null,
      top_k: num('k'), top_n: num('n'), threshold: num('threshold'), model_weight: num('weight'),
      title_weight: num('title-weight'), content_weight: num('content-weight'), scoring_mode: el('mode').value,
      rerank_config_id: el('mode').value === 'vector' ? null : el('rerank').value || null };
  }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function button(label, action) {
    const element = node('button', label, 'ghost-button');
    element.type = 'button'; element.addEventListener('click', action); return element;
  }
  const score = (value) => value == null ? '—' : Number(value).toFixed(4);
  function render(report) {
    current = report;
    const root = node('div', undefined, 'retrieval-report');
    const head = node('section', undefined, 'card');
    head.append(node('h2', report.status === 'SUCCESS' ? '检索执行完成' : '检索执行失败'));
    head.append(node('p', `${report.mode} · 候选 ${report.candidate_count} · 最终 ${report.result_count} · ${report.persisted ? '快照已保存' : '快照未保存'}`));
    head.append(node('p', `实验 ${report.experiment_id}`, 'muted'));
    const actions = node('div', undefined, 'retrieval-actions');
    actions.append(button('导出诊断 JSON', () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = `retrieval-${report.experiment_id}.json`; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    if (report.fallback_available) actions.append(button('复用候选，降级到向量评分', () => execute(() => post(`${api}/${report.experiment_id}/fallback`, {}), '正在使用原候选计算向量分数…')));
    if (report.parent_experiment_id) actions.append(button('查看原失败实验', () => execute(() => fetchJSON(`${api}/${report.parent_experiment_id}`), '正在读取原实验…')));
    head.append(actions); root.append(head);
    const selected = report.candidates.filter((r) => r.disposition === 'FINAL_SELECTED');
    const results = node('section', undefined, 'card'); results.append(node('h2', '最终 Top N'));
    if (!selected.length) results.append(node('p', report.status === 'FAILED' ? '当前实验失败，候选不作为最终结果。' : report.candidate_count ? '所有候选均低于阈值，没有最终结果。' : '当前范围未召回候选。'));
    selected.forEach((r) => {
      const article = node('article', undefined, 'retrieval-candidate');
      article.append(node('strong', `#${r.final_rank} ${r.title} · ${score(r.final_score)}`), node('p', r.content));
      article.append(node('small', `Chunk ${r.chunk_id} · Run ${r.run_id}`, 'muted')); results.append(article);
    }); root.append(results);
    const candidates = node('section', undefined, 'card'); candidates.append(node('h2', '全部候选与筛选原因'));
    const scroll = node('div', undefined, 'retrieval-table');
    const table = node('table');
    const header = node('tr');
    ['初始排名', '标题', '初始 ES 分', '标题覆盖', '正文覆盖', '关键词分', 'Cosine', 'Rerank', '关键词贡献', '模型贡献', '最终分', '处理结果'].forEach((t) => header.append(node('th', t)));
    const thead = node('thead'); thead.append(header); table.append(thead); const tbody = node('tbody');
    const reasons = { FINAL_SELECTED: '入选', BELOW_THRESHOLD: '低于阈值', OUTSIDE_TOP_N: '超出 Top N', NOT_SCORED: '评分未完成' };
    report.candidates.forEach((r) => {
      const tr = node('tr'); tr.dataset.selected = String(r.disposition === 'FINAL_SELECTED');
      [r.initial_rank, r.title, score(r.initial_score), score(r.title_score), score(r.content_score), score(r.token_score), score(r.cosine_score), score(r.rerank_score), score(r.keyword_contribution), score(r.model_contribution), score(r.final_score), reasons[r.disposition] || r.disposition].forEach((v) => tr.append(node('td', v)));
      tbody.append(tr);
    }); table.append(tbody); scroll.append(table); candidates.append(scroll);
    report.candidates.forEach((r) => {
      const details = node('details', undefined, 'retrieval-candidate'); details.append(node('summary', `#${r.initial_rank} ${r.chunk_id} · 正文与分词详情`));
      details.append(node('p', r.content), node('pre', JSON.stringify(r, null, 2))); candidates.append(details);
    }); root.append(candidates);
    const diagnostics = node('details', undefined, 'card'); diagnostics.append(node('summary', '阶段耗时、查询词、有效版本与参数'));
    const { candidates: ignored, ...metadata } = report;
    diagnostics.append(node('pre', JSON.stringify(metadata, null, 2))); root.append(diagnostics);
    el('result').replaceChildren(root);
    status(report.error ? `${report.error.stage}：${report.error.message}` : `检索已完成；分数通过规则不代表能够回答问题，需要人工检查。${report.warnings.join('；')}`, Boolean(report.error));
  }
  async function execute(action, message) {
    if (busy) return;
    busy = true; el('submit').disabled = true; el('refresh').disabled = true;
    el('result').querySelectorAll('button').forEach((b) => { b.disabled = true; });
    el('result').setAttribute('aria-busy', 'true'); status(message);
    try { render(await action()); } catch (error) { status(error.message, true); }
    finally {
      busy = false; el('submit').disabled = false; el('refresh').disabled = false;
      el('result').querySelectorAll('button').forEach((b) => { b.disabled = false; });
      el('result').setAttribute('aria-busy', 'false');
    }
  }
  el('form').addEventListener('submit', (event) => { event.preventDefault(); const body = payload(); execute(() => post(api, body), '正在召回与评分，请等待…'); });
  el('kb').addEventListener('change', () => loadDocuments().catch((e) => status(e.message, true)));
  el('mode').addEventListener('change', () => { el('rerank').disabled = el('mode').value === 'vector'; });
  el('refresh').addEventListener('click', load);
  document.querySelector('[data-view="retrieval"]').addEventListener('click', load);
})();

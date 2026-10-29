'use strict';
// All document/model text is rendered through textContent or form values.
const $ = id => document.getElementById(id);
const state = {token: sessionStorage.getItem('rag-token') || '', access: sessionStorage.getItem('rag-access') || 'admin', kbs: [], kb: null, documents: [], document: null, releases: [], release: null, capabilities: {}, ruleIndex: 0, editor: 'description', dirty: false};
const statusNames = {pending: '待审核', approved: '已通过', rejected: '已退回', queued: '排队中', processing: '处理中', succeeded: '已完成', failed: '处理失败', ready: '已解析', description: '说明', rule: '规则', uncertain: '待分类'};
const clone = value => JSON.parse(JSON.stringify(value));
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = String(text); if (cls) node.className = cls; return node; }
function empty(node, text) { node.replaceChildren(el('div', text, 'empty')); }
function notice(message, error = false) { $('notice').textContent = message; $('notice').className = 'notice' + (error ? ' error' : ''); }
function errorMessage(error) { return error?.message || String(error); }
function action(fn) { return async event => { event?.preventDefault(); try { await fn(event); } catch (error) { notice(errorMessage(error), true); } }; }
function badge(text, type = '') { return el('span', statusNames[text] || text || '待审核', 'badge ' + type); }
function reviewBadge(status) { return badge(status || 'pending', status === 'approved' ? 'ok' : status === 'rejected' ? 'error' : 'warn'); }
function button(text, callback, cls = '') { const node = el('button', text, cls); node.type = 'button'; node.addEventListener('click', action(callback)); return node; }
function field(label, input) { const node = el('label', label); node.append(input); return node; }
function input(value = '', type = 'text') { const node = el('input'); node.type = type; node.value = value ?? ''; return node; }
function area(value, rows = 3) { const node = el('textarea'); node.value = value || ''; node.rows = rows; return node; }
function option(value, text) { const node = el('option', text); node.value = value; return node; }
function select(options, value) { const node = el('select'); for (const [key, label] of options) node.append(option(key, label)); node.value = value; return node; }
function arrayResult(value) { return Array.isArray(value) ? value : value.items || value.documents || value.releases || value.knowledge_bases || []; }
function currentKb() { if (!state.kb) throw new Error('请先选择知识库。'); return state.kb; }
function requireAdmin() { if (state.access !== 'admin') throw new Error('当前为只读查询模式，请使用管理令牌连接。'); }
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set('Authorization', 'Bearer ' + state.token);
  let body = options.body;
  if (body && !(body instanceof FormData)) { headers.set('Content-Type', 'application/json'); body = JSON.stringify(body); }
  const response = await fetch('/api/v1' + path, {...options, body, headers});
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || payload.error || payload.message;
    const message = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : '请求失败（' + response.status + '）';
    throw new Error(response.status === 401 || response.status === 403 ? '访问未获授权，请检查令牌及访问方式。' : message);
  }
  return payload;
}
function setAccess() { document.querySelectorAll('.admin-action, .admin-form button, .admin-form input').forEach(node => { node.disabled = state.access !== 'admin'; }); }
function showTab(name) {
  $('page-name').textContent = {documents:'文档管理', review:'审核工作台', versions:'发布与版本', search:'检索与问答'}[name] || '文档管理';
  document.querySelectorAll('[data-tab]').forEach(node => { const active = node.dataset.tab === name; node.classList.toggle('active', active); node.setAttribute('aria-selected', String(active)); });
  document.querySelectorAll('.tab-panel').forEach(node => node.classList.toggle('hidden', node.id !== 'tab-' + name));
}
function deviceOptions() { const types = state.capabilities.device_types || {motor: '电机', coupling: '联轴器', pump: '水泵', fan: '风机', gearbox: '齿轮箱'}; return Array.isArray(types) ? types.map(key => [key, key]) : Object.entries(types).map(([key, value]) => [key, typeof value === 'string' ? value : value.name || key]); }
function metricRegistry() {
  const result = {};
  for (const [key, value] of Object.entries({...state.capabilities.metrics, ...state.capabilities.derived})) result[key] = Array.isArray(value) ? {name: value[0], unit: value[1]} : typeof value === 'string' ? {name: value, unit: '1'} : value;
  return Object.keys(result).length ? result : {kurtosis: {name: '非超额峭度', unit: '1'}, velocity_rms: {name: '速度有效值', unit: 'mm/s'}, low_frequency_impact: {name: '低频冲击值', unit: 'demo_index'}, crest_factor: {name: '峰值因子', unit: '1'}};
}
function expression(node) {
  if (!node || typeof node !== 'object') return '尚未填写判断条件';
  if (node.all || node.any) return '(' + (node.all || node.any).map(expression).join(node.all ? ' 且 ' : ' 或 ') + ')';
  const metric = metricRegistry()[node.metric] || {name: node.metric || '未选指标', unit: ''};
  const symbols = {ge: '≥', gt: '>', le: '≤', lt: '<'};
  return metric.name + ' ' + ['ge', 'gt', 'le', 'lt'].filter(key => key in node).map(key => symbols[key] + ' ' + node[key]).join(' 且 ') + (metric.unit && metric.unit !== '1' ? ' ' + metric.unit : '');
}
async function load() {
  if (!state.token) throw new Error('请先输入访问令牌。');
  const [kbs, caps] = await Promise.all([api(state.access === 'admin' ? '/admin/knowledge-bases' : '/knowledge-bases'), api('/capabilities')]);
  state.kbs = arrayResult(kbs); state.capabilities = caps;
  const previous = state.kb;
  $('kb-select').replaceChildren(option('', '选择知识库'), ...state.kbs.map(kb => option(kb.id, kb.name + (kb.archived ? '（已归档）' : ''))));
  state.kb = state.kbs.some(kb => kb.id === previous) ? previous : state.kbs[0]?.id || null;
  $('kb-select').value = state.kb || '';
  $('query-device').replaceChildren(option('', '所有类型'), ...deviceOptions().map(([key, name]) => option(key, name)));
  $('connection-state').textContent = state.access === 'admin' ? '管理与审核' : '只读查询'; $('connection-state').className = 'badge ok';
  setAccess(); await loadKb();
  try { const health = await api('/health'); const profile = health.embedding || health.profile || health.embedding_profile || {}; $('service-model').textContent = '向量模式：' + (profile.mode || health.embedding_mode || '按服务配置') + (profile.model ? ' · ' + profile.model : ''); } catch (_) { $('service-model').textContent = '服务已连接'; }
}
async function loadKb() {
  state.document = null; state.release = null; state.documents = []; state.releases = [];
  const kb = state.kbs.find(item => item.id === state.kb);
  $('kb-summary').textContent = kb ? '当前发布版本：' + (kb.current_release || '尚未发布') : '可先创建一个知识库。';
  $('archive-kb').textContent = kb?.archived ? '恢复当前知识库' : '归档当前知识库';
  $('review-body').classList.add('hidden'); $('review-empty').classList.remove('hidden');
  $('query-release').replaceChildren(option('', '当前发布版本')); $('restore').classList.add('hidden');
  empty($('search-results'), '输入问题，查看已发布知识及引用。');
  if (!state.kb) { empty($('document-list'), '尚无知识库，请先创建。'); empty($('release-list'), '暂无发布记录。'); return; }
  if (state.access === 'admin') { await Promise.all([loadDocuments(), loadReleases()]); }
  else { empty($('document-list'), '只读模式可在“检索与问答”查询已发布内容。'); empty($('release-list'), '发布管理需要管理令牌。'); showTab('search'); }
}
async function loadDocuments() {
  state.documents = arrayResult(await api('/admin/documents?kb_id=' + encodeURIComponent(currentKb())));
  $('document-count').textContent = state.documents.length + ' 份文档';
  $('review-document').replaceChildren(option('', '请选择文档'), ...state.documents.map(doc => option(doc.id, doc.title + ' · V' + doc.latest_revision)));
  const list = $('document-list'); list.replaceChildren(); list.classList.remove('empty');
  if (!state.documents.length) return empty(list, '还没有文档。上传一份资料开始构建知识库。');
  renderDocuments();
}
function renderDocuments() {
  const list = $('document-list'); list.replaceChildren();
  const term = $('document-filter').value.trim().toLowerCase();
  const docs = state.documents.filter(doc => (doc.title + ' ' + (doc.external_id || '')).toLowerCase().includes(term));
  if (!docs.length) return empty(list, term ? '没有匹配的文档。' : '还没有文档，点击“上传文档”开始。');
  const scroll = el('div', null, 'table-scroll'); const table = el('table', null, 'document-table');
  const head = el('thead'); const headings = el('tr'); ['文档名称', '处理与审核', '操作'].forEach(text => headings.append(el('th', text))); head.append(headings); const body = el('tbody'); table.append(head, body); scroll.append(table); list.append(scroll);
  for (const doc of docs) {
    const row = el('tr');
    const title = el('td'); title.append(el('div', doc.title, 'doc-title'), el('small', '修订 V' + doc.latest_revision + (doc.external_id ? ' · 外部编号 ' + doc.external_id : '')));
    const status = el('td'); status.append(badge(doc.disabled ? '待停用／已停用' : statusNames[doc.status] || doc.status || '待审核', doc.disabled ? 'warn' : ''), el('small', '说明：' + (statusNames[doc.description_status] || doc.description_status || '待审核')));
    const actions = el('div', null, 'doc-actions');
    actions.append(button('查看与审核', async () => { await openDocument(doc.id); showTab('review'); }));
    const file = input('', 'file'); file.accept = '.md,.docx,.pdf'; file.className = 'file-input'; file.setAttribute('aria-label', '选择替换 ' + doc.title + ' 的文件');
    file.addEventListener('change', action(async () => { if (!file.files.length) return; const body = new FormData(); body.append('file', file.files[0]); await waitTask(await api('/admin/documents/' + encodeURIComponent(doc.id) + '/revisions', {method: 'POST', body}), '处理文档新版本'); await loadDocuments(); }));
    actions.append(button('替换', () => file.click(), 'quiet'), file, button(doc.disabled ? '恢复使用' : '停用', async () => { await api('/admin/documents/' + encodeURIComponent(doc.id) + '/disable', {method: 'POST', body: {disabled: !doc.disabled}}); notice('已更新文档状态，请到“发布与版本”发布后对外生效。'); await loadDocuments(); }, doc.disabled ? '' : 'danger'));
    const actionCell = el('td'); actionCell.append(actions); row.append(title, status, actionCell); body.append(row);
  }
}
async function waitTask(response, title) {
  if (!response.task_id) { notice(title + '已完成。'); return response; }
  $('task-box').classList.remove('hidden'); $('task-title').textContent = title; showTab('documents');
  let attempt = 0;
  while (attempt++ < 900) {
    const task = await api('/admin/tasks/' + encodeURIComponent(response.task_id));
    $('task-detail').textContent = (statusNames[task.status] || task.status || '') + ' · ' + (task.stage || '') + ' · 任务 ' + response.task_id;
    if (task.status === 'succeeded') { $('task-title').textContent = title + '已完成'; notice(title + '已完成。'); return task.result; }
    if (task.status === 'failed') { $('task-title').textContent = title + '失败'; throw new Error(typeof task.error === 'string' ? task.error : '任务未完成，请检查服务配置后重试。'); }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
  throw new Error('任务仍在处理中，可稍后刷新查看。任务编号：' + response.task_id);
}
async function openDocument(id) {
  if (state.dirty && !confirm('当前文档有未保存的修改，确定切换文档？')) { $('review-document').value = state.document.id; return; }
  state.document = await api('/admin/documents/' + encodeURIComponent(id)); state.ruleIndex = 0; blockPage = 0; $('block-search').value = ''; $('block-kind').value = '';
  $('review-document').value = id; renderReview();
}
function renderReview() {
  const doc = state.document; if (!doc) return;
  state.dirty = false;
  doc.payload ||= {blocks: [], rules: []}; doc.payload.blocks ||= []; doc.payload.rules ||= [];
  $('review-body').classList.remove('hidden'); $('review-empty').classList.add('hidden');
  $('review-meta').replaceChildren(el('strong', doc.title), badge('文档 V' + doc.latest_revision), reviewBadge(doc.description_status), el('span', '编辑版本 ' + doc.edit_version, 'muted'));
  $('review-meta').append(button('下载原文', async () => {
    const response = await fetch('/api/v1/admin/documents/' + encodeURIComponent(doc.id) + '/original', {headers: {Authorization: 'Bearer ' + state.token}});
    if (!response.ok) throw new Error('原文下载失败，请检查访问权限。');
    const url = URL.createObjectURL(await response.blob()); const anchor = el('a'); anchor.href = url; anchor.download = doc.file_name || doc.title; document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, 'quiet'), button('查看审核记录', async () => {
    const rows = arrayResult(await api('/admin/documents/' + encodeURIComponent(doc.id) + '/audits'));
    const box = el('div', null, 'warning-list'); box.append(el('strong', '审核记录'));
    if (!rows.length) box.append(el('p', '暂无审核记录。'));
    for (const row of rows) box.append(el('p', [row.created_at, row.target || row.target_id, statusNames[row.decision] || row.decision, row.note].filter(Boolean).join(' · ')));
    $('review-meta').append(box);
  }, 'quiet'));
  renderDescription(); renderRules(); setEditor(state.editor);
}
function setEditor(name) { state.editor = name; $('description-editor').classList.toggle('hidden', name !== 'description'); $('rules-editor').classList.toggle('hidden', name !== 'rules'); $('edit-description').classList.toggle('active', name === 'description'); $('edit-rules').classList.toggle('active', name === 'rules'); }
async function saveDocument() {
  const doc = state.document; requireAdmin();
  state.document = await api('/admin/documents/' + encodeURIComponent(doc.id), {method: 'PATCH', body: {expected_version: doc.edit_version, blocks: doc.payload.blocks, rules: doc.payload.rules}});
  renderReview(); notice('草稿已保存。修改内容需重新审核；已发布版本仍保持原样。'); await loadDocuments(); $('review-document').value = doc.id;
}
async function review(target, decision, note, ruleId) {
  const doc = state.document; requireAdmin();
  if (state.dirty) throw new Error('有尚未保存的编辑，请先“保存修改”，核对保存后的内容，再进行审核。');
  state.document = await api('/admin/documents/' + encodeURIComponent(doc.id) + '/review', {method: 'POST', body: {expected_version: doc.edit_version, target, decision, note: note || '', ...(ruleId ? {rule_id: ruleId} : {})}});
  renderReview(); notice('已记录审核结果。发布后对外生效。');
}
function auditActions(container, target, ruleId) {
  const note = area('', 2); const noteField = field('审核意见（选填）', note); noteField.className = 'audit-note'; container.append(noteField);
  const actions = el('div', null, 'audit-actions');
  actions.append(button('保存修改', saveDocument), button('审核通过', () => review(target, 'approved', note.value, ruleId), 'primary'), button('退回修改', () => review(target, 'rejected', note.value, ruleId), 'danger'));
  container.append(actions, el('small', '先保存修改，再审核保存后的内容；未保存的编辑不会随审核提交。'));
}
let blockPage = 0;
const blocksPerPage = 6;
function visibleBlocks() {
  const term = $('block-search').value.trim().toLowerCase(); const kind = $('block-kind').value;
  const blocks = state.document.payload.blocks.filter(block => (!kind || block.kind === kind) && (!term || (block.text + ' ' + block.locator + ' ' + block.id).toLowerCase().includes(term)));
  const pages = Math.max(1, Math.ceil(blocks.length / blocksPerPage)); blockPage = Math.min(blockPage, pages - 1);
  $('block-count').textContent = blocks.length + ' 块 · 第 ' + (blockPage + 1) + ' / ' + pages + ' 页';
  $('blocks-prev').disabled = blockPage === 0; $('blocks-next').disabled = blockPage >= pages - 1;
  return blocks.slice(blockPage * blocksPerPage, (blockPage + 1) * blocksPerPage);
}
function textDisclosure(text, label = '展开完整内容') {
  const details = el('details', null, 'text-details'); details.append(el('summary', label), el('pre', text, 'preserve-lines')); return details;
}
function renderDescription() {
  const root = $('description-editor'); root.replaceChildren();
  root.append(el('p', '核对整篇说明的完整性与来源。规则段、待分类段不会作为说明发布。', 'muted'));
  const warnings = state.document.payload.warnings || [];
  if (warnings.length) { const box = el('div', null, 'warning-list'); warnings.forEach(text => box.append(el('p', text))); root.append(box); }
  const source = $('source-blocks'); source.replaceChildren();
  const blocks = visibleBlocks();
  if (!blocks.length) { empty(source, '没有匹配的原文块。'); root.append(el('p', '没有匹配的内容。', 'muted')); }
  for (const block of blocks) {
    const row = el('div', null, 'source-block' + (block.kind !== 'description' ? ' quarantined' : ''));
    const locator = el('div', null, 'locator'); locator.append(el('span', block.id + ' · ' + block.locator), badge(block.kind, block.kind === 'description' ? '' : 'warn'));
    const raw = block.source_text || block.text;
    row.append(locator, el('p', raw.slice(0, 140) + (raw.length > 140 ? '…' : ''), 'block-preview'), textDisclosure(raw, '查看完整原文')); source.append(row);
    const editor = el('details', null, 'block-editor');
    const summary = el('summary'); summary.append(el('span', block.id + ' · ' + block.locator), badge(block.kind));
    editor.append(summary, el('p', block.text.slice(0, 100) + (block.text.length > 100 ? '…' : ''), 'block-preview'));
    const heading = el('div', null, 'row');
    const kind = select([['description', '说明内容'], ['rule', '专业规则段'], ['uncertain', '待分类／待修正']], block.kind);
    kind.addEventListener('change', () => { block.kind = kind.value; });
    heading.append(el('span', block.id + ' · ' + block.locator, 'muted'), kind);
    const text = area(block.text, Math.min(7, Math.max(2, Math.ceil((block.text || '').length / 70)))); text.addEventListener('input', () => { block.text = text.value; });
    editor.append(heading, field('编辑内容', text)); root.append(editor);
  }
  auditActions(root, 'description');
}
function defaultLeaf() { return {metric: 'kurtosis', ge: 3.5}; }
function renderCondition(node, onChange, onRemove) {
  if (!node || typeof node !== 'object') node = defaultLeaf();
  if (node.all || node.any) {
    const root = el('div', null, 'condition-group'); const op = node.all ? 'all' : 'any'; const children = node[op];
    const header = el('div', null, 'row'); const operator = select([['all', '且：全部满足'], ['any', '或：任一满足']], op);
    operator.addEventListener('change', () => { delete node[op]; node[operator.value] = children; onChange(); });
    const controls = el('div', null, 'row'); controls.append(button('+ 条件', () => { children.push(defaultLeaf()); onChange(); }), button('+ 条件组', () => { children.push({all: [defaultLeaf()]}); onChange(); }));
    if (onRemove) controls.append(button('删除组', onRemove, 'danger'));
    header.append(operator, controls); root.append(header);
    children.forEach((child, i) => root.append(renderCondition(child, onChange, () => { children.splice(i, 1); if (!children.length) children.push(defaultLeaf()); onChange(); })));
    return root;
  }
  const root = el('div', null, 'condition-leaf'); const registry = metricRegistry(); const metric = select(Object.entries(registry).map(([key, value]) => [key, value.name || value.label || key]), node.metric);
  if (!registry[node.metric]) { metric.append(option(node.metric || '', node.metric || '请选择指标')); metric.value = node.metric || ''; }
  metric.addEventListener('change', () => { node.metric = metric.value; delete node.label; onChange(); });
  const top = el('div', null, 'metric-row'); top.append(metric, badge(registry[node.metric]?.unit || '单位待确认'));
  if (onRemove) top.append(button('删除', onRemove, 'danger')); root.append(top);
  const limits = el('div', null, 'condition-limits');
  for (const ops of [['ge', 'gt'], ['le', 'lt']]) {
    const current = ops.find(key => key in node) || '';
    const picker = select([['', ops[0] === 'ge' ? '无下限' : '无上限'], ...ops.map(key => [key, {ge: '≥', gt: '>', le: '≤', lt: '<'}[key]])], current);
    const number = input(current ? node[current] : '', 'number'); number.step = 'any'; number.disabled = !current; number.setAttribute('aria-label', ops[0] === 'ge' ? '下限阈值' : '上限阈值');
    picker.addEventListener('change', () => { const value = Number(number.value || 0); ops.forEach(key => delete node[key]); if (picker.value) node[picker.value] = value; delete node.label; onChange(); });
    number.addEventListener('input', () => {
      if (picker.value) node[picker.value] = number.value === '' ? null : Number(number.value);
      delete node.label; state.dirty = true;
      const preview = $('rules-editor').querySelector('.condition-preview');
      if (preview) preview.textContent = expression(state.document.payload.rules[state.ruleIndex].condition);
    });
    number.addEventListener('change', () => { if (picker.value) node[picker.value] = number.value === '' ? null : Number(number.value); delete node.label; onChange(); });
    limits.append(picker, number);
  }
  root.append(limits); return root;
}
function renderRules() {
  const root = $('rules-editor'); root.replaceChildren(); const rules = state.document.payload.rules;
  const toolbar = el('div', null, 'rule-toolbar');
  const picker = select(rules.map((rule, i) => [String(i), (rule.name || '未命名规则') + ' · ' + (statusNames[rule.review_status] || '待审核')]), String(Math.min(state.ruleIndex, Math.max(0, rules.length - 1))));
  picker.addEventListener('change', () => { state.ruleIndex = Number(picker.value); renderRules(); });
  toolbar.append(picker, button('+ 新增规则', () => { const id = globalThis.crypto?.randomUUID?.() || Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10); rules.push({id: 'draft-' + id, name: '', device_type: 'motor', original_text: '', condition: {all: [defaultLeaf()]}, phenomena: [], causes: [], actions: [], notes: [], review_notes: [], block_ids: [], review_status: 'pending'}); state.ruleIndex = rules.length - 1; state.dirty = true; renderRules(); })); root.append(toolbar);
  if (!rules.length) { root.append(el('p', '没有提取到专业规则。待分类段如含规则，可新增条目并关联原文块。', 'muted')); return; }
  state.ruleIndex = Math.min(state.ruleIndex, rules.length - 1); const rule = rules[state.ruleIndex];
  const status = el('div', null, 'row'); status.append(reviewBadge(rule.review_status), el('span', rule.id || '新规则', 'muted')); root.append(status);
  const fields = el('div', null, 'rule-title-grid'); const name = input(rule.name); name.addEventListener('input', () => { rule.name = name.value; });
  const dtype = select(deviceOptions(), rule.device_type); dtype.addEventListener('change', () => { rule.device_type = dtype.value; }); fields.append(field('规则名称', name), field('适用设备', dtype)); root.append(fields);
  const original = area(rule.original_text, 3); original.addEventListener('input', () => { rule.original_text = original.value; }); root.append(field('原文判据', original));
  root.append(el('h3', '执行条件'));
  if (rule.units) root.append(el('p', '当前记录单位：' + Object.entries(rule.units).map(([key, unit]) => (metricRegistry()[key]?.name || key) + ' / ' + unit).join('；'), 'muted'));
  const condition = el('div'); const preview = el('div', null, 'condition-preview');
  const syncUnits = node => { if (node.metric) return {[node.metric]: metricRegistry()[node.metric]?.unit || '1'}; return Object.assign({}, ...(node.all || node.any || []).map(syncUnits)); };
  const changedCondition = () => { state.dirty = true; rule.units = syncUnits(rule.condition); drawCondition(); };
  const drawCondition = () => { condition.replaceChildren(renderCondition(rule.condition, changedCondition)); preview.textContent = expression(rule.condition); };
  if (!rule.condition) rule.condition = {all: [defaultLeaf()]}; drawCondition(); root.append(condition, preview);
  const wrap = el('div', null, 'row'); wrap.append(button('外加“且”条件组', () => { rule.condition = {all: [rule.condition, defaultLeaf()]}; changedCondition(); }), button('外加“或”条件组', () => { rule.condition = {any: [rule.condition, defaultLeaf()]}; changedCondition(); }), button('采用指标约定单位', changedCondition)); root.append(wrap);
  for (const [key, label] of [['phenomena', '机理现象'], ['causes', '可能原因'], ['actions', '处理措施'], ['notes', '原文说明'], ['review_notes', '解释口径／需复核事项']]) {
    const control = area((rule[key] || []).join('\n'), 2); control.addEventListener('input', () => { rule[key] = control.value.split('\n').map(text => text.trim()).filter(Boolean); }); root.append(field(label + '（每行一项）', control));
  }
  const errors = rule.validation_errors || [];
  if (errors.length || rule.valid === false) { const box = el('div', null, 'warning-list'); box.append(el('strong', '需修正后才能审核通过')); errors.forEach(text => box.append(el('p', typeof text === 'string' ? text : JSON.stringify(text)))); root.append(box); }
  const associations = el('details', null, 'text-details associations');
  associations.append(el('summary', '关联原文块 · 已选 ' + (rule.block_ids || []).length + ' 块'));
  const associationList = el('div', null, 'association-list'); associations.append(associationList); root.append(associations);
  for (const block of state.document.payload.blocks) {
    const line = el('label', null, 'check-row'); const check = input('', 'checkbox'); check.checked = (rule.block_ids || []).includes(block.id);
    check.addEventListener('change', () => { rule.block_ids = check.checked ? [...new Set([...(rule.block_ids || []), block.id])] : (rule.block_ids || []).filter(id => id !== block.id); });
    line.append(check, el('span', block.id + ' · ' + block.locator + ' · ' + block.text.slice(0, 50))); associationList.append(line);
    check.addEventListener('change', () => { associations.querySelector('summary').textContent = '关联原文块 · 已选 ' + rule.block_ids.length + ' 块'; });
  }
  const retired = input('', 'checkbox'); retired.checked = Boolean(rule.retired); retired.addEventListener('change', () => { rule.retired = retired.checked; });
  const retireRow = el('label', null, 'check-row'); retireRow.append(retired, el('span', '停用这条规则（保存、审核并发布后生效）')); root.append(retireRow);
  auditActions(root, 'rule', rule.id);
}
async function loadReleases() {
  state.releases = arrayResult(await api('/admin/releases?kb_id=' + encodeURIComponent(currentKb())));
  const list = $('release-list'); list.replaceChildren(); list.classList.remove('empty');
  $('query-release').replaceChildren(option('', '当前发布版本'), ...state.releases.map(release => option(release.id, '发布 V' + release.number + ' · ' + release.created_at)));
  if (!state.releases.length) return empty(list, '暂无发布版本。审核内容后即可首次发布。');
  for (const release of state.releases.slice().sort((a, b) => b.number - a.number)) {
    const node = button('', async () => { state.release = await api('/admin/releases/' + encodeURIComponent(release.id)); renderRelease(); list.querySelectorAll('button').forEach(btn => btn.classList.remove('active')); node.classList.add('active'); }, 'release-item');
    node.append(el('strong', '发布 V' + release.number), el('small', release.created_at), el('small', (release.entry_count ?? '') + ' 条知识 · ' + (release.profile?.model || ''))); list.append(node);
  }
}
function renderRelease() {
  const release = state.release; const root = $('release-detail'); root.replaceChildren(); root.classList.remove('empty'); $('restore').classList.remove('hidden');
  root.append(el('p', release.id, 'muted')); const changes = release.changes || {}; const stats = el('div', null, 'metadata-grid');
  for (const [key, label] of [['added', '新增'], ['changed', '修改'], ['removed', '移除']]) { const value = changes[key]; const node = el('div'); node.append(el('strong', Array.isArray(value) ? value.length : typeof value === 'number' ? value : 0), el('small', label)); stats.append(node); } root.append(stats);
  if (release.profile) root.append(el('p', '索引模型：' + (release.profile.mode || '') + ' · ' + (release.profile.model || ''), 'muted'));
  for (const entry of release.entries || []) { const rule = entry.rule || entry; const row = el('div', null, 'release-entry'); row.append(el('strong', (rule.name || entry.id) + ' · ' + (entry.kind === 'rule' ? '专业规则' : '说明')), textDisclosure(entry.text || entry.original_text || ''));  if (rule.condition) row.append(el('p', expression(rule.condition), 'condition-preview')); row.append(sourceCaption(entry.source)); root.append(row); }
}
function sourceCaption(source = {}) { return el('div', [source.title, source.document_id, source.document_revision ? '文档 V' + source.document_revision : '', source.locator].filter(Boolean).join(' · '), 'source-caption'); }
function showResults(result) {
  const profile = result.profile || result.embedding_profile || {};
  $('search-meta').textContent = '发布版本：' + (result.release_id || '暂无') + (profile.model ? ' · 索引：' + profile.mode + ' / ' + profile.model : '');
  const items = result.items || result.rules || []; const root = $('search-results'); root.replaceChildren();
  if (!items.length && !result.answer) empty(root, '没有查询到符合条件的已发布知识。');
  for (const item of items) {
    const rule = item.rule || item;
    const card = el('article', null, 'result-card'); const header = el('div', null, 'section-heading compact'); header.append(el('h3', rule.name || item.id), badge(item.kind === 'rule' || rule.condition ? '专业规则' : '说明')); card.append(header);
    const excerpt = item.text || item.original_text || '';
    card.append(el('p', excerpt.slice(0, 180) + (excerpt.length > 180 ? '…' : ''), 'preserve-lines'));
    if (excerpt.length > 180) card.append(textDisclosure(excerpt));
    if (rule.condition) card.append(el('div', expression(rule.condition), 'condition-preview'));
    if (item.score !== undefined) card.append(el('small', '检索排序分值：' + Number(item.score).toFixed(4) + '（不是诊断置信度）'));
    card.append(sourceCaption(item.source));
    const details = el('div');
    card.append(button('查看完整知识', async () => { const query = new URLSearchParams({kb_id: currentKb()}); if (result.release_id) query.set('release_id', result.release_id); const value = await api('/knowledge/' + encodeURIComponent(item.knowledge_id || item.id) + '?' + query); const knowledge = value.item || value.knowledge || value; details.replaceChildren(el('pre', knowledge.text || knowledge.original_text || '', 'preserve-lines')); if (knowledge.rule?.condition || knowledge.condition) details.append(el('p', expression(knowledge.rule?.condition || knowledge.condition), 'condition-preview')); }, 'quiet'), details);
    root.append(card);
  }
  $('answer-box').classList.toggle('hidden', !result.answer);
  $('answer-text').textContent = result.answer || '';
  const citations = $('answer-citations'); citations.replaceChildren();
  for (const citation of result.citations || []) { const node = el('div', null, 'citation'); node.append(el('strong', citation.knowledge_id || citation.id || '引用'), el('blockquote', citation.quote || citation.text || ''), sourceCaption(citation.source)); citations.append(node); }
  if (result.status === 'insufficient_evidence') notice('已发布知识中没有足够依据，未生成确定性结论。');
}
async function query(mode) {
  const kb_id = currentKb(); const body = {kb_id, query: $('query').value.trim(), top_k: Number($('top-k').value) || 5};
  if ($('query-release').value) body.release_id = $('query-release').value;
  if ($('query-device').value) body.device_type = $('query-device').value;
  if (mode !== 'rules' && !body.query) throw new Error('请输入问题或关键词。');
  notice(mode === 'answers' ? '正在检索依据并组织回答…' : '正在读取已发布知识…');
  $('answer-box').classList.add('hidden'); $('search-meta').textContent = ''; empty($('search-results'), '查询中…');
  let result;
  if (mode === 'rules') { const params = new URLSearchParams({kb_id}); if (body.release_id) params.set('release_id', body.release_id); if (body.device_type) params.set('device_type', body.device_type); result = await api('/rules?' + params); }
  else result = await api('/' + mode, {method: 'POST', body});
  showResults(result); if (result.status !== 'insufficient_evidence') notice('查询完成。');
}
document.querySelectorAll('[data-tab]').forEach(node => node.addEventListener('click', () => showTab(node.dataset.tab)));
for (const type of ['input', 'change']) document.querySelector('.editor-pane').addEventListener(type, event => { if (!event.target.closest('.audit-note, .rule-toolbar')) state.dirty = true; });
$('connection-form').addEventListener('submit', action(async () => { state.token = $('token').value.trim(); state.access = $('access-mode').value; sessionStorage.setItem('rag-token', state.token); sessionStorage.setItem('rag-access', state.access); await load(); $('connection-dialog').close(); notice('已连接。'); }));
$('disconnect').addEventListener('click', () => { sessionStorage.removeItem('rag-token'); sessionStorage.removeItem('rag-access'); location.reload(); });
$('refresh').addEventListener('click', action(load));
$('kb-select').addEventListener('change', action(async () => { state.kb = $('kb-select').value || null; await loadKb(); }));
$('create-kb').addEventListener('submit', action(async event => { requireAdmin(); const data = new FormData(event.target); const kb = await api('/admin/knowledge-bases', {method: 'POST', body: {name: data.get('name')}}); state.kb = kb.id; event.target.reset(); $('create-dialog').close(); await load(); notice('知识库已创建，可以上传文档。'); }));
$('archive-kb').addEventListener('click', action(async () => { requireAdmin(); const kb = state.kbs.find(item => item.id === currentKb()); await api('/admin/knowledge-bases/' + encodeURIComponent(kb.id) + '/archive', {method: 'POST', body: {archived: !kb.archived}}); await load(); notice(kb.archived ? '知识库已恢复。' : '知识库已归档。'); }));
$('upload-form').addEventListener('submit', action(async event => { requireAdmin(); const body = new FormData(event.target); body.append('kb_id', currentKb()); $('upload-dialog').close(); await waitTask(await api('/admin/documents', {method: 'POST', body}), '解析并提取文档'); event.target.reset(); await loadDocuments(); }));
$('review-document').addEventListener('change', action(async () => { if ($('review-document').value) await openDocument($('review-document').value); }));
$('edit-description').addEventListener('click', () => setEditor('description'));
$('edit-rules').addEventListener('click', () => setEditor('rules'));
for (const [id, path, title] of [['publish', 'publish', '发布知识库'], ['reindex', 'reindex', '重建索引并发布']]) $(id).addEventListener('click', action(async () => { requireAdmin(); await waitTask(await api('/admin/' + path, {method: 'POST', body: {kb_id: currentKb()}}), title); await load(); showTab('versions'); }));
$('restore').addEventListener('click', action(async () => { requireAdmin(); if (!state.release) return; if (!confirm('恢复会创建新发布版本，并将当前草稿审核状态重置为待审核；草稿内容保留。继续恢复？')) return; await waitTask(await api('/admin/restore', {method: 'POST', body: {kb_id: currentKb(), release_id: state.release.id}}), '恢复为新的发布版本'); await load(); showTab('versions'); }));
$('search-form').addEventListener('submit', action(() => query('retrieve')));
$('ask').addEventListener('click', action(() => query('answers')));
$('all-rules').addEventListener('click', action(() => query('rules')));
$('token').value = state.token; $('access-mode').value = state.access; setAccess();
if (state.token) action(load)();

const icons = {
  file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',
  check:'M9 5H5v16h14V5h-4 M9 3h6v4H9z M8 14l3 3 5-6',
  layers:'m12 3 10 5-10 5L2 8z M2 12l10 5 10-5 M2 16l10 5 10-5',
  search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  book:'M12 5v16 M12 5C8 2 4 3 2 4v16c4-2 7-1 10 1 3-2 6-3 10-1V4c-2-1-6-2-10 1',
  settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z'
};
document.querySelectorAll('[data-icon]').forEach(node => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.6'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', icons[node.dataset.icon]); svg.append(path); node.append(svg);
});
$('open-settings').addEventListener('click', () => $('connection-dialog').showModal());
$('new-kb').addEventListener('click', () => $('create-dialog').showModal());
$('upload-open').addEventListener('click', action(() => { currentKb(); $('upload-dialog').showModal(); }));
document.querySelectorAll('[data-close]').forEach(node => node.addEventListener('click', () => $(node.dataset.close).close()));
$('document-filter').addEventListener('input', renderDocuments);
for (const id of ['block-search', 'block-kind']) $(id).addEventListener(id === 'block-search' ? 'input' : 'change', () => { blockPage = 0; if (state.document) renderDescription(); });
$('blocks-prev').addEventListener('click', () => { blockPage--; renderDescription(); });
$('blocks-next').addEventListener('click', () => { blockPage++; renderDescription(); });
if (!state.token) $('connection-dialog').showModal();

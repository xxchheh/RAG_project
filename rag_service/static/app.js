'use strict';
// All document/model text is rendered through textContent or form values.
const $ = id => document.getElementById(id);
const state = {category: sessionStorage.getItem('rag-category') || 'reference', page: 1, openRequest: 0, busy: false, exclusionsDirty: false, token: sessionStorage.getItem('rag-token') || '', access: sessionStorage.getItem('rag-access') || 'admin', kbs: [], kb: null, documents: [], document: null, releases: [], release: null, capabilities: {}, ruleIndex: 0, editor: 'description', dirty: false, activeTab: 'documents', wikiRules: [], wikiRuleId: null, wikiContext: null, wikiRequest: 0, queryRequest: 0};
const statusNames = {pending: '待审核', approved: '已通过', rejected: '已退回', queued: '排队中', processing: '处理中', succeeded: '已完成', failed: '处理失败', ready: '已解析', description: '说明', rule: '规则', uncertain: '待分类'};
const clone = value => JSON.parse(JSON.stringify(value));
function el(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = String(text); if (cls) node.className = cls; return node; }
function empty(node, text) { node.replaceChildren(el('div', text, 'empty')); }
let noticeTimer;
function notice(message, error = false) {
  clearTimeout(noticeTimer);
  $('notice').textContent = message;
  $('notice').className = 'notice' + (error ? ' error' : '');
  if (!error && !/正在|处理中|等待|未保存/.test(message)) noticeTimer = setTimeout(() => $('notice').classList.add('hidden'), 4000);
}
function technicalDetails(values) {
  const details = el('details', null, 'technical-details'); details.append(el('summary', '技术详情'));
  for (const [label, value] of Object.entries(values)) {
    const row = el('label', label); const control = input(value ?? ''); control.readOnly = true;
    control.addEventListener('focus', () => control.select()); row.append(control); details.append(row);
  }
  return details;
}
function viewSection(title) {
  const section = el('section', null, 'view-section'); const heading = el('h3', title);
  const symbol = {'基本信息':'file', '判断条件':'check', '结构化判断条件':'check', '处理措施':'tool'}[title];
  if (symbol) heading.prepend(inlineIcon(symbol));
  section.append(heading); return section;
}

function errorMessage(error) { return error?.message || String(error); }
function action(fn) { return async event => { event?.preventDefault(); const node = event?.currentTarget; if (node?.dataset.running) return; if (node) node.dataset.running = 'true'; try { await fn(event); } catch (error) { notice(errorMessage(error), true); } finally { if (node) delete node.dataset.running; } }; }
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
function setAccess() { document.querySelectorAll('.admin-action, .admin-form button, .admin-form input, .admin-form select').forEach(node => { node.disabled = state.access !== 'admin'; }); }
function showTab(name) {
  state.activeTab = name; sessionStorage.setItem('rag-tab', name);
  $('sidebar-conversations').classList.toggle('hidden', name !== 'search');
  $('page-name').textContent = name === 'documents' ? (state.category === 'rules' ? '知识管理 / 规则文档' : '知识管理 / 参考资料') : name === 'wiki' ? '专业规则' : '知识问答';
  document.querySelectorAll('[data-tab]').forEach(node => { const active = node.dataset.tab === name && (!node.dataset.category || node.dataset.category === state.category); node.classList.toggle('active', active); node.setAttribute('aria-selected', String(active)); });
  document.querySelectorAll('.tab-panel').forEach(node => node.classList.toggle('hidden', node.id !== 'tab-' + name));
  if (name === 'wiki' && state.token && state.kb && !state.wikiContext) action(loadWiki)();
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
function textDisclosure(text, label = '展开完整内容') {
  const details = el('details', null, 'text-details'); details.append(el('summary', label), el('pre', text, 'preserve-lines')); return details;
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
    const number = input(current ? node[current] : '', 'number'); number.step = 'any'; number.disabled = !current; number.dataset.inactive = String(!current); number.setAttribute('aria-label', ops[0] === 'ge' ? '下限阈值' : '上限阈值');
    picker.addEventListener('change', () => { const value = Number(number.value || 0); ops.forEach(key => delete node[key]); if (picker.value) node[picker.value] = value; delete node.label; onChange(); });
    number.addEventListener('input', () => {
      if (picker.value) node[picker.value] = number.value === '' ? null : Number(number.value);
      delete node.label; markDirty();
      const preview = $('rules-editor').querySelector('.condition-preview');
      if (preview) preview.textContent = expression(state.document.payload.rules[state.ruleIndex].condition);
    });
    number.addEventListener('change', () => { if (picker.value) node[picker.value] = number.value === '' ? null : Number(number.value); delete node.label; onChange(); });
    limits.append(picker, number);
  }
  root.append(limits); return root;
}
async function loadReleases() {
  state.releases = arrayResult(await api('/admin/releases?kb_id=' + encodeURIComponent(currentKb())));
  const list = $('release-list'); list.replaceChildren(); list.classList.remove('empty');
  $('query-release').replaceChildren(option('', '当前发布版本'), ...state.releases.map(release => option(release.id, '发布 V' + release.number + ' · ' + release.created_at)));
  $('wiki-release-options').replaceChildren(...state.releases.map(release => option(release.id, '发布 V' + release.number + ' · ' + release.created_at)));
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
  for (const entry of release.entries || []) { const rule = entry.rule || entry; const row = el('div', null, 'release-entry'); row.append(el('strong', (rule.name || entry.text?.split('\n', 1)[0] || entry.id) + ' · ' + (entry.kind === 'rule' ? '专业规则' : '说明')), textDisclosure(entry.text || entry.original_text || ''));  if (rule.condition) row.append(el('p', expression(rule.condition), 'condition-preview')); row.append(sourceCaption(entry.source)); root.append(row); }
}
function sourceCaption(source = {}) { return el('div', [source.title, source.locator].filter(Boolean).join(' · '), 'source-caption'); }
function showResults(result) {
  const profile = result.profile || result.embedding_profile || {};
  $('search-meta').textContent = '发布版本：' + (result.release_id || '暂无') + (profile.model ? ' · 索引：' + profile.mode + ' / ' + profile.model : '');
  const items = result.items || result.rules || []; const root = $('search-results'); root.replaceChildren();
  if (!items.length && !result.answer) empty(root, '没有查询到符合条件的已发布知识。');
  for (const item of items) {
    const rule = item.rule || item;
    const card = el('article', null, 'result-card'); const header = el('div', null, 'section-heading compact'); header.append(el('h3', rule.name || item.text?.split('\n', 1)[0] || item.id), badge(item.kind === 'rule' || rule.condition ? '专业规则' : '说明')); card.append(header);
    const focus = $('query').value;
    const selected = /原因|为什么|成因/.test(focus) ? rule.causes : /措施|处理|维修|怎么办/.test(focus) ? rule.actions : /现象|表现|症状/.test(focus) ? rule.phenomena : null;
    const excerpt = selected?.length ? selected.join('\n') : item.text || item.original_text || '';
    card.append(el('p', excerpt.slice(0, 180) + (excerpt.length > 180 ? '…' : ''), 'preserve-lines'));
    if (excerpt.length > 180 || selected?.length) card.append(textDisclosure(item.text || item.original_text || excerpt));
    if (rule.condition) card.append(el('div', expression(rule.condition), 'condition-preview'));
    if (item.score !== undefined) card.append(technicalDetails({'知识编号': item.id, '检索排序分值': Number(item.score).toFixed(4)}));
    card.append(sourceCaption(item.source));
    const details = el('div');
    card.append(button('查看完整知识', async () => { const query = new URLSearchParams({kb_id: result.kb_id}); if (result.release_id) query.set('release_id', result.release_id); const value = await api('/knowledge/' + encodeURIComponent(item.knowledge_id || item.id) + '?' + query); const knowledge = value.item || value.knowledge || value; details.replaceChildren(el('pre', knowledge.text || knowledge.original_text || '', 'preserve-lines')); if (knowledge.rule?.condition || knowledge.condition) details.append(el('p', expression(knowledge.rule?.condition || knowledge.condition), 'condition-preview')); }, 'quiet'), details);
    root.append(card);
  }
  $('answer-box').classList.toggle('hidden', !result.answer);
  $('answer-text').textContent = result.answer || '';
  if (result.model_info?.model) $('search-meta').textContent += ' · 回答模型：' + result.model_info.provider + ' / ' + result.model_info.model;
  const citations = $('answer-citations'); citations.replaceChildren();
  for (const [index, citation] of (result.citations || []).entries()) { const node = el('div', null, 'citation'); node.append(el('strong', '[' + (index + 1) + '] ' + (citation.source?.title || citation.knowledge_id || '引用')), el('blockquote', citation.quote || citation.text || ''), sourceCaption(citation.source)); citations.append(node); }
  if (result.status === 'insufficient_evidence') notice('已发布知识中没有足够依据，未生成确定性结论。');
}
async function query(mode) {
  const kb_id = currentKb(); const body = {kb_id, query: $('query').value.trim(), top_k: Number($('top-k').value)};
  const release_id = $('query-release-id').value.trim() || $('query-release').value;
  if (release_id) body.release_id = release_id;
  if ($('query-device').value) body.device_type = $('query-device').value;
  if (!body.query) throw new Error('请输入问题或关键词。');
  if (!Number.isInteger(body.top_k) || body.top_k < 1 || body.top_k > 50) throw new Error('检索条数应为1至50的整数。');
  const request = ++state.queryRequest;
  notice(mode === 'generated-answers' ? '正在检索依据并由大模型组织回答…' : '正在读取已发布知识…');
  $('answer-box').classList.add('hidden'); $('answer-text').textContent = ''; $('answer-citations').replaceChildren(); $('search-meta').textContent = ''; empty($('search-results'), '查询中…');
  const controls = [$('ask'), $('retrieve-evidence')]; controls.forEach(node => node.disabled = true);
  try {
    const result = await api('/' + mode, {method: 'POST', body});
    if (request !== state.queryRequest || kb_id !== state.kb) return;
    showResults(result); $('evidence-panel').open = mode === 'retrieve'; if (result.status !== 'insufficient_evidence') notice(mode === 'generated-answers' ? '大模型回答已完成。' : '检索依据已返回。');
  } catch (error) {
    if (request !== state.queryRequest || kb_id !== state.kb) return;
    empty($('search-results'), '本次查询未完成：' + errorMessage(error));
    if (mode === 'generated-answers') notice('未能生成大模型回答。可先点击“查看检索依据”；一般问答需在服务端配置回答模型。原因：' + errorMessage(error), true);
    else notice(errorMessage(error), true);
  } finally { if (request === state.queryRequest) controls.forEach(node => node.disabled = false); }
}
function wikiDeviceName(type) { return deviceOptions().find(([key]) => key === type)?.[1] || type || '未分类'; }
async function loadWiki() {
  const kb_id = currentKb(); let release = $('wiki-release').value.trim();
  const device = $('wiki-device').value;
  const request = ++state.wikiRequest;
  state.wikiRules = []; state.wikiRuleId = null; state.wikiContext = null;
  $('wiki-meta').textContent = ''; $('wiki-count').textContent = '0 条';
  empty($('wiki-list'), '正在读取完整规则集…'); empty($('wiki-detail'), '正在读取规则…');
  try {
    if (!release) {
      const kbs = await api('/knowledge-bases');
      if (request !== state.wikiRequest || kb_id !== state.kb) return;
      const kb = kbs.find(kb => kb.id === kb_id);
      if (!kb) throw new Error('知识库不存在或已归档。');
      release = kb.current_release;
    }
    if (!release) { empty($('wiki-list'), '当前知识库尚未发布。'); empty($('wiki-detail'), '完成专业规则审核并生效后，可在这里浏览。'); return; }
    const params = new URLSearchParams({kb_id, release_id: release}); if (device) params.set('device_type', device);
    const result = await api('/rules?' + params);
    if (request !== state.wikiRequest || kb_id !== state.kb) return;
    state.wikiRules = result.rules; state.wikiContext = {kb_id, release_id: result.release_id, schema: result.schema_version};
    $('wiki-meta').textContent = '已发布规则 ' + result.count + ' 条';
    renderWikiDirectory(); notice('已读取 ' + result.count + ' 条已发布规则。');
  } catch (error) {
    if (request !== state.wikiRequest || kb_id !== state.kb) return;
    empty($('wiki-list'), '规则读取失败'); empty($('wiki-detail'), errorMessage(error));
    $('wiki-meta').textContent = '本次未加载规则，请检查知识库、发布版本或访问权限。';
    notice(errorMessage(error), true);
  }
}
function renderWikiDirectory() {
  const term = $('wiki-search').value.trim().toLowerCase();
  const rules = state.wikiRules.filter(rule => !term || [rule.name, rule.id, ...(rule.phenomena || []), ...(rule.causes || []), ...(rule.actions || []), rule.original_text].join(' ').toLowerCase().includes(term));
  $('wiki-count').textContent = rules.length + ' / ' + state.wikiRules.length + ' 条';
  const list = $('wiki-list'); list.replaceChildren();
  if (!rules.some(rule => rule.id === state.wikiRuleId)) state.wikiRuleId = rules[0]?.id || null;
  if (!rules.length) { empty(list, term ? '没有匹配的故障规则。' : '此范围没有已发布规则。'); empty($('wiki-detail'), term ? '请调整关键词，或清空搜索。' : '普通参考资料不会自动成为诊断规则。'); return; }
  for (const [device, label] of deviceOptions()) {
    const group = rules.filter(rule => rule.device_type === device); if (!group.length) continue;
    list.append(el('div', label + ' · ' + group.length, 'wiki-group-title'));
    for (const rule of group) {
      const item = button('', () => { state.wikiRuleId = rule.id; renderWikiDirectory(); }, 'wiki-rule-link' + (rule.id === state.wikiRuleId ? ' active' : ''));
      item.setAttribute('aria-pressed', String(rule.id === state.wikiRuleId)); item.dataset.ruleId = rule.id;
      item.append(el('strong', rule.name)); list.append(item);
    }
  }
  renderWikiDetail(rules.find(rule => rule.id === state.wikiRuleId));
}
function renderWikiDetail(rule) {
  const root = $('wiki-detail'); root.replaceChildren(); if (!rule) return;
  const context = {...state.wikiContext};
  const heading = el('div', null, 'section-heading compact'); heading.append(el('h3', rule.name), badge(wikiDeviceName(rule.device_type))); root.append(heading);
  const conditions = viewSection('判断条件');
  const formulas = new Map();
  function group(node) {
    const box = el('div', null, 'reading-condition-group');
    const children = node.all || node.any;
    if (children) {
      box.append(el('h4', node.all ? '以下条件全部满足' : '以下条件任意一项满足'));
      let table;
      for (const child of children) {
        if (child.all || child.any) { table = null; box.append(group(child)); continue; }
        if (!table) { table = conditionTable(); box.append(table); }
        appendLeaf(table, child);
      }
    } else { const table = conditionTable(); appendLeaf(table, node); box.append(table); }
    return box;
  }
  function conditionTable() {
    const table = el('table', null, 'wiki-condition-table'); const head = el('thead'); const row = el('tr');
    ['指标', '比较条件', '单位'].forEach(text => row.append(el('th', text))); head.append(row); table.append(head, el('tbody')); return table;
  }
  function appendLeaf(table, node) {
    const metric = metricRegistry()[node.metric];
    const row = el('tr'); row.append(el('td', metric?.name || node.metric), el('td', ['ge','gt','le','lt'].filter(op => op in node).map(op => ({ge:'≥',gt:'>',le:'≤',lt:'<'}[op]) + ' ' + node[op]).join(' 且 ')), el('td', rule.units[node.metric] === '1' ? '无量纲' : rule.units[node.metric]));
    table.querySelector('tbody').append(row);
    if (metric?.numerators) {
      const name = id => metricRegistry()[id]?.name || id;
      formulas.set(node.metric, (metric.numerators.length === 1 ? name(metric.numerators[0]) : '√(' + metric.numerators.map(id => '(' + name(id) + ')²').join(' + ') + ')') + ' / ' + name(metric.denominator));
    }
  }
  conditions.append(group(rule.condition), textDisclosure(expression(rule.condition), '查看完整表达式'));
  if (formulas.size) conditions.append(textDisclosure([...formulas.values()].join('\n'), '计算说明'));
  root.append(conditions);
  for (const [key, label] of [['phenomena','机理现象'],['causes','可能原因'],['actions','处理措施'],['notes','原文说明'],['review_notes','审核说明']]) {
    if (!rule[key]?.length) continue;
    const section = viewSection(label); const list = el('ul'); rule[key].forEach(text => list.append(el('li', text))); section.append(list); if (['notes','review_notes'].includes(key)) { const extra = el('details', null, 'text-details'); extra.append(el('summary', label), list); root.append(extra); } else root.append(section);
  }
  root.append(textDisclosure(rule.original_text, '原文判据'));
  const source = el('details', null, 'text-details'); source.append(el('summary', '原文依据'), sourceCaption(rule.source));
  if (rule.source?.original_quote) source.append(textDisclosure(rule.source.original_quote, '查看来源摘录'));
  const full = el('div');
  source.append(button('查看完整知识', async () => {
    const value = await api('/knowledge/' + encodeURIComponent(rule.knowledge_id) + '?' + new URLSearchParams({kb_id: context.kb_id, release_id: context.release_id}));
    full.replaceChildren(el('pre', value.item.text, 'preserve-lines'));
  }, 'quiet'), full); root.append(source, technicalDetails({'规则编号': rule.id, '知识编号': rule.knowledge_id, '发布版本': context.release_id, '规则格式': context.schema, '知识修订': rule.knowledge_revision}));
}
const icons = {
  file:'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h6',
  check:'M9 5H5v16h14V5h-4 M9 3h6v4H9z M8 14l3 3 5-6',
  layers:'m12 3 10 5-10 5L2 8z M2 12l10 5 10-5 M2 16l10 5 10-5',
  search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  book:'M12 5v16 M12 5C8 2 4 3 2 4v16c4-2 7-1 10 1 3-2 6-3 10-1V4c-2-1-6-2-10 1',
  upload:'M12 16V3 m-5 5 5-5 5 5 M4 16v5h16v-5',
  save:'M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12l4 4v12a2 2 0 0 1-2 2 M7 3v6h10V3 M7 21v-8h10v8',
  chat:'M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 1 1 17-5 M8 10h8 M8 14h5',
  tool:'M14 6a5 5 0 0 0-6 6l-5 5a2 2 0 0 0 3 3l5-5a5 5 0 0 0 6-6l-3 3-3-3z',
  settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z'
};
function inlineIcon(name) {
  const node = el('span', null, 'inline-icon');
  node.setAttribute('aria-hidden', 'true');
  node.dataset.icon = name;
  drawIcon(node);
  return node;
}
function drawIcon(node) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '1.6'); svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', icons[node.dataset.icon]); svg.append(path); node.append(svg);
}
document.querySelectorAll('[data-icon]').forEach(drawIcon);

'use strict';
const $ = id => document.getElementById(id);
const fusion = {
  token: sessionStorage.getItem('rag-token') || '',
  project: null,
  run: null,
  selected: new Map(),
  gold: new Set(),
  data: {},
  timer: null,
  generation: 0
};
const base = '/api/v1/admin/knowledge-fusion';

function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}

function say(text, error = false) {
  $('message').textContent = text;
  $('message').classList.toggle('error', error);
}

function action(fn) {
  return async event => {
    const b = event?.currentTarget;
    try {
      if (b?.tagName === 'BUTTON') b.disabled = true;
      await fn(event);
    } catch (e) {
      say(e.message, true);
    } finally {
      if (b?.tagName === 'BUTTON') b.disabled = false;
    }
  };
}
async function request(path, options = {}) {
  const headers = {
    Authorization: 'Bearer ' + fusion.token,
    ...options.headers
  };
  if (options.body) {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const r = await fetch(path, {
    ...options,
    headers
  });
  if (!r.ok) {
    const e = await r.json();
    throw new Error(typeof e.detail === 'string' ? e.detail : JSON.stringify(e.detail));
  }
  return r;
}
async function api(path, options) {
  return (await request(base + path, options)).json();
}

function selectOptions(select, items, value) {
  select.replaceChildren(...items.map(([id, label]) => {
    const o = node('option', label);
    o.value = id;
    return o;
  }));
  if (value) select.value = value;
}

function show(view) {
  for (const id of ['project', 'raw', 'knowledge', 'summary']) $('view-' + id).classList.toggle('hidden', id !== view);
}
async function collection(name, runId) {
  let rows = [],
    offset = 0;
  for (;;) {
    const batch = await api('/runs/' + runId + '/' + name + '?limit=500&offset=' + offset);
    rows.push(...batch);
    if (batch.length < 500) return rows;
    offset += 500;
  }
}
async function download(path, name) {
  const r = await request(path);
  const url = URL.createObjectURL(await r.blob());
  const a = node('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function card(title) {
  const n = node('article', undefined, 'fusion-card');
  n.append(node('h3', title));
  return n;
}

function details(title, children) {
  const d = node('details');
  d.append(node('summary', title), ...children);
  return d;
}

function claimMap() {
  return new Map((fusion.data.claims || []).map(c => [c.id, c]));
}

function evidenceView(evidence) {
  const spans = new Map((fusion.data.spans || []).map(s => [s.id, s]));
  return evidence.map(e => {
    const s = spans.get(e.source_span_id);
    const n = node('div');
    n.append(node('blockquote', e.quote), node('small', s ? (s.document_id + ' · ' + s.locator) : e.source_span_id));
    if (s) {
      const button = node('button', '下载本次运行的原件');
      button.onclick = action(() => download(base + '/runs/' + fusion.run + '/documents/' + s.document_id + '/original', 'original'));
      n.append(button);
      if (s.page) {
        const preview = node('button', '核对 PDF 原页');
        preview.onclick = action(async () => {
          const r = await request(base + '/runs/' + fusion.run + '/documents/' + s.document_id + '/pages/' + s.page);
          const u = URL.createObjectURL(await r.blob());
          const image = node('img');
          image.src = u;
          image.alt = s.locator;
          image.style.maxWidth = '100%';
          image.onload = () => URL.revokeObjectURL(u);
          n.append(image);
        });
        n.append(preview);
      }
    }
    return n;
  });
}

function render() {
  const d = fusion.data,
    byId = claimMap();
  const filter = $('claim-document').value;
  selectOptions($('claim-document'), [
    ['', '全部文档'], ...(d.documents || []).map(x => [x.document_id, x.title])
  ], filter);
  $('claims').replaceChildren(...(d.claims || []).filter(c => !filter || c.document_id === filter).map(c => {
    const n = card(c.concept);
    const check = node('input');
    check.type = 'checkbox';
    check.checked = fusion.gold.has(c.id);
    check.onchange = () => check.checked ? fusion.gold.add(c.id) : fusion.gold.delete(c.id);
    n.prepend(check);
    n.append(node('p', c.statement), node('small', c.id + ' · ' + c.knowledge_type), node('p', '条件 ' + c.conditions.join('；')), node('p', '限定 ' + c.qualifiers.join('；')), details('原文证据', evidenceView(c.evidence)));
    return n;
  }));
  const rels = new Map((d.relations || []).map(r => [r.id, r]));
  $('relations').replaceChildren(...(d.candidates || []).map(pair => {
    const r = rels.get(pair.id),
      n = card(r?.relation || '待判断');
    const sides = node('div', undefined, 'fusion-pair');
    for (const id of [pair.claim_a, pair.claim_b]) sides.append(node('p', byId.get(id)?.statement || id));
    n.append(sides, node('small', '相似度 ' + pair.similarity.toFixed(4)));
    if (r) n.append(node('p', r.reason), node('p', r.conditions_comparison), node('small', '建议合并 ' + r.merge_recommended));
    return n;
  }));
  $('knowledge').replaceChildren(...(d.knowledge || []).map(k => {
    const n = card(k.concept);
    n.id = k.id;
    n.append(node('p', k.statement), node('p', '条件 ' + k.conditions.join('；')), node('p', '限定 ' + k.qualifiers.join('；')), details('归并成员', k.claim_ids.map(id => node('p', (byId.get(id)?.statement || id) + ' · ' + id))), details('全部来源', evidenceView(k.evidence)));
    return n;
  }));
  $('concepts').replaceChildren(...(d.concepts || []).map(c => node('p', c.name + ' · ' + c.knowledge_ids.length + ' 条知识')));
  const topicNodes = new Map();
  $('topics').replaceChildren();
  for (const t of d.topics || []) {
    const n = node('div', undefined, 'fusion-topic');
    n.append(node('strong', t.name), details('关联知识', t.knowledge_ids.map(id => node('p', (d.knowledge || []).find(k => k.id === id)?.statement || id))));
    topicNodes.set(t.id, n);
  }
  for (const t of d.topics || [])(topicNodes.get(t.parent_id) || $('topics')).append(topicNodes.get(t.id));
  selectOptions($('summary-topic'), (d.topics || []).map(t => [t.id, t.name]));
  $('summaries').replaceChildren(...(d.summaries || []).map(s => {
    const n = card(s.title);
    if (s.insufficient_evidence) n.append(node('p', '资料不足，以下内容限于现有证据。'));
    for (const p of s.paragraphs) {
      const paragraph = node('p', p.text);
      for (const id of p.knowledge_ids) {
        const a = node('a', ' [' + id.slice(-8) + ']');
        a.href = '#' + id;
        a.onclick = () => show('knowledge');
        paragraph.append(a);
      }
      n.append(paragraph);
    }
    return n;
  }));
  const stats = d.evaluation || {},
    labels = {
      documents: '参与文档',
      raw_claims: '原始知识',
      candidate_pairs: '候选对',
      merged_groups: '归并组',
      merged_claim_reduction: '归并减少条数',
      canonical_knowledge: '规范化知识',
      traceable_ratio: '来源链完整率',
      labeled_pairs: '人工标注配对',
      precision: '等价识别 Precision',
      recall: '等价识别 Recall',
      f1: '等价识别 F1'
    };
  const grid = node('div', undefined, 'fusion-stats');
  for (const [key, label] of Object.entries(labels)) {
    const n = node('div', label);
    n.append(node('strong', stats[key] === null || stats[key] === undefined ? '—' : String(stats[key])));
    grid.append(n);
  }
  $('metrics').replaceChildren(grid, node('p', stats.evaluation_scope || ''), node('p', Object.entries(stats.relation_counts || {}).map(([k, v]) => k + ' ' + v).join(' · ')));
}
async function refreshRun() {
  if (!fusion.run) return;
  const generation = ++fusion.generation,
    id = fusion.run;
  const run = await api('/runs/' + id);
  const names = ['documents', 'spans', 'claims', 'candidates', 'relations', 'knowledge', 'concepts', 'topics', 'summaries'];
  const values = await Promise.all(names.map(n => collection(n, id)));
  const evaluation = await api('/runs/' + id + '/evaluation');
  if (generation !== fusion.generation) return;
  fusion.data = Object.fromEntries(names.map((n, i) => [n, values[i]]));
  fusion.data.evaluation = evaluation;
  render();
  $('progress').textContent = run.status + ' · ' + run.stage + (run.error ? '\n' + run.error : '');
  clearTimeout(fusion.timer);
  if (['queued', 'processing'].includes(run.status)) fusion.timer = setTimeout(() => action(refreshRun)(), 2000);
}
async function loadRuns() {
  clearTimeout(fusion.timer);
  fusion.generation++;
  fusion.gold.clear();
  const runs = await api('/projects/' + fusion.project + '/runs');
  const remembered = sessionStorage.getItem('fusion-run:' + fusion.project);
  fusion.run = runs.find(r => r.id === remembered)?.id || runs[0]?.id || null;
  selectOptions($('runs'), runs.map(r => [r.id, r.created_at + ' · ' + r.status]), fusion.run);
  if (fusion.run) await refreshRun();
  else {
    $('progress').textContent = '新项目可选择文档开始运行';
    fusion.data = {};
    render();
  }
}
async function loadProjects() {
  const projects = await api('/projects');
  fusion.project = projects.find(p => p.id === sessionStorage.getItem('fusion-project'))?.id || projects[0]?.id || null;
  selectOptions($('projects'), projects.map(p => [p.id, p.name]), fusion.project);
  if (fusion.project) await loadRuns();
}

function renderSelected() {
  $('selected-documents').replaceChildren(...[...fusion.selected.values()].map(d => node('p', d.title + ' · 修订 ' + d.latest_revision)));
}
async function loadDocuments() {
  const kb = $('source-kb').value;
  if (!kb) {
    $('source-documents').replaceChildren();
    return;
  }
  const rows = await (await request('/api/v1/admin/documents?kb_id=' + encodeURIComponent(kb))).json();
  $('source-documents').replaceChildren(...rows.filter(d => d.status === 'ready' && !d.disabled).map(d => {
    const label = node('label'),
      check = node('input');
    check.type = 'checkbox';
    check.checked = fusion.selected.has(d.id);
    check.onchange = () => {
      if (check.checked) fusion.selected.set(d.id, d);
      else fusion.selected.delete(d.id);
      renderSelected();
    };
    label.append(check, node('span', d.title + ' · 修订 ' + d.latest_revision));
    return label;
  }));
}
async function connect() {
  fusion.token = $('fusion-token').value.trim();
  const status = await api('/status');
  if (!status.enabled) throw new Error('服务尚未启用 RAG_FUSION_ENABLED');
  sessionStorage.setItem('rag-token', fusion.token);
  sessionStorage.setItem('rag-access', 'admin');
  $('workspace').classList.remove('hidden');
  const kbs = await (await request('/api/v1/admin/knowledge-bases')).json();
  selectOptions($('source-kb'), kbs.filter(k => !k.archived).map(k => [k.id, k.name]));
  await Promise.all([loadProjects(), loadDocuments()]);
  say('已连接。资料上传与解析在 RAG 工作台完成。');
}
$('fusion-token').value = fusion.token;
$('connect').onsubmit = action(async e => {
  e.preventDefault();
  await connect();
});
for (const n of document.querySelectorAll('[data-view]')) n.onclick = () => show(n.dataset.view);
$('projects').onchange = action(async () => {
  fusion.project = $('projects').value;
  sessionStorage.setItem('fusion-project', fusion.project);
  await loadRuns();
});
$('runs').onchange = action(async () => {
  fusion.run = $('runs').value;
  fusion.gold.clear();
  sessionStorage.setItem('fusion-run:' + fusion.project, fusion.run);
  await refreshRun();
});
$('source-kb').onchange = action(loadDocuments);
$('claim-document').onchange = render;
$('create-project').onclick = action(async () => {
  const name = prompt('融合项目名称');
  if (!name?.trim()) return;
  const p = await api('/projects', {
    method: 'POST',
    body: {
      name: name.trim()
    }
  });
  sessionStorage.setItem('fusion-project', p.id);
  await loadProjects();
});
$('start-fusion').onclick = action(async () => {
  if (!fusion.project) throw new Error('请先创建融合项目');
  if (!fusion.selected.size) throw new Error('请选择文档');
  const r = await api('/projects/' + fusion.project + '/runs', {
    method: 'POST',
    body: {
      documents: [...fusion.selected.values()].map(d => ({
        document_id: d.id,
        revision: d.latest_revision,
        edit_version: d.edit_version
      })),
      top_k: Number($('fusion-top-k').value),
      same_document: $('same-document').checked
    }
  });
  sessionStorage.setItem('fusion-run:' + fusion.project, r.id);
  await loadRuns();
});
$('refresh-fusion').onclick = action(refreshRun);
$('retry-fusion').onclick = action(async () => {
  await api('/runs/' + fusion.run + '/retry', {
    method: 'POST'
  });
  await refreshRun();
});
$('generate-summary').onclick = action(async () => {
  await api('/runs/' + fusion.run + '/summaries', {
    method: 'POST',
    body: {
      topic_id: $('summary-topic').value
    }
  });
  await refreshRun();
});
$('export-labels').onclick = action(() => download(base + '/runs/' + fusion.run + '/annotations.jsonl', 'annotations.jsonl'));
$('export-subset').onclick = action(() => {
  if (!fusion.gold.size) throw new Error('请勾选 Claim');
  const query = new URLSearchParams();
  for (const id of fusion.gold) query.append('claim_ids', id);
  return download(base + '/runs/' + fusion.run + '/annotations.jsonl?' + query, 'subset-annotations.jsonl');
});
$('import-labels').onchange = action(async e => {
  const file = e.target.files[0];
  if (!file) return;
  const rows = (await file.text()).split('\n').filter(s => s.trim()).map(s => JSON.parse(s)).filter(r => r.relation).map(r => ({
    claim_a: r.claim_a,
    claim_b: r.claim_b,
    relation: r.relation
  }));
  if (!rows.length) throw new Error('文件中没有已填写 relation 的配对');
  await api('/runs/' + fusion.run + '/annotations', {
    method: 'PUT',
    body: {
      labels: rows
    }
  });
  await refreshRun();
  say('标注已导入');
});
if (fusion.token) action(connect)();

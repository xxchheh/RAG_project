'use strict';
const maDisclosureStates = new Map();

function rememberMAOpenStates() {
  document.querySelectorAll('details[data-ma-key]').forEach(node => {
    maDisclosureStates.set(node.dataset.maKey, node.open);
  });
}

function maDisclosure(key, label, open = false) {
  const node = el('details');
  node.dataset.maKey = key;
  node.open = maDisclosureStates.has(key) ? maDisclosureStates.get(key) : open;
  node.append(el('summary', label));
  return node;
}

function renderMAAnalysis(analysis, key) {
  const states = {processing: '核对中', completed: '已生成最终回答', failed: '本轮未完成'};
  const panel = maDisclosure(key, '候选分歧与证据核对 · ' + states[analysis.status], true);
  panel.className = 'ma-process';
  if (analysis.demonstration) panel.append(el('p', '固定候选演示 · 演示数据', 'ma-demo-label'));
  const steps = el('ol', null, 'ma-steps');
  const labels = ['候选对比', '发现分歧', '补充检索', '最终回答'];
  const stage = {conflict_detected:1, searching:2, evidence_ready:2, finalizing:3, completed:3, failed:3}[analysis.stage];
  labels.forEach((label, index) => {
    const step = el('li', label);
    step.className = index < stage || analysis.status === 'completed' ? 'complete' : index === stage ? 'current' : '';
    if (analysis.status === 'failed') {
      const evidenceReady = analysis.searches.length && analysis.searches.every(search => search.status === 'completed');
      step.className = index < 2 ? 'complete' : index === 2 && evidenceReady ? 'complete' : 'failed';
    }
    if (index === 2 && !analysis.searches.length && stage === 3) {
      step.textContent += ' · 未执行';
      step.className = '';
    }
    if (index === stage) step.setAttribute('aria-current', 'step');
    steps.append(step);
  });
  panel.append(steps, el('h4', '候选结论'));
  const candidates = el('div', null, 'ma-candidates');
  for (const candidate of analysis.candidates) {
    const card = el('section', null, 'ma-candidate');
    card.append(el('strong', '候选 ' + candidate.id + ' · 草稿'));
    const text = candidate.summary.replace(/\s*\[\d+\]/g, '').trim();
    card.append(el('p', text.slice(0, 220) + (text.length > 220 ? '…' : ''), 'preserve-lines'));
    const detail = maDisclosure(key + ':candidate:' + candidate.id, '候选详情与引用');
    if (text.length > 220) detail.append(el('p', text, 'preserve-lines'));
    for (const citation of candidate.citations) {
      detail.append(el('blockquote', citation.quote), sourceCaption(citation.source));
    }
    card.append(detail);
    candidates.append(card);
  }
  panel.append(candidates, el('h4', '本轮分歧'));
  const conflicts = el('ul', null, 'ma-conflicts');
  analysis.conflicts.forEach(text => conflicts.append(el('li', text)));
  panel.append(conflicts);
  const review = maDisclosure(key + ':review', '查看复核依据');
  review.append(el('p', analysis.review_summary), el('small', '候选参考顺序 ' + analysis.ranking.join(' → ')));
  panel.append(review, el('h4', '补充检索'));
  if (!analysis.searches.length) panel.append(el('p', analysis.status === 'processing' && analysis.stage === 'conflict_detected'
    ? '正在确定可核对的证据。' : '本轮没有执行补充检索。', 'muted'));
  for (const [index, search] of analysis.searches.entries()) {
    const item = el('section', null, 'ma-search');
    item.append(el('p', search.query, 'ma-search-query'));
    item.append(el('small', search.status === 'pending' ? (analysis.status === 'failed' ? '检索未完成' : '检索中…') : '命中 ' + search.found_count + ' 条 · 相对首轮新增 ' + search.new_count + ' 条'));
    if (search.evidence.length) {
      const sources = maDisclosure(key + ':search:' + index, '查看命中片段与来源');
      for (const hit of search.evidence) sources.append(el('p', hit.text, 'preserve-lines'), sourceCaption(hit.source));
      item.append(sources);
    }
    panel.append(item);
  }
  const outcome = el('p', analysis.status === 'processing'
    ? (analysis.stage === 'finalizing' ? '证据已整理，正在生成最终回答…' : '正在核对分歧与证据…')
    : analysis.outcome, 'ma-outcome');
  outcome.setAttribute('role', 'status');
  panel.append(outcome);
  if (analysis.status === 'completed') panel.append(el('small', '终答证据池新增条目 ' + analysis.new_evidence_count + ' 条 · 模型调用 ' + analysis.model_calls + ' 次'));
  if (analysis.error) panel.append(el('p', analysis.error, 'chat-error'));
  return panel;
}

function initializeMADemo() {
  if (document.body.dataset.maDemo !== 'true') return;
  const banner = $('ma-demo-banner');
  banner.classList.remove('hidden');
  const cases = [
    ['普通回答示例', '电机轴承润滑不足应该先检查什么？'],
    ['冲突流程示例', '电机一倍频升高，就可以直接判定转子不平衡吗？'],
  ];
  for (const [label, query] of cases) {
    banner.append(button(label, () => {
      resetChat();
      showTab('search');
      $('query').value = query;
      $('query').focus();
    }));
  }
}

'use strict';
let pollTimer, unsavedResolve, previewUrl, previewKey = '', previewRequest = 0;
const pictureUrls = [];
function documentType(doc) { return ['reference','rules'].includes(doc.document_type || doc.payload?.document_type) ? (doc.document_type || doc.payload.document_type) : ((doc.rule_count || doc.payload?.rules?.length) ? 'rules' : 'reference'); }
function rememberSelection() { if (state.document) sessionStorage.setItem('rag-document:' + state.kb + ':' + state.category, state.document.id); sessionStorage.setItem('rag-category', state.category); }
function hasEdits() { return state.dirty || state.exclusionsDirty; }
function markDirty() { state.dirty = true; updateControls(); }
function currentRule() { return state.document?.payload.rules[state.ruleIndex]; }
function relevantTasks(doc, rule) {
  const seen = new Set();
  return (doc?.tasks || []).filter(task => {
    if (rule && (task.rule_ids?.length ? !task.rule_ids.includes(rule.id) : task.rule_id && task.rule_id !== rule.id)) return false;
    const key = task.operation + ':' + (task.rule_ids?.length ? [...task.rule_ids].sort().join(',') : task.rule_id || '');
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
}
function activeTasks(doc) { return (doc?.tasks || []).filter(t => ['queued','processing'].includes(t.status)); }
function statusLabel(doc, rule) {
  const pub = doc.publication || {}, info = rule ? pub.rules?.[rule.id] : null;
  if (doc.disabled) return pub.entry_count ? '停用待同步' : '已停用';
  if (doc.status !== 'ready') return doc.status === 'failed' ? '解析失败' : '解析中';
  const tasks = relevantTasks(doc, rule), active = tasks.find(t => ['queued','processing'].includes(t.status));
  if (active) return active.operation === 'parse' ? '解析中' : '入库中';
  const current = rule ? info?.current : pub.entry_count && !pub.pending_changes && pub.current_revision_count;
  if (current) return rule && !info.entry_approved && doc.description_status !== 'approved' ? '判据已生效，说明待审核' : '已生效';
  if ((rule ? rule.review_status === 'approved' : doc.description_status === 'approved') && tasks.find(t => t.status === 'failed' && t.operation !== 'parse')) return '入库失败';
  if (rule?.retired) return rule.review_status === 'approved' && tasks.some(t=>t.operation==='entry'&&t.status==='succeeded') && !info?.published ? '已停用' : '待审核停用';
  if (rule ? info?.published : pub.entry_count) return '更新待确认';
  return rule ? (rule.review_status === 'approved' ? '已审核，待生效' : '待审核') : (doc.description_status === 'approved' ? '已确认，待入库' : '待确认');
}
async function allowSwitch() {
  if (!hasEdits()) return true;
  const choice = await new Promise(resolve => { unsavedResolve = resolve; $('unsaved-dialog').showModal(); });
  if (choice === 'save') { await saveDocument(); return true; }
  if (choice === 'discard') { state.dirty = false; state.exclusionsDirty = false; await refreshCurrent(true); return true; }
  return false;
}
async function navigate(tab, category = state.category) {
  if (!await allowSwitch()) return;
  if (state.category !== category) $('document-filter').value = '';
  state.category = category; sessionStorage.setItem('rag-category', category);
  showTab(tab);
  if (tab === 'documents') { renderDocuments(); await selectInitialDocument(); }
}
async function load() {
  if (!state.token) throw new Error('请先输入访问令牌。');
  const [kbs, caps] = await Promise.all([api(state.access === 'admin' ? '/admin/knowledge-bases' : '/knowledge-bases'), api('/capabilities')]);
  state.kbs = kbs; state.capabilities = caps;
  const selected = state.kb || sessionStorage.getItem('rag-kb'); state.kb = kbs.find(k=>k.id === selected)?.id || kbs[0]?.id || null;
  $('kb-select').replaceChildren(option('', '选择知识库'), ...kbs.map(k=>option(k.id,k.name + (k.archived ? '（已归档）' : '')))); $('kb-select').value = state.kb || '';
  for (const id of ['query-device','wiki-device']) $(id).replaceChildren(option('', '全部设备类型'), ...deviceOptions().map(([id,name])=>option(id,name)));
  $('connection-state').textContent = state.access === 'admin' ? '管理与审核' : '只读查询'; $('connection-state').className = 'badge ok';
  document.querySelectorAll('.admin-only').forEach(n=>n.classList.toggle('hidden', state.access !== 'admin')); setAccess();
  await loadKb();
  try { const health = await api('/health'); $('service-model').textContent = '向量模型：' + (health.embedding?.model || '按服务配置'); } catch (_) { $('service-model').textContent = '服务已连接'; }
}
async function loadKb() {
  clearTimeout(pollTimer); state.openRequest++; state.document = null; state.documents = []; state.dirty = false; state.exclusionsDirty = false;
  state.queryRequest++; state.wikiRequest++; state.wikiRules = []; state.wikiContext = null; state.wikiRuleId = null;
  $('query-release-id').value = ''; $('wiki-release').value = ''; $('wiki-search').value = ''; $('answer-box').classList.add('hidden'); $('search-meta').textContent = ''; $('wiki-meta').textContent = '';
  empty($('search-results'), '输入问题查看已发布知识。'); empty($('wiki-list'), '请选择知识库。'); empty($('wiki-detail'), '从左侧目录选择规则。');
  const kb = state.kbs.find(k=>k.id === state.kb); $('kb-summary').textContent = kb?.current_release ? '已发布知识可供查询' : '尚未发布知识'; $('archive-kb').textContent = kb?.archived ? '恢复当前知识库' : '归档当前知识库';
  if (!state.kb) { await syncChat(); renderDocuments(); renderDocument(); return; }
  sessionStorage.setItem('rag-kb', state.kb);
  if (state.access === 'admin') { await Promise.all([loadDocuments(), loadReleases()]); if (state.activeTab === 'documents') await selectInitialDocument(); }
  else { if (state.activeTab === 'documents') showTab('search'); renderDocuments(); renderDocument(); }
  await syncChat();
  if (state.activeTab === 'wiki') await loadWiki();
}
async function loadDocuments() { const kb = state.kb; const docs = await api('/admin/documents?kb_id=' + encodeURIComponent(kb)); if (kb !== state.kb) return; state.documents = docs; renderDocuments(); }
function renderDocuments() {
  $('category-title').textContent = state.category === 'rules' ? '规则文档' : '参考资料'; $('upload-open').textContent = state.category === 'rules' ? '上传规则文档' : '上传参考资料'; $('upload-open').prepend(inlineIcon('upload'));
  const list = $('document-list'), term = $('document-filter').value.trim().toLowerCase(); list.replaceChildren();
  const docs = state.documents.filter(doc=>documentType(doc) === state.category && (doc.title + ' ' + (doc.external_id || '')).toLowerCase().includes(term));
  $('document-count').textContent = docs.length + ' 份文档';
  if (!docs.length) return empty(list, state.access !== 'admin' ? '请使用管理令牌维护文档。' : term ? '没有匹配的文档。' : '尚无文档，点击上方上传。');
  for (const doc of docs) {
    const link = button('', () => openDocument(doc.id), 'document-link' + (doc.id === state.document?.id ? ' active' : ''));
    link.dataset.documentId = doc.id; link.setAttribute('aria-pressed', String(doc.id === state.document?.id));
    link.append(el('strong', doc.title), badge(statusLabel(doc), statusLabel(doc) === '已生效' ? 'ok' : ''));
    list.append(link);
  }
}
async function selectInitialDocument() {
  const docs = state.documents.filter(doc=>documentType(doc) === state.category), remembered = sessionStorage.getItem('rag-document:' + state.kb + ':' + state.category);
  const id = docs.find(d=>d.id === state.document?.id)?.id || docs.find(d=>d.id === remembered)?.id || docs[0]?.id;
  if (id) await openDocument(id, false); else { state.document = null; renderDocument(); }
}
async function openDocument(id, guard = true) {
  if (guard && !await allowSwitch()) return;
  const request = ++state.openRequest, doc = await api('/admin/documents/' + encodeURIComponent(id));
  if (request !== state.openRequest) return;
  state.document = doc; state.category = documentType(doc); state.dirty = false; state.exclusionsDirty = false;
  state.ruleIndex = Math.min(Number(sessionStorage.getItem('rag-rule:' + id) || 0), Math.max(0, doc.payload.rules.length-1));
  state.page = Number(sessionStorage.getItem('rag-page:' + id) || 1);
  rememberSelection(); $('document-browser').classList.remove('drawer-open'); showTab('documents'); renderDocuments(); renderDocument();
  if (doc.status !== 'ready') { try { const processing = await api('/admin/documents/' + encodeURIComponent(id) + '/processing'); if (request === state.openRequest) { doc.payload.pages = processing; renderDocument(); } } catch (_) {} }
  schedulePoll();
}
function renderDocument() {
  const doc = state.document; $('document-empty').classList.toggle('hidden', !!doc); $('document-body').classList.toggle('hidden', !doc); if (!doc) return;
  const rules = documentType(doc) === 'rules'; $('document-title').textContent = doc.title;
  $('batch-open').classList.toggle('hidden', !rules); $('rules-workspace').classList.toggle('hidden', !rules); $('reference-workspace').classList.toggle('hidden', rules); $('rule-selector').classList.toggle('hidden', !rules); $('reference-action-title').classList.toggle('hidden', rules); $('rule-more').classList.toggle('hidden', !rules); $('open-source').classList.toggle('hidden', !rules);
  $('disable-document').textContent = doc.disabled ? '恢复文档使用' : '停用文档';
  if (rules) renderRules(); else { renderReference(); $('reference-summary').append(technicalDetails({'文档编号': doc.id, '文档修订': doc.latest_revision, '发布版本': doc.publication?.release_id || ''})); }
  renderOperation(); updateControls();
}
function renderRules() {
  const doc = state.document, rules = doc.payload.rules, root = $('rules-editor'); root.replaceChildren();
  state.ruleIndex = Math.min(state.ruleIndex, Math.max(0,rules.length-1));
  $('rule-picker').replaceChildren(...rules.map((r,i)=>option(String(i), r.name + ' · ' + statusLabel(doc,r)))); $('rule-picker').value = String(state.ruleIndex);
  $('previous-rule').disabled = state.ruleIndex <= 0; $('next-rule').disabled = state.ruleIndex >= rules.length-1;
  const rule = currentRule(); if (!rule) return empty(root,'未提取到规则。可在“更多”新增条目并关联原文。');
  const fields = el('div',null,'rule-title-grid'), name = input(rule.name), dtype = select(deviceOptions(),rule.device_type);
  name.addEventListener('input',()=>{rule.name=name.value;markDirty();}); dtype.addEventListener('change',()=>{rule.device_type=dtype.value;markDirty();}); fields.append(field('规则名称',name),field('适用设备',dtype)); const basic = viewSection('基本信息'); basic.append(fields); root.append(basic);
  const original = area(rule.original_text,3); original.addEventListener('input',()=>{rule.original_text=original.value;markDirty();}); const originalSection = viewSection('原文判据'); originalSection.append(original); root.append(originalSection);
  const condition=el('div'), preview=el('div',null,'condition-preview');
  const syncUnits=node=>node.metric ? {[node.metric]:metricRegistry()[node.metric]?.unit || '1'} : Object.assign({},...(node.all||node.any||[]).map(syncUnits));
  const changed=()=>{rule.units=syncUnits(rule.condition);markDirty();draw();};
  const draw=()=>{condition.replaceChildren(renderCondition(rule.condition,changed));preview.textContent=expression(rule.condition);};
  if (rule.condition) draw(); else condition.append(button('初始化判断条件',()=>{rule.condition={all:[defaultLeaf()]};changed();}));
  const conditionSection = viewSection('结构化判断条件'); const expressionDetail = el('details', null, 'text-details'); expressionDetail.append(el('summary', '查看完整表达式'), preview); conditionSection.append(condition, expressionDetail); root.append(conditionSection);
  const controls=el('div',null,'row');controls.append(button('外加“且”条件组',()=>{rule.condition={all:[rule.condition||defaultLeaf(),defaultLeaf()]};changed();}),button('外加“或”条件组',()=>{rule.condition={any:[rule.condition||defaultLeaf(),defaultLeaf()]};changed();}));conditionSection.append(controls);
  const explanations=el('details',null,'entry-explanations'); explanations.append(el('summary','现象、原因、措施与说明'));
  for (const [key,label] of [['phenomena','机理现象'],['causes','可能原因'],['actions','处理措施'],['notes','原文说明'],['review_notes','审核说明／解释口径']]) {
    const control=area((rule[key]||[]).join('\n'),2);control.addEventListener('input',()=>{rule[key]=control.value.split('\n').map(s=>s.trim()).filter(Boolean);markDirty();}); explanations.append(field(label+'（每行一项）',control));
  }
  root.append(explanations, technicalDetails({'规则编号': rule.id, '文档编号': doc.id, '文档修订': doc.latest_revision}));
  if (rule.validation_errors?.length || rule.valid === false) { const box=el('div',null,'warning-list');box.append(el('strong','需修正后保存，才能审核生效'));for(const error of rule.validation_errors||[])box.append(el('p',typeof error==='string'?error:JSON.stringify(error)));root.prepend(box); }
  if (rule.retired) root.prepend(el('p','本条目标记为停用，审核生效后从当前规则集中移除。','warning-list'));
}
function referenceUnits() {
  const payload=state.document.payload, blocks=payload.blocks;
  const max=Math.max(0,...blocks.map(b=>Number(b.page)||0), /\.pdf$/i.test(state.document.file_name) ? Number(payload.pages?.total)||0 : 0);
  if (max) return Array.from({length:max},(_,i)=>({number:i+1,title:'第 '+(i+1)+' 页',blocks:blocks.filter(b=>Number(b.page)===i+1)}));
  const units=[];
  for(const block of blocks){if(!units.length || block.heading || (block.section && block.section!==units.at(-1).section))units.push({number:units.length+1,title:block.section||block.text.slice(0,40)||'内容',section:block.section,blocks:[]});units.at(-1).blocks.push(block);}
  return units;
}
function renderReference() {
  const doc=state.document,payload=doc.payload,units=referenceUnits();state.page=Math.min(Math.max(1,state.page),Math.max(1,units.length));
  const count=payload.blocks.filter(b=>b.kind==='description'&&!b.excluded&&!payload.excluded_pages?.includes(b.page)).length;
  $('reference-summary').replaceChildren(el('strong',(units.length ? units.length+' '+(payload.blocks.some(b=>b.page)?'页／幻灯片':'个章节')+' · ':'')+count+' 块可用说明'),el('p','整份确认即可入库，不要求逐页审核。可按需抽查原页，公式等待核对内容不会自动进入检索。','muted'));
  $('excluded-pages').value=(payload.excluded_pages||[]).join(', ');$('excluded-pages').closest('details').classList.toggle('hidden',!payload.blocks.some(b=>b.page));
  const attention=$('attention-pages');attention.replaceChildren();
  let flagged=0;
  for(const unit of units){const reasons=[];if(payload.pages?.failed_pages?.[unit.number])reasons.push('解析失败');if(payload.pages?.unresolved_pages?.[unit.number])reasons.push('未提取到可靠文字');if(unit.blocks.some(b=>!b.excluded&&b.kind==='uncertain'))reasons.push('含待核对内容');if(unit.blocks.some(b=>typeof b.ocr_confidence==='number'&&b.ocr_confidence<.8))reasons.push('建议抽查OCR');if(reasons.length){flagged++;attention.append(button(unit.title+' · '+reasons.join('、'),()=>selectPage(unit.number),'attention-link'));}}
  $('attention-title').textContent=flagged ? flagged+' 页／章节需关注（不要求逐项通过）' : '没有记录到需关注页面';$('attention-panel').classList.toggle('hidden',!flagged);
  const physical=payload.blocks.some(b=>b.page)||/\.pdf$/i.test(doc.file_name);$('reference-page').classList.toggle('hidden',!physical);$('reference-section').classList.toggle('hidden',physical);$('reference-page').max=String(units.length);$('reference-page').value=String(state.page);$('reference-section').replaceChildren(...units.map(u=>option(String(u.number),u.title)));$('reference-section').value=String(state.page);$('previous-page').disabled=state.page<=1;$('next-page').disabled=state.page>=units.length;
  $('page-label').textContent=/\.pptx$/i.test(doc.file_name)?'幻灯片':/\.pdf$/i.test(doc.file_name)?'物理页码':'章节';$('page-caption').textContent=units.length?'共 '+units.length+(physical?' 页':' 节'):'';
  $('reference-pager').classList.toggle('hidden',!units.length);renderReferencePage(units[state.page-1]);
}
async function renderReferencePage(unit) {
  const doc=state.document;if(!doc)return;sessionStorage.setItem('rag-page:'+doc.id,String(state.page));
  const root=$('reference-blocks');root.replaceChildren();
  for(const block of unit?.blocks||[]){const section=el('section',null,'reference-block');section.append(el('div',block.locator,'source-caption'),badge(block.excluded?'解析器已排除':block.kind==='description'?'参考说明':'待核对，不进入检索'));
    const text=area(block.text,Math.min(12,Math.max(3,Math.ceil(block.text.length/80))));text.addEventListener('input',()=>{block.text=text.value;markDirty();});section.append(text);
    if(!block.excluded){const kind=select([['description','可检索说明'],['uncertain','待核对，暂不检索']],block.kind==='description'?'description':'uncertain');kind.addEventListener('change',()=>{block.kind=kind.value;markDirty();});section.append(field('内容处理',kind));}root.append(section);}
  if(!unit?.blocks.length)empty(root,'此页暂无可核对的识别文字，可查看原页或重新解析。');
  const pdf=/\.pdf$/i.test(doc.file_name),pptx=/\.pptx$/i.test(doc.file_name);$('recognized-text').open=!pdf;$('recognized-text').querySelector('summary').textContent=pdf?'对照本页识别文本':'查看与修正提取内容';
  $('reference-image').classList.add('hidden');$('reference-pictures').replaceChildren();$('preview-message').textContent=pdf?'正在读取原始页面…':pptx?'按幻灯片展示提取内容和已有图片；不提供完整幻灯片原页渲染。':'此格式提供标题与段落内容核对，可在“文档更多”下载原件。';
  const key=doc.id+':'+doc.latest_revision+':'+state.page,request=++previewRequest;
  if(pdf){try{if(previewKey!==key){const response=await fetch('/api/v1/admin/documents/'+encodeURIComponent(doc.id)+'/pages/'+state.page,{headers:{Authorization:'Bearer '+state.token}});if(!response.ok)throw new Error((await response.json()).detail||'原页读取失败');const blob=await response.blob();if(request!==previewRequest)return;if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);previewKey=key;}$('reference-image').src=previewUrl;$('reference-image').classList.remove('hidden');$('preview-message').textContent='原始PDF物理第 '+state.page+' 页 · 点击图片可放大';}catch(error){if(request===previewRequest)$('preview-message').textContent=errorMessage(error)+'，可下载原件核对。';}}
  if(pptx){pictureUrls.splice(0).forEach(URL.revokeObjectURL);const assets=[...new Set((unit?.blocks||[]).map(b=>b.asset).filter(a=>/^picture-\d+\.png$/.test(a||'')))];for(const asset of assets){try{const response=await fetch('/api/v1/admin/documents/'+encodeURIComponent(doc.id)+'/pictures/'+asset.match(/\d+/)[0],{headers:{Authorization:'Bearer '+state.token}});if(!response.ok){$('preview-message').textContent='部分原始图片暂不可读取，可下载原件核对；以下为已有提取内容。';continue;}const blob=await response.blob();if(request!==previewRequest)return;const url=URL.createObjectURL(blob);pictureUrls.push(url);const img=el('img');img.src=url;img.alt='幻灯片原始图像区域';img.addEventListener('click',()=>window.open(url,'_blank','noopener'));$('reference-pictures').append(img);}catch(_){}}}
  updateControls();
}
async function selectPage(page) { if(!Number.isInteger(page)||page<1||page>referenceUnits().length){$('reference-page').value=String(state.page);throw new Error('页码超出文档范围。');} if(!await allowSwitch()){$('reference-page').value=String(state.page);return;}state.page=Number(page);$('reference-page').value=String(page);$('reference-section').value=String(page);$('previous-page').disabled=page<=1;$('next-page').disabled=page>=referenceUnits().length;renderReferencePage(referenceUnits()[page-1]); }
function parseExcludedPages(){const value=$('excluded-pages').value.trim();if(!value)return [];const pages=new Set();for(const part of value.split(/[,，、\s]+/)){const m=/^(\d+)(?:-(\d+))?$/.exec(part);if(!m)throw new Error('排除页应填写 12, 15-18 这样的格式。');const a=Number(m[1]),b=Number(m[2]||m[1]);if(a<1||b<a||b-a>10000)throw new Error('排除页范围无效。');for(let i=a;i<=b;i++)pages.add(i);}return [...pages].sort((a,b)=>a-b);}
function updateControls(){
  const doc=state.document;if(!doc)return;const rule=documentType(doc)==='rules'?currentRule():null,busy=state.busy||activeTasks(doc).length>0||state.documents.some(d=>activeTasks(d).some(t=>t.operation!=='parse')),readonly=state.access!=='admin';
  const label=statusLabel(doc,rule);$('document-status').replaceChildren(badge(label,label==='已生效'?'ok':label.includes('失败')?'error':''));
  $('draft-status').textContent=hasEdits()?'有未保存修改':'';
  $('save-document').disabled=readonly||busy||doc.status!=='ready'||!hasEdits();
  $('confirm-document').textContent=label==='入库失败'?'重试入库':rule?'审核通过并生效':documentType(doc)==='rules'?'审核通过并生效':'确认整份入库';
  $('confirm-document').disabled=readonly||busy||doc.status!=='ready'||!!doc.disabled||(documentType(doc)==='rules'&&(!rule||rule.valid===false))||state.dirty;
  $('confirm-document').title=rule?'仅审核并发布当前条目：'+rule.name:'确认本份资料的可用说明';
  document.querySelectorAll('#rules-editor input,#rules-editor textarea,#rules-editor select,#rules-editor button,#reference-blocks input,#reference-blocks textarea,#reference-blocks select,#excluded-pages,#source-content input').forEach(n=>n.disabled=readonly||busy||doc.status!=='ready'||n.dataset.inactive==='true');
  $('batch-open').disabled=readonly||busy||doc.status!=='ready'||!!doc.disabled;
  for(const id of ['new-rule','reject-rule','retire-rule','reprocess-open','replace-document','disable-document'])$(id).disabled=readonly||busy;
}
function renderOperation(){
  const doc=state.document,rule=documentType(doc)==='rules'?currentRule():null,box=$('operation-status');box.replaceChildren();
  const label=statusLabel(doc,rule),task=relevantTasks(doc,rule).find(t=>['queued','processing','failed'].includes(t.status));
  if(!task&&doc.status==='failed'){box.classList.remove('hidden');box.append(el('strong','解析失败'),el('p','此历史记录没有关联的失败详情，可重新解析；已发布内容继续可用。'),button('重新解析',openReprocess));return;}
  const show=task&&(task.status!=='failed'||label.includes('失败')||task.operation==='document'||doc.status==='failed');
  box.classList.toggle('hidden',!show||label==='已生效');if(!show||label==='已生效')return;
  box.append(el('strong',task.status==='failed'?'处理失败':label),el('p',task.error||task.stage||'等待处理','preserve-lines'));
  if(task.status==='failed'){
    if(task.operation==='parse')box.append(button('重新解析',openReprocess));
    else if(task.operation==='document')box.append(button('重试状态同步',async()=>{await api('/admin/documents/'+encodeURIComponent(doc.id)+'/publish',{method:'POST',body:{expected_version:doc.edit_version}});await refreshCurrent(true);schedulePoll();}));
    else if (task.rule_ids?.length) box.append(button('重试本批 ' + task.rule_ids.length + ' 条', async () => {
      if (!task.rule_ids.every(id => doc.payload.rules.some(r => r.id === id && r.review_status === 'approved') && doc.payload.entry_reviews?.[id])) throw new Error('条目已修改，请重新打开批量审核核对。');
      await submitBatch(task.rule_ids);
    }));
    else box.append(el('small','确认结果已保留，原发布版继续可用。点击上方“重试入库”。'));
  }
}
async function saveDocument(){requireAdmin();const doc=state.document;if(!doc) return;const body={expected_version:doc.edit_version};if(documentType(doc)==='rules')body.rules=doc.payload.rules;else{body.blocks=doc.payload.blocks;if(state.exclusionsDirty)body.excluded_pages=parseExcludedPages();}const updated=await api('/admin/documents/'+encodeURIComponent(doc.id),{method:'PATCH',body});state.document={...doc,...updated};state.dirty=false;state.exclusionsDirty=false;await refreshCurrent(true);notice('修改已保存，确认后才对外生效。');}
async function confirmCurrent(){requireAdmin();const doc=state.document,rules=documentType(doc)==='rules',rule=currentRule();if(state.dirty)throw new Error('请先保存修改，再审核保存后的内容。');if(rules&&!rule)throw new Error('请选择规则条目。');state.busy=true;updateControls();try{await api('/admin/documents/'+encodeURIComponent(doc.id)+'/confirm',{method:'POST',body:{expected_version:doc.edit_version,target:rules?'rule':'description',...(rules?{rule_id:rule.id,include_context:true}:{excluded_pages:parseExcludedPages()})}});state.exclusionsDirty=false;await refreshCurrent(true);notice('确认已记录，系统自动准备索引并入库。');}finally{state.busy=false;updateControls();schedulePoll();}}
async function refreshCurrent(force=false){const doc=state.document;if(!doc)return;const request=state.openRequest,updated=await api('/admin/documents/'+encodeURIComponent(doc.id));if(request!==state.openRequest||state.document?.id!==doc.id)return;if(force||!hasEdits())state.document=updated;else{state.document.tasks=updated.tasks;state.document.publication=updated.publication;}const i=state.documents.findIndex(d=>d.id===doc.id);if(i>=0)state.documents[i]={...state.documents[i],...updated,document_type:updated.payload.document_type};renderDocuments();if(force||!hasEdits())renderDocument();else{renderOperation();updateControls();}}
function schedulePoll(){clearTimeout(pollTimer);if(activeTasks(state.document).length||state.documents.some(d=>activeTasks(d).length)){const request=state.openRequest;pollTimer=setTimeout(async()=>{try{await loadDocuments();if(request===state.openRequest)await refreshCurrent();}catch(error){notice('进度读取失败：'+errorMessage(error),true);}finally{if(request===state.openRequest)schedulePoll();}},1800);}}
async function selectRule(index){if(!await allowSwitch()){$('rule-picker').value=String(state.ruleIndex);return;}state.ruleIndex=Number(index);sessionStorage.setItem('rag-rule:'+state.document.id,String(index));renderRules();renderOperation();updateControls();}
function sourceBlocks(rule){const blocks=state.document.payload.blocks,ids=new Set(rule.block_ids||[]),indexes=blocks.map((b,i)=>ids.has(b.id)?i:-1).filter(i=>i>=0);if(!indexes.length)return [];const first=Math.min(...indexes),last=Math.max(...indexes);const boundary=b=>b.heading||/^\s*故障名称\s*[:：]/.test(b.text);let a=first,b=last+1;while(a>0&&!boundary(blocks[a]))a--;while(b<blocks.length&&!boundary(blocks[b]))b++;return blocks.slice(a,b);}
function showSource(){const rule=currentRule();if(!rule)return;const root=$('source-content');root.replaceChildren(el('h4',rule.name));for(const block of sourceBlocks(rule))root.append(el('small',block.locator,'source-caption'),el('pre',block.source_text||block.text,'preserve-lines'));const associations=el('details',null,'text-details');associations.append(el('summary','调整规则关联的原文块'));for(const block of state.document.payload.blocks.filter(b=>!b.excluded)){const check=input('','checkbox');check.checked=rule.block_ids?.includes(block.id)||false;check.addEventListener('change',()=>{rule.block_ids=check.checked?[...new Set([...(rule.block_ids||[]),block.id])]:(rule.block_ids||[]).filter(id=>id!==block.id);markDirty();});const line=el('label',null,'check-row');line.append(check,el('span',block.id+' · '+block.locator+' · '+block.text.slice(0,120)));associations.append(line);}root.append(associations);$('source-drawer').showModal();updateControls();}
async function downloadOriginal(){const doc=state.document,response=await fetch('/api/v1/admin/documents/'+encodeURIComponent(doc.id)+'/original',{headers:{Authorization:'Bearer '+state.token}});if(!response.ok)throw new Error('原件下载失败');const url=URL.createObjectURL(await response.blob()),link=el('a');link.href=url;link.download=doc.file_name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function openReprocess(){if(!state.document)return;$('reprocess-type').value=documentType(state.document);$('reprocess-dialog').showModal();}
async function waitTask(response,title){if(!response.task_id)return response;for(let i=0;i<900;i++){const task=await api('/admin/tasks/'+encodeURIComponent(response.task_id));if(task.status==='succeeded')return task.result;if(task.status==='failed')throw new Error(task.error||'任务失败');notice(title+' · '+task.stage);await new Promise(r=>setTimeout(r,1200));}throw new Error('任务仍在处理，请稍后查看。');}
async function disableCurrent(){if(!await allowSwitch())return;const doc=state.document;if(!confirm(doc.disabled?'恢复此文档已审核的内容？':'停用后从当前检索移除，历史内容保留。继续？'))return;await api('/admin/documents/'+encodeURIComponent(doc.id)+'/disable',{method:'POST',body:{disabled:!doc.disabled}});await api('/admin/documents/'+encodeURIComponent(doc.id)+'/publish',{method:'POST',body:{expected_version:doc.edit_version}});await refreshCurrent(true);schedulePoll();}

// Navigation and durable workspace selection.
document.querySelectorAll('[data-tab]').forEach(n=>n.addEventListener('click',action(()=>navigate(n.dataset.tab,n.dataset.category||state.category))));
for(const [id,choice] of [['unsaved-save','save'],['unsaved-discard','discard'],['unsaved-cancel','cancel']])$(id).addEventListener('click',()=>{$('unsaved-dialog').close();unsavedResolve?.(choice);unsavedResolve=null;});
$('unsaved-dialog').addEventListener('cancel',()=>{unsavedResolve?.('cancel');unsavedResolve=null;});
window.addEventListener('beforeunload',event=>{if(hasEdits()){event.preventDefault();event.returnValue='';}});
$('document-filter').addEventListener('input',renderDocuments);
$('choose-document').addEventListener('click',()=>$('document-browser').classList.add('drawer-open'));$('close-document-picker').addEventListener('click',()=>$('document-browser').classList.remove('drawer-open'));
$('rule-picker').addEventListener('change',action(()=>selectRule(Number($('rule-picker').value))));$('previous-rule').addEventListener('click',action(()=>selectRule(state.ruleIndex-1)));$('next-rule').addEventListener('click',action(()=>selectRule(state.ruleIndex+1)));
$('save-document').addEventListener('click',action(saveDocument));$('confirm-document').addEventListener('click',action(confirmCurrent));$('open-source').addEventListener('click',showSource);
$('previous-page').addEventListener('click',action(()=>selectPage(state.page-1)));$('next-page').addEventListener('click',action(()=>selectPage(state.page+1)));$('reference-page').addEventListener('change',action(()=>selectPage(Number($('reference-page').value))));
$('reference-page').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();action(()=>selectPage(Number($('reference-page').value)))();}});$('reference-section').addEventListener('change',action(()=>selectPage(Number($('reference-section').value))));
$('excluded-pages').addEventListener('input',()=>{state.exclusionsDirty=true;updateControls();});$('reference-image').addEventListener('click',()=>{if(previewUrl)window.open(previewUrl,'_blank','noopener');});
$('download-original').addEventListener('click',action(downloadOriginal));$('reprocess-open').addEventListener('click',openReprocess);$('disable-document').addEventListener('click',action(disableCurrent));
$('replace-document').addEventListener('click',action(async()=>{if(await allowSwitch())$('replacement-file').click();}));
$('replacement-file').addEventListener('change',action(async()=>{const file=$('replacement-file').files[0];if(!file)return;const body=new FormData();body.append('file',file);await api('/admin/documents/'+encodeURIComponent(state.document.id)+'/revisions',{method:'POST',body});$('replacement-file').value='';await refreshCurrent(true);schedulePoll();}));
$('reprocess-form').addEventListener('submit',action(async()=>{if(!await allowSwitch())return;const doc=state.document,type=$('reprocess-type').value;$('reprocess-dialog').close();await api('/admin/documents/'+encodeURIComponent(doc.id)+'/reprocess',{method:'POST',body:{document_type:type}});state.category=type;await refreshCurrent(true);rememberSelection();showTab('documents');schedulePoll();}));
$('document-audits').addEventListener('click',action(async()=>{
  const rows=await api('/admin/documents/'+encodeURIComponent(state.document.id)+'/audits');$('audit-content').replaceChildren();
  for(const row of rows){
    const record=el('section');record.append(el('p',[row.created_at,row.target,statusNames[row.decision],row.note].filter(Boolean).join(' · ')));
    if(row.entry_snapshot){const detail=el('details',null,'text-details'),snapshot=row.entry_snapshot,rule=snapshot.rule;detail.append(el('summary','查看当时审核的条目与来源'),el('h4',rule.name),el('p',rule.original_text),el('p',expression(rule.condition),'condition-preview'));for(const key of ['phenomena','causes','actions','notes','review_notes'])if(rule[key]?.length)detail.append(el('p',rule[key].join('\n'),'preserve-lines'));for(const block of snapshot.context)detail.append(el('small',block.locator),el('pre',block.source_text||block.text,'preserve-lines'));record.append(detail);}
    $('audit-content').append(record);
  }
  $('audit-dialog').showModal();
}));
$('new-rule').addEventListener('click',action(async()=>{if(!await allowSwitch())return;const id=globalThis.crypto?.randomUUID?.()||Date.now().toString(36);state.document.payload.rules.push({id:'draft-'+id,name:'新规则',device_type:'motor',original_text:'',condition:{all:[defaultLeaf()]},block_ids:[],phenomena:[],causes:[],actions:[],notes:[],review_notes:[],review_status:'pending',valid:false});state.ruleIndex=state.document.payload.rules.length-1;markDirty();renderRules();updateControls();}));
$('retire-rule').addEventListener('click',()=>{const rule=currentRule();if(!rule)return;rule.retired=!rule.retired;markDirty();renderRules();updateControls();});
$('reject-rule').addEventListener('click',action(async()=>{if(hasEdits())throw new Error('请先保存修改');const doc=state.document,rule=currentRule();if(!rule)return;await api('/admin/documents/'+encodeURIComponent(doc.id)+'/review',{method:'POST',body:{expected_version:doc.edit_version,target:'rule',rule_id:rule.id,decision:'rejected'}});await refreshCurrent(true);notice('已退回当前条目，原发布内容保留。');}));
$('upload-open').addEventListener('click',action(()=>{currentKb();$('upload-title').textContent=state.category==='rules'?'上传规则文档':'上传参考资料';$('upload-dialog').showModal();}));
$('upload-form').addEventListener('submit',action(async event=>{if(!await allowSwitch())return;const body=new FormData(event.target);body.append('kb_id',currentKb());body.append('document_type',state.category);const result=await api('/admin/documents',{method:'POST',body});$('upload-dialog').close();event.target.reset();await loadDocuments();await openDocument(result.document_id,false);}));
$('new-kb').addEventListener('click',()=>$('create-dialog').showModal());$('create-kb').addEventListener('submit',action(async event=>{if(!await allowSwitch())return;state.kb=(await api('/admin/knowledge-bases',{method:'POST',body:{name:new FormData(event.target).get('name')}})).id;$('create-dialog').close();await load();}));
$('archive-kb').addEventListener('click',action(async()=>{if(!await allowSwitch())return;const kb=state.kbs.find(k=>k.id===state.kb);if(!kb)throw new Error('请先选择知识库');if(!confirm(kb.archived?'恢复此知识库？':'归档后此知识库将不可对外查询，继续？'))return;await api('/admin/knowledge-bases/'+encodeURIComponent(kb.id)+'/archive',{method:'POST',body:{archived:!kb.archived}});await load();}));
$('kb-select').addEventListener('change',action(async()=>{const kb=$('kb-select').value;if(!await allowSwitch()){$('kb-select').value=state.kb;return;}if(!changeChatKnowledgeBase(kb||null)){$('kb-select').value=state.kb||'';return;}state.kb=kb||null;await loadKb();}));
$('refresh').addEventListener('click',action(async()=>{if(!await allowSwitch())return;await load();}));
$('open-history').addEventListener('click',action(async()=>{await loadReleases();$('history-dialog').showModal();}));
for(const [id,path]of [['publish','publish'],['reindex','reindex']])$(id).addEventListener('click',action(async()=>{if(!await allowSwitch())return;if(!confirm(path==='publish'?'高级操作：发布整个知识库已审核草稿及停用状态，继续？':'重建当前发布内容的索引，继续？'))return;await waitTask(await api('/admin/'+path,{method:'POST',body:{kb_id:currentKb()}}),'准备索引');await loadReleases();await refreshCurrent(true);}));
$('restore').addEventListener('click',action(async()=>{if(!state.release||!await allowSwitch())return;if(!confirm('恢复为新版本，并将现有草稿重置为待审核，继续？'))return;await waitTask(await api('/admin/restore',{method:'POST',body:{kb_id:currentKb(),release_id:state.release.id}}),'恢复历史版本');await loadReleases();await loadDocuments();await refreshCurrent(true);}));
$('search-form').addEventListener('submit',action(()=>sendChat()));$('retrieve-evidence').addEventListener('click',action(()=>chatEvidence()));
$('wiki-refresh').addEventListener('click',action(loadWiki));$('wiki-device').addEventListener('change',action(loadWiki));$('wiki-release').addEventListener('change',action(loadWiki));$('wiki-search').addEventListener('input',renderWikiDirectory);
$('open-settings').addEventListener('click',()=>$('connection-dialog').showModal());
$('connection-form').addEventListener('submit',async event=>{event.preventDefault();$('connection-error').textContent='';state.token=$('token').value.trim();state.access=$('access-mode').value;try{await load();sessionStorage.setItem('rag-token',state.token);sessionStorage.setItem('rag-access',state.access);$('connection-dialog').close();notice('已连接');}catch(error){$('connection-error').textContent=errorMessage(error);}});
$('disconnect').addEventListener('click',()=>{sessionStorage.removeItem('rag-token');sessionStorage.removeItem('rag-access');location.reload();});
document.querySelectorAll('.menu button').forEach(n=>n.addEventListener('click',()=>n.closest('details').open=false));
document.querySelectorAll('[data-close]').forEach(n=>n.addEventListener('click',()=>$(n.dataset.close).close()));
$('token').value=state.token;$('access-mode').value=state.access;
showTab(sessionStorage.getItem('rag-tab')||'documents');if(state.token)action(load)();else $('connection-dialog').showModal();

function openBatch() {
  requireAdmin();
  if (hasEdits()) throw new Error('请先保存修改，再进行批量审核。');
  const doc = state.document;
  const root = $('batch-rows'); root.replaceChildren(); $('batch-all').checked = false; $('batch-note').value = '';
  for (const rule of doc.payload.rules) {
    const label = statusLabel(doc, rule);
    const row = el('label', null, 'batch-row'); const check = input('', 'checkbox'); check.value = rule.id;
    check.disabled = !!rule.retired || label === '已生效';
    check.addEventListener('change', updateBatch);
    const body = el('div'); body.append(el('strong', rule.name), badge(label), el('p', expression(rule.condition), 'batch-summary'));
    if (!rule.valid) body.append(el('p', (rule.validation_errors || ['校验未通过']).join('、'), 'batch-invalid'));
    row.append(check, body); root.append(row);
  }
  updateBatch(); $('batch-dialog').showModal();
}
function updateBatch() {
  const selected = [...document.querySelectorAll('#batch-rows input:checked')].map(node => node.value);
  const invalid = state.document.payload.rules.filter(rule => selected.includes(rule.id) && !rule.valid);
  $('batch-error').textContent = invalid.length ? '请修正或取消勾选这些条目：' + invalid.map(rule => rule.name).join('、') : '';
  $('batch-confirm').textContent = '审核并生效所选 ' + selected.length + ' 条';
  $('batch-confirm').disabled = !selected.length || !!invalid.length;
}
$('batch-open').addEventListener('click', action(openBatch));
$('batch-all').addEventListener('change', () => { document.querySelectorAll('#batch-rows input:not(:disabled)').forEach(node => node.checked = $('batch-all').checked); updateBatch(); });
async function submitBatch(rule_ids, note = '') {
  requireAdmin(); const doc = state.document;
  if (hasEdits()) throw new Error('请先保存修改，再审核。');
  $('batch-confirm').disabled = true; state.busy = true; updateControls();
  try {
    await api('/admin/documents/' + encodeURIComponent(doc.id) + '/confirm-batch', {method:'POST', body:{expected_version:doc.edit_version, rule_ids, note}});
    $('batch-dialog').close(); state.ruleIndex = Math.max(0, doc.payload.rules.findIndex(rule => rule.id === rule_ids[0])); await refreshCurrent(true); notice('批量审核已记录，正在统一准备索引。');
  } catch (error) { $('batch-error').textContent = errorMessage(error); throw error; }
  finally { state.busy = false; updateControls(); updateBatch(); schedulePoll(); }
}
$('batch-confirm').addEventListener('click', action(() => submitBatch([...document.querySelectorAll('#batch-rows input:checked')].map(node => node.value), $('batch-note').value)));

"use strict";
let conversation = freshChat(), sessions = [], chatRequest = 0, chatBusy = false;
let sessionLoad = 0, chatPoll, restoringChat = 0;
function freshChat() { return {id:null, title:'新对话', kb_id:state.kb, release_id:null, device_type:null, turns:[]}; }
function chatScope() { return {release_id:$('query-release-id').value.trim() || $('query-release').value || '', device_type:$('query-device').value || ''}; }
function rememberChat() {
  if (conversation.id) localStorage.setItem('rag-chat-active', conversation.id);
  else localStorage.removeItem('rag-chat-active');
}
function cancelChatView() {
  chatRequest++; sessionLoad++; state.queryRequest++; clearTimeout(chatPoll); chatBusy = false;
  $('chat-progress').textContent = ''; $('ask').disabled = false; $('retrieve-evidence').disabled = false;
  $('answer-box').classList.add('hidden'); $('evidence-panel').open = false;
}
function resetChat() {
  cancelChatView(); conversation = freshChat(); rememberChat(); $('query').value = '';
  $('query-release').value = ''; $('query-release-id').value = ''; $('query-device').value = '';
  renderChat(); renderSessions();
}
function changeChatKnowledgeBase(kb) {
  if (kb !== conversation.kb_id && conversation.id) {
    if (!confirm('切换知识库将开始新对话，当前会话已保存在历史中，继续？')) return false;
    resetChat(); conversation.kb_id = kb;
  }
  return true;
}
async function refreshSessions() {
  sessions = await api('/chat/sessions'); renderSessions();
}
function renderSessions() {
  const root = $('chat-session-list'); root.replaceChildren();
  if (!sessions.length) empty(root, '暂无历史会话');
  for (const item of sessions) {
    const node = button('', ()=>openSession(item.id), 'session-link' + (item.id === conversation.id ? ' active' : ''));
    node.append(el('strong', item.title));
    const name = state.kbs.find(k=>k.id === item.kb_id)?.name || '原知识库';
    node.append(el('small', name)); node.title = item.title; root.append(node);
  }
}
function applySession(session) {
  conversation = session; rememberChat();
  $('query-release').value = ''; $('query-release-id').value = session.release_id; $('query-device').value = session.device_type || '';
  chatBusy = session.turns.some(t=>t.status === 'pending');
  $('ask').disabled = chatBusy; $('retrieve-evidence').disabled = chatBusy;
  $('chat-progress').textContent = chatBusy ? '正在检索并生成回答…' : '';
  renderChat(); renderSessions();
  clearTimeout(chatPoll);
  if (chatBusy) {
    const id = session.id, request = chatRequest;
    chatPoll = setTimeout(action(async()=>{
      const current = await api('/chat/sessions/'+encodeURIComponent(id));
      if (request === chatRequest && id === conversation.id) applySession(current);
    }), 2000);
  }
}
async function openSession(id) {
  cancelChatView(); const load = ++sessionLoad;
  const session = await api('/chat/sessions/'+encodeURIComponent(id));
  if (load !== sessionLoad) return;
  restoringChat++;
  try {
    if (session.kb_id !== state.kb) {
      state.kb = session.kb_id; $('kb-select').value = state.kb; await loadKb();
    }
    if (load !== sessionLoad) return;
    $('query').value = ''; applySession(session);
  } finally { restoringChat--; }
}
async function syncChat() {
  if (restoringChat) return;
  await refreshSessions();
  const id = conversation.id || localStorage.getItem('rag-chat-active');
  if (id && sessions.some(s=>s.id === id)) await openSession(id);
  else { conversation = freshChat(); rememberChat(); renderChat(); }
}
function renderChat() {
  $('chat-title').textContent = conversation.title || '新对话';
  $('rename-chat').disabled = !conversation.id; $('delete-chat').disabled = !conversation.id;
  const root = $('chat-messages'); root.replaceChildren();
  if (!conversation.turns.length) empty(root, '从一个问题开始，可以继续追问原因、措施，或让回答更简洁。');
  for (const [index, turn] of conversation.turns.entries()) {
    const user = el('article', null, 'chat-message chat-user'); user.append(el('strong', '你'), el('p', turn.query, 'preserve-lines')); root.append(user);
    const assistant = el('article', null, 'chat-message chat-assistant'); assistant.append(el('strong', '知识助手'));
    if (turn.result) {
      const result = turn.result;
      assistant.append(el('div', result.answer, 'chat-answer preserve-lines'));
      if (result.status === 'clarification_required') assistant.append(badge('请补充说明'));
      if (result.status === 'insufficient_evidence') assistant.append(badge('知识依据不足', 'warn'));
      if (result.citations.length) {
        const refs = el('details', null, 'chat-sources'); refs.append(el('summary', '回答依据 · ' + result.citations.length));
        result.citations.forEach((citation, i) => {const item = el('div', null, 'citation'); item.append(el('strong', '['+(i+1)+'] '+citation.source.title), el('blockquote', citation.quote), sourceCaption(citation.source)); refs.append(item);});
        assistant.append(refs);
      }
      const evidence = el('details', null, 'chat-sources chat-evidence'); evidence.append(el('summary', '检索片段 · ' + result.items.length));
      for (const hit of result.items) {const item = el('section', null, 'citation'); item.append(el('p', hit.text, 'preserve-lines'), sourceCaption(hit.source)); evidence.append(item);}
      assistant.append(evidence, technicalDetails({'检索问题':result.retrieval_query || '本轮需要澄清', '发布版本':result.release_id, '回答模型':result.model_info?.model || '未调用生成模型'}));
    } else if (turn.error) {
      assistant.append(el('p', turn.error, 'chat-error'), button(index === conversation.turns.length-1 ? '重试本轮' : '本轮未完成', ()=>sendChat(index)));
    } else assistant.append(el('p', '正在检索并生成回答…', 'muted'));
    if (index !== conversation.turns.length-1 && turn.error) assistant.querySelector('button').disabled = true;
    root.append(assistant);
  }
  $('chat-version').textContent = conversation.release_id ? '当前对话已固定知识版本，后续追问沿用同一份资料。' : '首次发送时创建并保存会话。Enter 发送，Shift＋Enter 换行。';
}

async function sendChat(retryIndex) {
  if (chatBusy) return;
  const retry = Number.isInteger(retryIndex), scope = chatScope();
  const queryText = retry ? conversation.turns[retryIndex]?.query : $('query').value.trim();
  if (!queryText) throw new Error('请输入问题。');
  const top_k = Number($('top-k').value);
  if (!Number.isInteger(top_k) || top_k < 1 || top_k > 50) throw new Error('检索条数应为1至50的整数。');
  const request = ++chatRequest;
  chatBusy = true; $('ask').disabled = true; $('retrieve-evidence').disabled = true;
  $('chat-progress').textContent = '正在检索并生成回答…';
  try {
    if (!conversation.id) {
      const body = {kb_id:currentKb()}; if(scope.release_id) body.release_id = scope.release_id; if(scope.device_type) body.device_type = scope.device_type;
      const session = await api('/chat/sessions',{method:'POST',body});
      if (request !== chatRequest) { await refreshSessions(); return; }
      conversation = session; rememberChat();
    }
    const id = conversation.id, body = {query:queryText, top_k, request_id:globalThis.crypto?.randomUUID?.() || (Date.now().toString(36)+Math.random().toString(36).slice(2))};
    if (retry) body.retry_turn_id = conversation.turns[retryIndex].id;
    const response = api('/chat/sessions/'+id+'/messages', {method:'POST',body});
    $('query').value = '';
    const pending = {query:queryText, status:'pending', result:null, error:null};
    if (retry) conversation.turns[retryIndex] = pending; else conversation.turns.push(pending);
    renderChat();
    const session = await response;
    if (request !== chatRequest || id !== conversation.id) return;
    applySession(session); await refreshSessions();
  } catch (error) {
    if (request !== chatRequest) return;
    // Read back server state after a network interruption; never invent a saved reply.
    if (conversation.id) {
      try { applySession(await api('/chat/sessions/'+conversation.id)); await refreshSessions(); }
      catch (_) { resetChat(); }
    }
    $('query').value = queryText; notice('本轮未完成：'+errorMessage(error), true);
  } finally {
    if (request === chatRequest) {
      chatBusy = conversation.turns.some(t=>t.status === 'pending');
      $('ask').disabled = chatBusy; $('retrieve-evidence').disabled = chatBusy;
      $('chat-progress').textContent = chatBusy ? '正在检索并生成回答…' : ''; renderChat();
    }
  }
}
async function chatEvidence() {
  if (chatBusy) return;
  const last = conversation.turns.at(-1);
  if (last?.result && !$('query').value.trim()) {
    const details = [...$('chat-messages').querySelectorAll('.chat-assistant')].at(-1)?.querySelector('.chat-evidence');
    if (details) {details.open = true; details.scrollIntoView({block:'nearest'});} return;
  }
  const typed = $('query-release-id').value;
  try {if (conversation.release_id) $('query-release-id').value = conversation.release_id; await query('retrieve');}
  finally {$('query-release-id').value = typed;}
}
$('new-chat').addEventListener('click', resetChat);
$('rename-chat').addEventListener('click', action(async()=>{
  if (!conversation.id) return;
  const title = prompt('会话名称', conversation.title); if (title === null) return;
  const id = conversation.id, session = await api('/chat/sessions/'+id, {method:'PATCH',body:{title}});
  if (id === conversation.id) applySession(session); await refreshSessions();
}));
$('delete-chat').addEventListener('click', action(async()=>{
  const id = conversation.id;
  if (!id || !confirm('永久删除此会话及消息、引用记录？')) return;
  await api('/chat/sessions/'+id, {method:'DELETE'});
  if (id === conversation.id) resetChat(); await refreshSessions();
}));
$('query').addEventListener('keydown', event=>{
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {event.preventDefault(); action(()=>sendChat())();}
});
for (const id of ['query-release','query-release-id','query-device']) $(id).addEventListener('change', ()=>{
  if (id === 'query-release') $('query-release-id').value = '';
  if (!conversation.id) return;
  const scope = chatScope();
  if (scope.release_id === conversation.release_id && scope.device_type === (conversation.device_type || '')) return;
  if (!confirm('修改查询范围将开始新对话，当前会话已保存在历史中，继续？')) {
    $('query-release').value = ''; $('query-release-id').value = conversation.release_id; $('query-device').value = conversation.device_type || ''; return;
  }
  resetChat(); $('query-release-id').value = scope.release_id; $('query-device').value = scope.device_type;
});
renderChat();

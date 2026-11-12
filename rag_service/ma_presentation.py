"""Explicit candidate conclusions and evidence events for the token-owned chat UI."""
from .api_contract import ConflictAnalysis


def conflict_presentation(stage, trace, demonstration=False):
    review = trace.get('review') or {}
    if not review.get('conflicts'):
        return None
    initial_ids = set(trace['initial_evidence_ids'])
    searches = []
    for search in trace['supplementary_searches']:
        items = search.get('items', [])
        searches.append({
            'query': search['query'], 'status': search.get('status', 'pending'),
            'found_count': len(search['evidence_ids']),
            'new_count': len(set(search['evidence_ids']) - initial_ids),
            'evidence': [{'knowledge_id': item['id'], 'text': item['text'][:400],
                          'source': item['source']} for item in items[:3]],
        })
    new_count = len(set(trace.get('final_evidence_ids', [])) - initial_ids)
    cited_new = {item['knowledge_id'] for item in trace.get('citations', [])} - initial_ids
    outcome = '正在核对争议及其依据。'
    if stage == 'completed':
        outcome = ('最终回答引用了补查新增的证据。' if cited_new
                   else '最终回答引用首轮证据，补查结果保留在本轮过程记录中。')
        if not searches:
            outcome = '本轮没有可执行的补查问题，最终回答依据现有资料说明争议与限制。'
        if trace.get('status') == 'insufficient_evidence':
            outcome = '现有证据仍不足，最终回答已标明知识不足。'
    if stage == 'failed':
        outcome = '本轮证据核对尚未完成，可重试。'
    error = '证据核对或生成失败，请重试或检查模型配置。' if stage == 'failed' else None
    payload = {
        'status': {'failed': 'failed', 'completed': 'completed'}.get(stage, 'processing'),
        'stage': stage,
        'candidates': [{'id': index, 'summary': candidate['answer'][:1600],
                        'citations': candidate['citations'][:3]}
                       for index, candidate in enumerate(trace.get('candidates', []), 1)],
        'conflicts': review['conflicts'], 'ranking': review['ranking'],
        'review_summary': review['rationale'], 'searches': searches,
        'new_evidence_count': new_count, 'model_calls': trace['model_calls'],
        'outcome': outcome, 'error': error, 'demonstration': demonstration,
    }
    return ConflictAnalysis.model_validate(payload).model_dump(exclude_unset=True)

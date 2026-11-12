"""Bounded MA-RAG evidence handling and the normal persistent chat entry point."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.ma_rag import answer_with_ma_rag, enabled_from_env
from rag_service import api_contract as wire


def evidence(identifier, text=None, device='motor'):
    text = text or f'{identifier}：应核对完整判断条件及适用工况。'
    return {'id': identifier, 'kind': 'description', 'text': text,
            'device_type': device, 'knowledge_revision': 1,
            'source': {'document_id': 'doc-' + identifier, 'document_revision': 1,
                       'title': identifier + '.md', 'sha256': 'fixture-hash',
                       'locator': '第1段', 'original_quote': text},
            'semantic_score': 1., 'keyword_score': 1., 'score': 1.}


def scope(items, kb='kb-test', release='release-test'):
    return {'kb_id': kb, 'release_id': release,
            'profile': {'mode': 'test', 'model': 'fixed'}, 'items': items}


class ScriptedModel:
    def __init__(self):
        self.calls = []
        self.review = {'ranking': [3, 1, 2], 'rationale': '第三份候选考虑了适用条件。',
                       'conflicts': [], 'gaps': [], 'queries': []}
        self.on_review = None
        self.fail_review = False
        self.bad_final_quote = False

    def complete(self, messages):
        payload = json.loads(messages[-1]['content'])
        self.calls.append(payload)
        if 'history' in payload:
            result = {'query': '电机完整判断条件及处理措施', 'clarification': None}
        elif 'candidates' in payload:
            if self.on_review:
                self.on_review()
            if self.fail_review:
                raise TimeoutError('模拟评审超时')
            result = self.review
        else:
            item = payload['evidence'][0]
            quote = item['text'][:40]
            final = payload.get('working_context', {}).get('stage') == 'final'
            if final and self.bad_final_quote:
                quote = '这段引用完全不在所给证据中。'
            label = '最终回答' if final else '候选分析'
            result = {'paragraphs': [{'text': label + '需要核对资料中的完整条件。',
                                     'citations': [{'knowledge_id': item['knowledge_id'],
                                                    'quote': quote}]}]}
        return {'content': json.dumps(result, ensure_ascii=False)}


class MARAGTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trace_dir = Path(self.temp.name) / 'trace'
        self.model = ScriptedModel()
        self.found = scope([evidence('initial')])
        self.searches = []

    def run_answer(self, found=None, retrieve_more=None, **kwargs):
        def default_search(query):
            self.searches.append(query)
            return scope([evidence('supplement')])
        return answer_with_ma_rag('需要哪些判断条件？', found or self.found,
                                  lambda: self.model, retrieve_more or default_search,
                                  trace_dir=self.trace_dir, **kwargs)

    def traces(self):
        return [json.loads(path.read_text()) for path in self.trace_dir.glob('*.json')]

    def test_no_evidence_skips_every_model_call(self):
        result = self.run_answer(found=scope([]))
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['items'], [])
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.searches, [])
        self.assertEqual(self.traces()[0]['model_calls'], 0)

    def test_no_conflict_keeps_search_budget_and_existing_result_shape(self):
        # A query alone does not activate supplement retrieval without a gap/conflict.
        self.model.review['queries'] = ['不应执行的检索']
        result = self.run_answer()
        self.assertEqual(len(self.model.calls), 5)
        self.assertEqual(self.searches, [])
        self.assertEqual(result['status'], 'answered')
        final = self.model.calls[-1]
        self.assertEqual([item['candidate_id'] for item in final['working_context']['ranked_candidates']], [3, 1, 2])
        self.assertNotIn('working_context', result)
        self.assertNotIn('review', result)
        self.assertNotIn('candidates', result)
        trace = self.traces()[0]
        self.assertEqual(trace['model_calls'], 5)
        self.assertEqual(trace['status'], 'answered')
        self.assertEqual(trace['supplementary_searches'], [])

    def test_conflicts_trigger_queries_and_final_citation_uses_new_evidence(self):
        self.model.review.update(conflicts=['候选对判据是否充分给出不同结论。'],
                                 queries=['电机完整判据', '电机适用工况'])
        result = self.run_answer()
        self.assertEqual(self.searches, self.model.review['queries'])
        self.assertEqual(len(self.model.calls), 5)
        self.assertEqual([item['id'] for item in result['items']], ['supplement', 'initial'])
        self.assertEqual(result['citations'][0]['knowledge_id'], 'supplement')
        self.assertEqual(len(self.traces()[0]['supplementary_searches']), 2)

    def test_duplicate_queries_and_duplicate_passages_are_not_repeated(self):
        self.model.review.update(gaps=['缺少适用条件'], queries=['补充工况', '补充工况'])
        result = self.run_answer()
        self.assertEqual(self.searches, ['补充工况'])
        self.assertEqual(len(result['items']), 2)
        self.model.review['queries'] = ['当前问题', '当前问题']
        self.searches.clear()
        self.run_answer(retrieval_query='当前问题')
        self.assertEqual(self.searches, [])

    def test_fresh_evidence_gets_space_when_initial_evidence_exceeds_prompt_budget(self):
        initial = [evidence(f'old-{index}', f'旧证据{index}：' + '甲' * 8000)
                   for index in range(8)]
        self.model.review.update(gaps=['缺少限制条件'], queries=['补查限制条件'])
        result = self.run_answer(found=scope(initial), max_items=8)
        final_evidence = self.model.calls[-1]['evidence']
        self.assertEqual(final_evidence[0]['knowledge_id'], 'supplement')
        self.assertTrue(any(item['knowledge_id'].startswith('old-') for item in final_evidence))
        self.assertLessEqual(sum(len(item['text']) for item in final_evidence), 24000)
        self.assertTrue(all(len(item['text']) <= 6000 for item in final_evidence))
        self.assertEqual(len({item['id'] for item in result['items']}), len(result['items']))
        self.assertLessEqual(len(result['items']), 8)

    def test_both_supplement_queries_contribute_within_top_k(self):
        self.model.review.update(gaps=['缺少两类适用条件'], queries=['查询一', '查询二'])
        def retrieve_more(query):
            return scope([evidence(query + '-' + str(index)) for index in range(5)])
        result = self.run_answer(retrieve_more=retrieve_more, max_items=5)
        identifiers = {item['id'] for item in result['items']}
        self.assertLessEqual(len(identifiers), 5)
        self.assertIn('查询一-0', identifiers)
        self.assertIn('查询二-0', identifiers)
        self.assertIn('initial', identifiers)

    def test_top_k_one_remains_a_one_item_result(self):
        self.model.review.update(gaps=['缺少条件'], queries=['补充条件'])
        result = self.run_answer(max_items=1)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['citations'][0]['knowledge_id'], result['items'][0]['id'])

    def test_supplement_scope_mismatch_fails_before_final_generation(self):
        self.model.review.update(gaps=['需补充证据'], queries=['补充查询'])
        for bad in [scope([evidence('new')], kb='other'),
                    scope([evidence('new')], release='other')]:
            with self.subTest(scope=bad):
                self.model.calls.clear()
                with self.assertRaisesRegex(ValueError, '不一致'):
                    self.run_answer(retrieve_more=lambda query: bad)
                self.assertEqual(len(self.model.calls), 4)
        self.assertTrue(all(trace['status'] == 'failed' for trace in self.traces()))

    def test_invalid_review_is_rejected_without_supplement_or_final_generation(self):
        changes = [{'ranking': [1, 1, 3]}, {'ranking': [1, 2, 4]},
                   {'ranking': [True, 2, 3]}, {'ranking': [1, 2]},
                   {'queries': ['一', '二', '三']}, {'queries': ['   ']},
                   {'queries': ['问' * 4001]}]
        valid = copy.deepcopy(self.model.review)
        for change in changes:
            with self.subTest(change=change):
                self.model.review = {**valid, **change}
                self.model.calls.clear()
                with self.assertRaisesRegex(ValueError, '复核结果格式无效'):
                    self.run_answer()
                self.assertEqual(len(self.model.calls), 4)
        self.assertEqual(self.searches, [])

    def test_review_timeout_and_invented_final_citation_fail_explicitly(self):
        self.model.fail_review = True
        with self.assertRaises(TimeoutError):
            self.run_answer()
        self.model.fail_review = False
        self.model.bad_final_quote = True
        with self.assertRaisesRegex(ValueError, '引用校验'):
            self.run_answer()
        self.assertTrue(all(trace['status'] == 'failed' for trace in self.traces()))

    def test_environment_switch_defaults_off_and_rejects_invalid_values(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(enabled_from_env())
        with patch.dict(os.environ, {'MA_RAG_ENABLED': 'true'}):
            self.assertTrue(enabled_from_env())
        with patch.dict(os.environ, {'MA_RAG_ENABLED': 'maybe'}):
            with self.assertRaises(ValueError):
                enabled_from_env()


class Embedding:
    def profile(self): return {'mode': 'test', 'model': 'fixed', 'fingerprint': 'ma-http'}
    def matches(self, profile): return profile == self.profile()
    def embed(self, texts): return [[1., .5] for _ in texts]


class MAChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = ScriptedModel()
        self.environment = patch.dict(os.environ, {'MA_RAG_ENABLED': 'true'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(Settings(self.root, 'admin-token-123', 'read-token-123'),
                              embedder=Embedding(), answer_model_factory=lambda: self.model)
        self.store = self.app.state.store
        self.kb = self.store.create_kb('MA会话测试')['id']
        self.add_document('电机资料', 'motor', '电机判据包括条件和适用范围。')
        self.add_document('风机资料', 'fan', '风机判据有独立适用范围。')
        self.release = self.store.publish(self.kb, Embedding())
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.client.headers['Authorization'] = 'Bearer read-token-123'

    def add_document(self, title, device, text):
        upload = self.store.reserve_upload(self.kb, title + '.md', title.encode())
        doc = self.store.finish_upload(upload['document_id'], 1,
            {'document_type': 'reference', 'rules': [], 'warnings': [],
             'blocks': [{'id': 'b1', 'kind': 'description', 'text': text,
                         'device_type': device, 'locator': '第1段'}]})
        self.store.review(doc['id'], doc['edit_version'], 'description', 'approved')

    def create_session(self):
        response = self.client.post('/api/v1/chat/sessions', json={
            'kb_id': self.kb, 'release_id': self.release['id'], 'device_type': 'motor'})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def send(self, session, request='first', **fields):
        return self.client.post('/api/v1/chat/sessions/' + session['id'] + '/messages',
            json={'query': '电机判据是什么？', 'request_id': request, **fields})

    def test_normal_session_entry_keeps_scope_and_idempotency(self):
        session = self.create_session()
        self.model.review.update(gaps=['补查适用条件'], queries=['电机适用条件'])
        self.model.on_review = lambda: self.store.publish(self.kb, Embedding())
        from rag_service.retrieval import retrieve as original_search
        scopes = []
        def record_search(query, entries, index, embedder, top_k, device_type):
            scopes.append((query, {entry['id'] for entry in entries}, device_type))
            return original_search(query, entries, index, embedder, top_k, device_type)
        with patch('rag_service.retrieval.retrieve', side_effect=record_search):
            response = self.send(session)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()
        result = saved['turns'][0]['result']
        wire.ChatAnswer.model_validate(result)
        self.assertEqual(result['release_id'], self.release['id'])
        self.assertEqual(result['retrieval_query'], '电机判据是什么？')
        self.assertEqual(len(scopes), 2)
        self.assertTrue(all(call[2] == 'motor' for call in scopes))
        expected_ids = {entry['id'] for entry in self.release['entries']}
        self.assertTrue(all(call[1] == expected_ids for call in scopes))
        self.assertTrue(all(hit['device_type'] == 'motor' for hit in result['items']))
        self.assertEqual(len(self.model.calls), 5)
        repeated = self.send(session)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(len(self.model.calls), 5)
        read = self.client.get('/api/v1/chat/sessions/' + session['id'])
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()['turns'][0]['result'], result)
        self.assertEqual(len(self.model.calls), 5)
        for internal in ['working_context', 'ranking', 'supplementary_searches', 'model_calls']:
            self.assertNotIn(internal, result)
        traces = list((self.root / 'ma_rag').glob('*.json'))
        self.assertEqual(len(traces), 1)

    def test_failed_session_can_retry_once_without_duplicate_turn(self):
        session = self.create_session()
        self.model.fail_review = True
        failed = self.send(session).json()
        self.assertEqual(failed['turns'][0]['status'], 'failed')
        self.assertTrue(failed['turns'][0]['error'])
        self.model.fail_review = False
        retried = self.send(session, 'retry', retry_turn_id=failed['turns'][0]['id']).json()
        self.assertEqual(len(retried['turns']), 1)
        self.assertEqual(retried['turns'][0]['status'], 'succeeded')
        calls = len(self.model.calls)
        self.send(session, 'retry', retry_turn_id=failed['turns'][0]['id'])
        self.assertEqual(len(self.model.calls), calls)

    def test_real_rule_items_preserve_conditions_through_ma_session_roundtrip(self):
        from rag_service.documents import parse_document
        path = Path(__file__).resolve().parents[1] / 'rag_service/assets/rules_reference.docx'
        upload = self.store.reserve_upload(self.kb, path.name, path.read_bytes(), document_type='rules')
        doc = self.store.finish_upload(upload['document_id'], 1,
            parse_document(path.read_bytes(), path.name, document_type='rules'))
        for rule in doc['payload']['rules']:
            doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved',
                                    rule['id'], include_context=True)
        self.release = self.store.publish(self.kb, Embedding())
        original = copy.deepcopy(self.release)
        session = self.create_session()
        response = self.send(session, query='电机转子不平衡有哪些判断条件？', top_k=50)
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()['turns'][0]['result']
        wire.ChatAnswer.model_validate(saved)
        rules = [item['rule'] for item in saved['items'] if item.get('rule')]
        self.assertEqual(len(rules), 4)
        source_rules = {item['id']: item['rule'] for item in original['entries'] if item['kind'] == 'rule'}
        for rule in rules:
            self.assertEqual(rule['condition'], source_rules[rule['id']]['condition'])
            self.assertEqual(rule['units'], source_rules[rule['id']]['units'])
        restored = self.client.get('/api/v1/chat/sessions/' + session['id'])
        self.assertEqual(restored.status_code, 200, restored.text)
        wire.Conversation.model_validate(restored.json())
        self.assertEqual(self.store.release(self.kb, self.release['id']), original)

    def test_disabled_switch_preserves_single_generation(self):
        with patch.dict(os.environ, {'MA_RAG_ENABLED': 'false'}):
            app = create_app(Settings(self.root / 'disabled', 'admin-token-123', 'read-token-123'),
                             embedder=Embedding(), answer_model_factory=lambda: self.model)
        store = app.state.store
        kb = store.create_kb('原流程')['id']
        up = store.reserve_upload(kb, 'reference.md', b'fixture')
        doc = store.finish_upload(up['document_id'], 1, {'rules': [], 'warnings': [],
            'blocks': [{'id': 'b1', 'kind': 'description', 'text': '电机需要核对完整条件。', 'locator': '第1段'}]})
        store.review(doc['id'], doc['edit_version'], 'description', 'approved')
        store.publish(kb, Embedding())
        with TestClient(app) as client:
            client.headers['Authorization'] = 'Bearer read-token-123'
            response = client.post('/api/v1/chat', json={'kb_id': kb, 'query': '电机判据是什么？'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.model.calls), 1)
        self.assertNotIn('working_context', self.model.calls[0])


if __name__ == '__main__':
    unittest.main()

"""Conditional presentation, token-scoped persistence and reproducible demo cases."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from rag_service import api_contract as wire
from rag_service.chat_store import ChatStore
from scripts.ma_show_demo import CONFLICT_QUESTION, NORMAL_QUESTION, create_demo_app


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.dict(os.environ, {'MA_RAG_ENABLED': 'true'}):
            self.app = create_demo_app(self.root, delay=0)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.client.headers['Authorization'] = 'Bearer demo-read-token-123'
        self.kb = self.app.state.store.list_kbs()[0]['id']
        self.session = self.client.post('/api/v1/chat/sessions', json={'kb_id': self.kb}).json()
        self.url = '/api/v1/chat/sessions/' + self.session['id']

    def send(self, query, request_id='first', **fields):
        response = self.client.post(self.url + '/messages', json={
            'query': query, 'request_id': request_id, **fields,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_normal_answer_has_no_process_panel_payload(self):
        session = self.send(NORMAL_QUESTION)
        turn = session['turns'][0]
        self.assertEqual(turn['status'], 'succeeded')
        self.assertNotIn('analysis', turn)
        self.assertIn('润滑', turn['result']['answer'])
        self.assertTrue(turn['result']['citations'])

    def test_conflict_has_candidate_evidence_and_persisted_history(self):
        session = self.send(CONFLICT_QUESTION)
        turn = session['turns'][0]
        self.assertEqual(turn['status'], 'succeeded', turn['error'])
        analysis = turn['analysis']
        wire.ConflictAnalysis.model_validate(analysis)
        self.assertEqual(analysis['status'], 'completed')
        self.assertEqual(len(analysis['candidates']), 3)
        self.assertTrue(analysis['conflicts'])
        self.assertEqual(len(analysis['searches']), 2)
        self.assertGreater(analysis['new_evidence_count'], 0)
        self.assertTrue(analysis['demonstration'])
        self.assertTrue(all(search['status'] == 'completed' for search in analysis['searches']))
        self.assertEqual(self.client.get(self.url).json()['turns'][0]['analysis'], analysis)
        owner = ChatStore.owner('demo-read-token-123')
        self.assertEqual(ChatStore(self.root).get(owner, session['id'])['turns'][0]['analysis'], analysis)
        self.client.headers['Authorization'] = 'Bearer demo-admin-token-123'
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_process_is_available_while_final_answer_is_pending(self):
        from scripts.ma_show_demo import DemoModel
        original = DemoModel.complete
        started, release = threading.Event(), threading.Event()
        responses = []

        def blocked(model, messages):
            payload = json.loads(messages[-1]['content'])
            if payload.get('working_context', {}).get('stage') == 'final':
                started.set()
                release.wait(5)
            return original(model, messages)

        with patch.object(DemoModel, 'complete', blocked):
            thread = threading.Thread(target=lambda: responses.append(self.send(CONFLICT_QUESTION)))
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                current = self.client.get(self.url).json()['turns'][0]
                self.assertEqual(current['status'], 'pending')
                self.assertIsNone(current['result'])
                self.assertEqual(current['analysis']['stage'], 'finalizing')
                self.assertTrue(current['analysis']['conflicts'])
            finally:
                release.set()
                thread.join(5)
        self.assertEqual(responses[0]['turns'][0]['analysis']['stage'], 'completed')

    def test_knowledge_gap_alone_does_not_show_conflict_process(self):
        from scripts.ma_show_demo import DemoModel
        original = DemoModel.complete

        def gap_only(model, messages):
            payload = json.loads(messages[-1]['content'])
            if 'candidates' in payload:
                return {'content': json.dumps({'ranking': [1, 2, 3], 'rationale': '说明基本一致。',
                    'conflicts': [], 'gaps': ['可进一步检索润滑检查的说明。'], 'queries': ['电机轴承润滑不足检查']})}
            return original(model, messages)

        with patch.object(DemoModel, 'complete', gap_only):
            turn = self.send(NORMAL_QUESTION)['turns'][0]
        self.assertEqual(turn['status'], 'succeeded')
        self.assertNotIn('analysis', turn)

    def test_failure_and_retry_do_not_leave_fake_completed_analysis(self):
        from scripts.ma_show_demo import DemoModel
        original = DemoModel.complete

        def fail_final(model, messages):
            if json.loads(messages[-1]['content']).get('working_context', {}).get('stage') == 'final':
                raise ValueError('模拟最终生成失败')
            return original(model, messages)

        with patch.object(DemoModel, 'complete', fail_final):
            failed = self.send(CONFLICT_QUESTION)['turns'][0]
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['analysis']['status'], 'failed')
        self.assertEqual(failed['analysis']['stage'], 'failed')
        self.assertNotIn('正在', failed['analysis']['outcome'])
        retried = self.send(CONFLICT_QUESTION, 'retry', retry_turn_id=failed['id'])
        self.assertEqual(len(retried['turns']), 1)
        self.assertEqual(retried['turns'][0]['status'], 'succeeded')
        self.assertEqual(retried['turns'][0]['analysis']['status'], 'completed')
        repeated = self.send(CONFLICT_QUESTION, 'retry')
        self.assertEqual(repeated['turns'], retried['turns'])

    def test_regular_service_conflict_display_is_independent_of_demo_label(self):
        from rag_service.app import create_app
        from rag_service.config import Settings
        from scripts.ma_show_demo import DemoEmbedding, DemoModel
        with patch.dict(os.environ, {'MA_RAG_ENABLED': 'true'}):
            app = create_app(Settings(self.root, 'demo-admin-token-123', 'demo-read-token-123'),
                             embedder=DemoEmbedding(), answer_model_factory=lambda: DemoModel(0))
        with TestClient(app) as client:
            client.headers['Authorization'] = 'Bearer demo-read-token-123'
            response = client.post(self.url + '/messages', json={'query': CONFLICT_QUESTION, 'request_id': 'regular'})
            self.assertEqual(response.status_code, 200, response.text)
            analysis = response.json()['turns'][0]['analysis']
            self.assertTrue(analysis['conflicts'])
            self.assertFalse(analysis['demonstration'])
            self.assertNotIn('data-ma-demo="true"', client.get('/').text)

    def test_insufficient_final_evidence_is_reported_in_process(self):
        from scripts.ma_show_demo import DemoModel
        original = DemoModel.complete

        def insufficient(model, messages):
            if json.loads(messages[-1]['content']).get('working_context', {}).get('stage') == 'final':
                return {'content': '{"paragraphs":[]}'}
            return original(model, messages)

        with patch.object(DemoModel, 'complete', insufficient):
            turn = self.send(CONFLICT_QUESTION)['turns'][0]
        self.assertEqual(turn['result']['status'], 'insufficient_evidence')
        self.assertEqual(turn['result']['citations'], [])
        self.assertIn('知识不足', turn['analysis']['outcome'])

    def test_restart_marks_pending_analysis_as_interrupted(self):
        from rag_service.ma_presentation import conflict_presentation
        trace = {'initial_evidence_ids': [], 'supplementary_searches': [], 'model_calls': 4,
                 'review': {'conflicts': ['两个候选的适用条件不同。'], 'ranking': [2, 1, 3], 'rationale': '需要复核。'}}
        store = ChatStore(self.root)
        owner = ChatStore.owner('demo-read-token-123')
        store.begin(owner, self.session['id'], CONFLICT_QUESTION, 'interrupted')
        store.update_analysis(owner, self.session['id'], 'interrupted', conflict_presentation('searching', trace))
        turn = ChatStore(self.root).get(owner, self.session['id'])['turns'][0]
        self.assertEqual(turn['status'], 'failed')
        self.assertEqual(turn['analysis']['stage'], 'failed')
        self.assertIn('未完成', turn['analysis']['outcome'])
        self.assertIn('重启', turn['analysis']['error'])

    def test_demo_page_is_explicitly_labelled(self):
        response = self.client.get('/')
        self.assertIn('data-ma-demo="true"', response.text)
        self.assertIn('固定候选演示', response.text)


if __name__ == '__main__':
    unittest.main()

"""The short workflow preserves review boundaries and immutable releases."""
import copy
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.__main__ import import_legacy
from rag_service.store import StoreError


class Embedding:
    fail = False
    def profile(self): return {'mode':'test','model':'fixed','fingerprint':'test'}
    def matches(self, profile): return profile == self.profile()
    def embed(self, texts):
        if self.fail: raise ValueError('测试索引失败')
        return [[1., .5] for _ in texts]


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.emb = Embedding()
        self.app = create_app(Settings(Path(self.tmp.name), 'admin-token-123', 'read-token-123'), embedder=self.emb, model_factory=lambda: None)
        self.client = TestClient(self.app); self.addCleanup(self.client.close)
        self.client.headers['Authorization'] = 'Bearer admin-token-123'
        self.store = self.app.state.store
        self.kb = self.store.create_kb('整份确认')['id']

    def reference(self, name='参考.md'):
        up = self.store.reserve_upload(self.kb, name, name.encode(), document_type='reference')
        return self.store.finish_upload(up['document_id'], 1, {'document_type':'reference', 'rules':[], 'warnings':[], 'blocks':[
            {'id':'b1', 'page':1, 'locator':'第1页', 'kind':'description', 'text':'机械振动的研究包括系统分析以及响应计算。', 'chunk_ready':True},
            {'id':'b2', 'page':2, 'locator':'第2页', 'kind':'description', 'text':'这是人工选择排除的表格内容，单位需要重新核对。', 'chunk_ready':True},
            {'id':'b3', 'page':3, 'locator':'第3页公式', 'kind':'uncertain', 'text':'未确认的公式不得进入公共检索。', 'chunk_ready':True}]})

    def confirm(self, doc, **kwargs):
        response = self.client.post('/api/v1/admin/documents/'+doc['id']+'/confirm', json={'expected_version':doc['edit_version'], 'target':'description', **kwargs})
        self.assertEqual(response.status_code, 202, response.text)
        task = self.store.task(response.json()['task_id'])
        return task

    def test_one_confirmation_excludes_pages_and_never_publishes_other_drafts(self):
        other = self.reference('其他参考.md')
        self.store.review(other['id'], other['edit_version'], 'description', 'approved')
        doc = self.reference()
        task = self.confirm(doc, excluded_pages=[2])
        self.assertEqual(task['status'], 'succeeded', task)
        old = self.store.release(self.kb)
        self.assertEqual(len(old['entries']), 1)
        self.assertEqual(old['entries'][0]['source']['locator'], '第1页')
        self.assertEqual(old['entries'][0]['source']['document_id'], doc['id'])
        # A subsequent confirmation can include page 2 without mutating history.
        doc = self.store.document(doc['id'])
        self.assertEqual(self.confirm(doc, excluded_pages=[])['status'], 'succeeded')
        self.assertEqual(len(self.store.release(self.kb)['entries']), 2)
        self.assertEqual(self.store.release(self.kb, old['id'])['entries'], old['entries'])
        self.assertFalse(self.store.publication_status(doc['id'])['pending_changes'])

    def test_failed_index_preserves_current_release_and_retry_reuses_approval(self):
        doc = self.reference(); self.assertEqual(self.confirm(doc)['status'], 'succeeded')
        original = self.store.release(self.kb)
        doc = self.store.document(doc['id']); blocks = copy.deepcopy(doc['payload']['blocks']); blocks[0]['text'] += ' 修正后的正文。'
        doc = self.store.edit_document(doc['id'], doc['edit_version'], blocks=blocks)
        self.emb.fail = True
        self.assertEqual(self.confirm(doc)['status'], 'failed')
        self.assertEqual(self.store.release(self.kb)['id'], original['id'])
        reviewed = self.store.document(doc['id']); count = len(self.store.audits(doc['id']))
        self.assertEqual(reviewed['description_status'], 'approved')
        self.emb.fail = False
        self.assertEqual(self.confirm(reviewed)['status'], 'succeeded')
        self.assertEqual(self.store.document(doc['id'])['edit_version'], reviewed['edit_version'])
        self.assertEqual(len(self.store.audits(doc['id'])), count)

    def test_selected_rule_only_and_description_does_not_release_staged_rules(self):
        upload = import_legacy(self.store, self.kb); doc = self.store.document(upload['document_id'])
        # Rule R002 was reviewed separately but not yet published.
        doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved', 'R002')
        self.assertEqual(self.confirm(doc, target='rule', rule_id='R001')['status'], 'succeeded')
        result = self.client.get('/api/v1/rules', params={'kb_id':self.kb}).json()
        self.assertEqual([rule['id'] for rule in result['rules']], ['R001'])
        doc = self.store.document(doc['id'])
        self.assertEqual(self.confirm(doc)['status'], 'succeeded')
        result = self.client.get('/api/v1/rules', params={'kb_id':self.kb}).json()
        self.assertEqual([rule['id'] for rule in result['rules']], ['R001'])

    def test_description_enriches_published_rule_without_releasing_staged_sibling(self):
        from rag_service.documents import parse_document
        path = Path(__file__).resolve().parents[1]/'rag_service/assets/rules_reference.docx'
        up = self.store.reserve_upload(self.kb, path.name, path.read_bytes(), document_type='rules')
        doc = self.store.finish_upload(up['document_id'], 1, parse_document(path.read_bytes(), path.name, document_type='rules'))
        first, sibling = doc['payload']['rules'][:2]
        doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved', sibling['id'])
        self.confirm(doc, target='rule', rule_id=first['id'])
        doc = self.store.document(doc['id']); self.confirm(doc)
        release = self.store.release(self.kb)
        rules = [entry for entry in release['entries'] if entry['kind'] == 'rule']
        self.assertEqual([rule['id'] for rule in rules], [first['id']])
        self.assertTrue(rules[0]['rule']['causes'])
        self.assertEqual(len(release['entries']), 16)  # one rule and 15 prose-only fault entries

    def test_invalid_pages_readonly_and_stale_confirmation_are_rejected(self):
        doc = self.reference(); url = '/api/v1/admin/documents/'+doc['id']+'/confirm'
        body = {'expected_version':doc['edit_version'], 'target':'description', 'excluded_pages':[999]}
        self.assertEqual(self.client.post(url, json=body).status_code, 400)
        self.assertEqual(self.store.document(doc['id'])['description_status'], 'pending')
        self.client.headers['Authorization'] = 'Bearer read-token-123'
        self.assertEqual(self.client.post(url, json=body).status_code, 403)
        self.client.headers['Authorization'] = 'Bearer admin-token-123'
        self.confirm(doc)
        self.assertEqual(self.client.post(url, json={**body, 'excluded_pages':[]}).status_code, 409)

    def test_document_changes_before_indexing_require_new_confirmation(self):
        doc = self.reference(); reviewed = self.store.review(doc['id'], doc['edit_version'], 'description', 'approved')
        blocks = copy.deepcopy(reviewed['payload']['blocks']); blocks[0]['text'] += ' 又一次修改。'
        self.store.edit_document(doc['id'], reviewed['edit_version'], blocks=blocks)
        with self.assertRaises(StoreError):
            self.store.publish(self.kb, self.emb, document_id=doc['id'], target='description', expected_version=reviewed['edit_version'])
        self.assertIsNone(self.store.list_kbs()[0]['current_release'])

    def test_document_disable_is_scoped_and_keeps_history(self):
        doc = self.reference(); self.confirm(doc)
        old = self.store.release(self.kb)
        other = self.reference('其他参考.md'); self.store.review(other['id'], other['edit_version'], 'description', 'approved')
        self.store.disable_document(doc['id'], True); doc = self.store.document(doc['id'])
        response = self.client.post('/api/v1/admin/documents/'+doc['id']+'/publish', json={'expected_version':doc['edit_version']})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.store.task(response.json()['task_id'])['status'], 'succeeded')
        self.assertEqual(self.store.release(self.kb)['entries'], [])
        self.assertEqual(self.store.release(self.kb, old['id'])['entries'], old['entries'])


if __name__ == '__main__': unittest.main()

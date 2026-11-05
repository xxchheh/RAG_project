import copy
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.documents import parse_document
from rag_service.store import Store


class Embedding:
    fail = False
    def profile(self): return {'mode':'test','model':'batch','fingerprint':'batch'}
    def matches(self,p): return p == self.profile()
    def embed(self,texts):
        if self.fail: raise ValueError('模拟索引失败')
        return [[1.,.5] for _ in texts]


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.emb=Embedding(); self.app=create_app(Settings(Path(self.tmp.name),'admin-token-123','read-token-123'),embedder=self.emb,model_factory=lambda:None)
        self.client=TestClient(self.app); self.addCleanup(self.client.close); self.client.headers['Authorization']='Bearer admin-token-123'
        self.store=self.app.state.store; self.kb=self.store.create_kb('批量')['id']
        path=Path(__file__).resolve().parents[1]/'rag_service/assets/rules_reference.docx'
        up=self.store.reserve_upload(self.kb,path.name,path.read_bytes(),document_type='rules')
        self.doc=self.store.finish_upload(up['document_id'],1,parse_document(path.read_bytes(),path.name,document_type='rules'))
        self.ids=[rule['id'] for rule in self.doc['payload']['rules'][:2]]

    def confirm(self,doc,ids=None):
        return self.client.post('/api/v1/admin/documents/'+doc['id']+'/confirm-batch',json={'expected_version':doc['edit_version'],'rule_ids':ids or self.ids})

    def test_scoped_publish_one_release_independent_snapshots(self):
        third=self.doc['payload']['rules'][2]['id']
        doc=self.store.review(self.doc['id'],self.doc['edit_version'],'rule','approved',third,include_context=True)
        response=self.confirm(doc); self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.store.task(response.json()['task_id'])['status'],'succeeded')
        entries=self.store.release(self.kb)['entries']; self.assertEqual({e['id'] for e in entries},set(self.ids))
        self.assertTrue(all(e['rule']['causes'] for e in entries)); self.assertEqual(len(self.store.releases(self.kb)),1)
        records=[a for a in self.store.audits(doc['id']) if a['target'] in ['entry:'+key for key in self.ids]]
        self.assertEqual(len(records),2); self.assertTrue(all(a['entry_snapshot'] for a in records))
        self.assertEqual(set(Store(Path(self.tmp.name)).document_tasks(doc['id'],1)[0]['rule_ids']),set(self.ids))

    def test_invalid_member_rolls_back_entire_review(self):
        rules=copy.deepcopy(self.doc['payload']['rules']); rules[1]['condition']={'metric':'kurtosis','ge':999}
        doc=self.store.edit_document(self.doc['id'],self.doc['edit_version'],rules=rules)
        self.assertEqual(self.confirm(doc).status_code,400)
        self.assertEqual(self.store.audits(doc['id']),[])
        self.assertEqual(self.store.document(doc['id'])['payload']['rules'][0]['review_status'],'pending')

    def test_index_failure_retry_and_version_conflict(self):
        self.emb.fail=True; first=self.confirm(self.doc); self.assertEqual(first.status_code,202)
        self.assertEqual(self.store.task(first.json()['task_id'])['status'],'failed')
        doc=self.store.document(self.doc['id']); count=len(self.store.audits(doc['id']))
        self.assertIsNone(self.store.list_kbs()[0]['current_release'])
        self.assertEqual(self.confirm(self.doc).status_code,409)
        self.emb.fail=False; result=self.confirm(doc)
        self.assertEqual(self.store.task(result.json()['task_id'])['status'],'succeeded')
        self.assertEqual(len(self.store.audits(doc['id'])),count)

    def test_auth_unknown_duplicate_and_retired_are_rejected(self):
        self.client.headers['Authorization']='Bearer read-token-123'; self.assertEqual(self.confirm(self.doc).status_code,403)
        self.client.headers['Authorization']='Bearer admin-token-123'
        self.assertEqual(self.confirm(self.doc,['missing']).status_code,400)
        self.assertEqual(self.confirm(self.doc,[self.ids[0],self.ids[0]]).status_code,400)
        rules=copy.deepcopy(self.doc['payload']['rules']); rules[0]['retired']=True
        doc=self.store.edit_document(self.doc['id'],self.doc['edit_version'],rules=rules)
        self.assertEqual(self.confirm(doc).status_code,400); self.assertEqual(self.store.audits(doc['id']),[])

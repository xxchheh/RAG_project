"""Full-entry review, source invalidation, and persistent task context."""
import copy
import json
import sqlite3
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
    def profile(self): return {'mode':'test','model':'entry-review','fingerprint':'test'}
    def matches(self, p): return p == self.profile()
    def embed(self, texts):
        if self.fail: raise ValueError('模拟索引失败')
        return [[1., .5] for _ in texts]


class EntryReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.emb=Embedding()
        self.app=create_app(Settings(self.root,'admin-token-123','read-token-123'),embedder=self.emb,model_factory=lambda:None)
        self.client=TestClient(self.app);self.addCleanup(self.client.close);self.client.headers['Authorization']='Bearer admin-token-123'
        self.store=self.app.state.store;self.kb=self.store.create_kb('整条审核')['id']
        path=Path(__file__).resolve().parents[1]/'rag_service/assets/rules_reference.docx'
        up=self.store.reserve_upload(self.kb,path.name,path.read_bytes(),document_type='rules')
        self.doc=self.store.finish_upload(up['document_id'],1,parse_document(path.read_bytes(),path.name,document_type='rules'))

    def confirm(self, doc, **changes):
        body={'expected_version':doc['edit_version'],'target':'rule','rule_id':doc['payload']['rules'][0]['id'],'include_context':True,**changes}
        response=self.client.post('/api/v1/admin/documents/'+doc['id']+'/confirm',json=body)
        self.assertEqual(response.status_code,202,response.text)
        return self.store.task(response.json()['task_id'])

    def test_one_entry_approval_releases_explanations_without_approving_document(self):
        self.assertEqual(self.confirm(self.doc)['status'],'succeeded')
        doc=self.store.document(self.doc['id']);first,other=doc['payload']['rules'][:2]
        self.assertEqual(doc['description_status'],'pending')
        self.assertTrue(self.store.entry_approved(doc,first));self.assertFalse(self.store.entry_approved(doc,other))
        result=self.client.get('/api/v1/rules',params={'kb_id':self.kb}).json()
        self.assertEqual(result['count'],1);self.assertTrue(result['rules'][0]['causes']);self.assertTrue(result['rules'][0]['actions'])
        self.assertNotIn('entry_reviews',json.dumps(result))
        self.assertNotIn(other['original_text'],result['rules'][0]['source']['original_quote'])
        self.assertTrue(self.store.publication_status(doc['id'])['rules'][first['id']]['current'])
        self.assertTrue(self.store.audits(doc['id'])[0]['target'].startswith('entry:'))

    def test_source_explanation_edit_invalidates_only_dependent_entry(self):
        self.confirm(self.doc);doc=self.store.document(self.doc['id']);original=self.store.release(self.kb)
        snapshot=self.store.audits(doc['id'])[0]['entry_snapshot']
        self.assertTrue(snapshot['rule']['causes']);self.assertTrue(snapshot['context'])
        first=doc['payload']['rules'][0];blocks=copy.deepcopy(doc['payload']['blocks'])
        context=self.store.entry_body(doc,first)['context'];block_id=next(b['id'] for b in context if b['kind']=='description' and not b.get('heading') and b['id'] not in first['block_ids'])
        next(b for b in blocks if b['id']==block_id)['text']+=' 核对后的文字。'
        changed=self.store.edit_document(doc['id'],doc['edit_version'],blocks=blocks)
        self.assertEqual(changed['payload']['rules'][0]['review_status'],'pending')
        self.assertFalse(self.store.entry_approved(changed,changed['payload']['rules'][0]))
        self.assertEqual(self.store.audits(doc['id'])[0]['entry_snapshot'],snapshot)
        self.assertEqual(self.store.release(self.kb)['entries'],original['entries'])
        self.assertEqual(self.store.release(self.kb,original['id'])['entries'],original['entries'])

    def test_failed_publish_retry_reuses_full_review_and_task_links_survive_restart(self):
        self.emb.fail=True;task=self.confirm(self.doc);self.assertEqual(task['status'],'failed')
        doc=self.store.document(self.doc['id']);count=len(self.store.audits(doc['id']))
        tasks=Store(self.root).document_tasks(doc['id'],1)
        self.assertEqual(tasks[0]['rule_id'],doc['payload']['rules'][0]['id']);self.assertEqual(tasks[0]['operation'],'entry');self.assertEqual(tasks[0]['status'],'failed')
        self.emb.fail=False;self.assertEqual(self.confirm(doc)['status'],'succeeded')
        self.assertEqual(len(self.store.audits(doc['id'])),count)
        self.assertEqual(self.store.document(doc['id'])['edit_version'],doc['edit_version'])

    def test_upload_context_and_interrupted_task_recovery(self):
        response=self.client.post('/api/v1/admin/documents',data={'kb_id':self.kb,'document_type':'reference'},files={'file':('reference.md','# 正文\n\n机械振动响应是研究内容。'.encode())})
        self.assertEqual(response.status_code,202)
        doc=self.client.get('/api/v1/admin/documents/'+response.json()['document_id']).json()
        self.assertEqual(doc['tasks'][0]['operation'],'parse');self.assertEqual(doc['tasks'][0]['status'],'succeeded')
        key=self.store.new_task(document_id=doc['id'],revision=1,operation='description',expected_version=doc['edit_version'])
        recovered=Store(self.root,recover_tasks=True)
        self.assertEqual(recovered.task(key)['status'],'failed');self.assertIn('重启',recovered.task(key)['error'])

    def test_restore_clears_full_entry_draft_approval_without_rewriting_release(self):
        self.confirm(self.doc);release=self.store.release(self.kb)
        self.store.publish(self.kb,self.emb,restore_id=release['id'])
        draft=self.store.document(self.doc['id']);self.assertFalse(draft['payload'].get('entry_reviews'))
        self.assertEqual(self.store.release(self.kb,release['id'])['entries'],release['entries'])

    def test_all_sixteen_entries_can_publish_without_document_approval(self):
        doc=self.doc
        for rule in doc['payload']['rules']:
            self.confirm(doc,rule_id=rule['id'])
            doc=self.store.document(doc['id'])
        result=self.client.get('/api/v1/rules',params={'kb_id':self.kb}).json()
        self.assertEqual(result['count'],16)
        self.assertTrue(all(rule['causes'] and rule['actions'] for rule in result['rules']))
        self.assertEqual(doc['description_status'],'pending')
        self.assertFalse(self.store.publication_status(doc['id'])['pending_changes'])
        # Editing one entry's review notes does not revoke another entry.
        rules=copy.deepcopy(doc['payload']['rules']);rules[0]['review_notes'].append('人工核对说明')
        changed=self.store.edit_document(doc['id'],doc['edit_version'],rules=rules)
        self.assertFalse(self.store.entry_approved(changed,changed['payload']['rules'][0]))
        self.assertTrue(self.store.entry_approved(changed,changed['payload']['rules'][1]))

    def test_legacy_condition_only_review_still_hides_unapproved_explanations(self):
        self.confirm(self.doc,include_context=False)
        rule=self.client.get('/api/v1/rules',params={'kb_id':self.kb}).json()['rules'][0]
        self.assertEqual(rule['causes'],[]);self.assertEqual(rule['actions'],[])


if __name__=='__main__':unittest.main()

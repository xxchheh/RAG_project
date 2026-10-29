import copy
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.store import Store, StoreError
from rag_service.__main__ import import_legacy


class Embedding:
    def profile(self): return {'mode':'test','model':'fixed','fingerprint':'test'}
    def matches(self, p): return p == self.profile()
    def embed(self, texts): return [[1.,1.,.5] for _ in texts]


def payload():
    a='加速度非超额峭度大于等于3.5且小于4.5。'
    b='速度有效值大于等于2.8且小于4.5 mm/s。'
    return {'blocks':[{'id':'b1','text':'润滑不足会导致轴承磨损。','locator':'行1','kind':'description'},
                      {'id':'b2','text':a+'\n'+b,'locator':'行2','kind':'rule'}],
            'rules':[{'id':'A','name':'电机轴承规则','device_type':'motor','original_text':a,'condition':{'metric':'kurtosis','ge':3.5,'lt':4.5},'block_ids':['b2']},
                     {'id':'B','name':'电机速度规则','device_type':'motor','original_text':b,'condition':{'metric':'velocity_rms','ge':2.8,'lt':4.5},'block_ids':['b2']}], 'warnings':[]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'store')
        self.kb=self.store.create_kb('试验')['id']
        self.emb=Embedding()

    def document(self, body=None):
        p=body or payload()
        upload=self.store.reserve_upload(self.kb,'demo.md',json.dumps(p,ensure_ascii=False).encode())
        return self.store.finish_upload(upload['document_id'],1,p)

    def approve(self, doc, target='description', rule_id=None):
        return self.store.review(doc['id'],doc['edit_version'],target,'approved',rule_id)

    def test_partial_approval_never_leaks_pending_rules(self):
        doc=self.document()
        doc=self.approve(doc)
        rel=self.store.publish(self.kb,self.emb)
        self.assertEqual([e['kind'] for e in rel['entries']],['description'])
        self.assertNotIn('3.5',json.dumps(rel,ensure_ascii=False))
        # A shared source block may hold multiple individually reviewed rules.
        # Public citations expose only the approved rule's own excerpt.
        doc=self.approve(doc,'rule','A')
        rel=self.store.publish(self.kb,self.emb)
        entry=next(e for e in rel['entries'] if e['id']=='A')
        self.assertNotIn('2.8',json.dumps(entry,ensure_ascii=False))
        self.assertNotIn('B',[e['id'] for e in rel['entries']])

    def test_individual_rule_publish_and_source_scope(self):
        p=payload()
        p['blocks'][1]['text']=p['rules'][0]['original_text']
        p['blocks'].append({'id':'b3','text':p['rules'][1]['original_text'],'kind':'rule','locator':'行3'})
        p['rules'][1]['block_ids']=['b3']
        doc=self.document(p)
        self.assertTrue(doc['payload']['rules'][0]['valid'])
        doc=self.approve(doc)
        doc=self.approve(doc,'rule','A')
        rel=self.store.publish(self.kb,self.emb)
        self.assertEqual([e['id'] for e in rel['entries'] if e['kind']=='rule'],['A'])
        item=next(e for e in rel['entries'] if e['id']=='A')
        self.assertNotIn('2.8',json.dumps(item,ensure_ascii=False))
        self.assertEqual(item['source']['original_quote'],p['rules'][0]['original_text'])
        frozen=copy.deepcopy(rel)
        doc=self.approve(doc,'rule','B')
        new=self.store.publish(self.kb,self.emb)
        self.assertEqual(len(new['entries']),3)
        self.assertEqual(self.store.release(self.kb,rel['id']),frozen)

    def test_edit_invalidates_approval_and_stale_review_conflicts(self):
        doc=self.approve(self.document())
        old_version=doc['edit_version']
        blocks=copy.deepcopy(doc['payload']['blocks']);blocks[0]['text']='应当检查润滑状态。'
        changed=self.store.edit_document(doc['id'],old_version,blocks=blocks)
        self.assertEqual(changed['description_status'],'pending')
        self.assertEqual(changed['payload']['blocks'][0]['source_text'],'润滑不足会导致轴承磨损。')
        with self.assertRaises(StoreError):self.store.review(doc['id'],old_version,'description','approved')
        self.assertEqual(self.store.publish(self.kb,self.emb)['entry_count'],0)

    def test_units_and_fabricated_threshold_cannot_be_approved(self):
        p=payload();p['blocks'][1]['text']=p['rules'][0]['original_text'];p['rules']=p['rules'][:1]
        doc=self.document(p)
        rules=doc['payload']['rules'];rules[0]['condition']['ge']=3.8
        doc=self.store.edit_document(doc['id'],doc['edit_version'],rules=rules)
        self.assertFalse(doc['payload']['rules'][0]['valid'])
        with self.assertRaises(StoreError):self.approve(doc,'rule','A')
        rules[0]['condition']['ge']=3.5;rules[0]['units']={'kurtosis':'mm/s'}
        doc=self.store.edit_document(doc['id'],doc['edit_version'],rules=rules)
        self.assertFalse(doc['payload']['rules'][0]['valid'])

    def test_failed_index_preserves_release_and_concurrent_edit_aborts(self):
        doc=self.approve(self.document())
        old=self.store.publish(self.kb,self.emb)
        class Broken(Embedding):
            def embed(self,texts):raise ValueError('模拟超时')
        with self.assertRaises(ValueError):self.store.publish(self.kb,Broken())
        self.assertEqual(self.store.release(self.kb)['id'],old['id'])
        store=self.store
        class Changing(Embedding):
            def embed(self,texts):
                store.disable_document(doc['id'],True)
                return super().embed(texts)
        with self.assertRaises(StoreError):self.store.publish(self.kb,Changing())
        self.assertEqual(self.store.release(self.kb)['id'],old['id'])

    def test_disable_restore_and_external_push_identity(self):
        from rag_service.documents import parse_document
        content='润滑检查和轴承维护。'.encode()
        up=self.store.reserve_upload(self.kb,'one.md',content,'external-1')
        doc=self.store.finish_upload(up['document_id'],1,parse_document(content,'one.md'))
        doc=self.approve(doc); old=self.store.publish(self.kb,self.emb)
        same=self.store.reserve_upload(self.kb,'one.md',content,'external-1')
        self.assertTrue(same['unchanged'])
        up2=self.store.reserve_upload(self.kb,'one.md','更换润滑油。'.encode(),'external-1')
        self.assertEqual(up2['document_id'],doc['id']);self.assertEqual(up2['revision'],2)
        # In-flight/failed update leaves old publication unchanged.
        self.assertEqual(self.store.publish(self.kb,self.emb)['entries'],old['entries'])
        self.store.fail_upload(doc['id'])
        self.store.disable_document(doc['id'],True)
        gone=self.store.publish(self.kb,self.emb)
        self.assertEqual(gone['entries'],[])
        restored=self.store.publish(self.kb,self.emb,restore_id=old['id'])
        self.assertEqual(restored['entries'],old['entries'])
        self.assertNotEqual(restored['id'],old['id'])

    def test_explicit_builtin_import_is_draft_and_preserves_ids(self):
        up=import_legacy(self.store,self.kb)
        doc=self.store.document(up['document_id'])
        self.assertEqual(len(doc['payload']['rules']),16)
        self.assertEqual(doc['payload']['rules'][0]['id'],'R001')
        self.assertTrue(all(r['valid'] for r in doc['payload']['rules']),[(r['id'],r['validation_errors']) for r in doc['payload']['rules'] if not r['valid']])
        self.assertTrue(all(r['review_status']=='pending' for r in doc['payload']['rules']))
        self.assertEqual(self.store.publish(self.kb,self.emb)['entry_count'],0)

    def client(self):
        self.app=create_app(Settings(Path(self.tmp.name)/'http','admin-token-123','read-token-123'),embedder=self.emb,model_factory=lambda:None)
        c=TestClient(self.app)
        c.headers['Authorization']='Bearer admin-token-123'
        return c

    def test_http_auth_upload_review_publish_and_read(self):
        c=self.client()
        kb=c.post('/api/v1/admin/knowledge-bases',json={'name':'HTTP'}).json()['id']
        upload=c.post('/api/v1/admin/documents',data={'kb_id':kb},files={'file':('notes.md','润滑不足会引起磨损。'.encode())}).json()
        task=c.get('/api/v1/admin/tasks/'+upload['task_id']).json();self.assertEqual(task['status'],'succeeded',task)
        doc=c.get('/api/v1/admin/documents/'+upload['document_id']).json()
        review=c.post('/api/v1/admin/documents/'+doc['id']+'/review',json={'expected_version':doc['edit_version'],'target':'description','decision':'approved'})
        self.assertEqual(review.status_code,200,review.text)
        task=c.post('/api/v1/admin/publish',json={'kb_id':kb}).json()
        self.assertEqual(c.get('/api/v1/admin/tasks/'+task['task_id']).json()['status'],'succeeded')
        c.headers['Authorization']='Bearer read-token-123'
        hits=c.post('/api/v1/retrieve',json={'kb_id':kb,'query':'润滑'}).json()
        self.assertEqual(len(hits['items']),1)
        self.assertNotIn(str(self.tmp.name),json.dumps(hits))
        detail=c.get('/api/v1/knowledge/'+hits['items'][0]['id'],params={'kb_id':kb,'release_id':hits['release_id']})
        self.assertEqual(detail.status_code,200)
        self.assertEqual(c.post('/api/v1/admin/publish',json={'kb_id':kb}).status_code,403)
        self.assertEqual(c.get('/api/v1/admin/documents',params={'kb_id':kb}).status_code,403)
        c.headers.clear();self.assertEqual(c.get('/api/v1/knowledge-bases').status_code,401)
        self.assertEqual(c.get('/').status_code,200)
        schema=c.get('/openapi.json').json();self.assertIn('/api/v1/rules',schema['paths'])

    def test_failed_upload_and_unknown_release_do_not_report_success(self):
        c=self.client();kb=c.post('/api/v1/admin/knowledge-bases',json={'name':'坏文件'}).json()['id']
        result=c.post('/api/v1/admin/documents',data={'kb_id':kb},files={'file':('bad.pdf',b'not a pdf')}).json()
        task=c.get('/api/v1/admin/tasks/'+result['task_id']).json()
        self.assertEqual(task['status'],'failed');self.assertTrue(task['error'])
        self.assertEqual(c.get('/api/v1/rules',params={'kb_id':kb,'release_id':'unknown'}).status_code,404)
        self.assertEqual(c.post('/api/v1/retrieve',json={'kb_id':kb,'query':''}).status_code,422)
        self.assertEqual(c.post('/api/v1/admin/documents',data={'kb_id':kb},files={'file':('bad.exe',b'1')}).status_code,400)


if __name__=='__main__':unittest.main()

"""Real rule-document results must survive save/read/response validation."""
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.documents import parse_document
from rag_service.chat_store import ChatStore, repair_legacy_conditions
from rag_service import api_contract as wire
from rag_service.contracts import normalize_condition
import test_chat as fixtures


class EvidenceModel:
    def complete(self, messages):
        payload = json.loads(messages[-1]['content'])
        if 'history' in payload:
            value = {'query':'电机转子不平衡的原因', 'clarification':None}
        else:
            hit = payload['evidence'][0]
            value = {'paragraphs':[{'text':'可参考资料中的故障判据。','citations':[{'knowledge_id':hit['knowledge_id'],'quote':hit['text'][:40]}]}]}
        return {'content':json.dumps(value,ensure_ascii=False)}


class RuleConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.app=create_app(Settings(self.root,'admin-token-123','read-token-123'),embedder=fixtures.Embedding(),answer_model_factory=EvidenceModel)
        self.client=TestClient(self.app,raise_server_exceptions=False);self.addCleanup(self.client.close)
        self.client.headers['Authorization']='Bearer read-token-123'
        self.store=self.app.state.store;self.kb=self.store.create_kb('真实规则文档回归')['id']
        path=Path(__file__).resolve().parents[1]/'rag_service/assets/rules_reference.docx'
        up=self.store.reserve_upload(self.kb,path.name,path.read_bytes(),document_type='rules')
        doc=self.store.finish_upload(up['document_id'],1,parse_document(path.read_bytes(),path.name,document_type='rules'))
        for rule in doc['payload']['rules']:
            doc=self.store.review(doc['id'],doc['edit_version'],'rule','approved',rule['id'],include_context=True)
        self.release=self.store.publish(self.kb,fixtures.Embedding())
        self.session=self.client.post('/api/v1/chat/sessions',json={'kb_id':self.kb}).json()
        self.path='/api/v1/chat/sessions/'+self.session['id']

    def ask(self, identifier='first'):
        return self.client.post(self.path+'/messages',json={'query':'电机转子不平衡有哪些原因？','request_id':identifier,'top_k':50})

    def test_real_rules_roundtrip_and_followup(self):
        first=self.ask()
        self.assertEqual(first.status_code,200,first.text)
        saved=first.json()['turns'][0]['result']
        self.assertTrue(any(item.get('rule') for item in saved['items']))
        # The public session JSON itself must also remain valid on revalidation.
        wire.ChatAnswer.model_validate(saved)
        read=self.client.get(self.path)
        self.assertEqual(read.status_code,200,read.text)
        self.assertEqual(read.json()['turns'][0]['result']['citations'],saved['citations'])
        followup=self.ask('followup')
        self.assertEqual(followup.status_code,200,followup.text)
        self.assertEqual(len(followup.json()['turns']),2)
        self.assertEqual(followup.json()['release_id'],self.release['id'])

    def test_old_null_padded_rule_snapshots_recover_without_replacing_release(self):
        original=self.ask().json()
        chats=ChatStore(self.root);owner=chats.owner('read-token-123')
        with chats.connection() as c:
            body=chats.read(c,owner,self.session['id'])
            result=body['turns'][0]['result']
            # Reproduce the previous model_dump() representation including defaults.
            body['turns'][0]['result']=wire.ChatAnswer.model_validate(result).model_dump()
            raw=json.dumps(body,ensure_ascii=False)
            c.execute('UPDATE conversations SET body=? WHERE id=?',(raw,self.session['id']))
        old_updated=body['updated_at']
        response=self.client.get(self.path)
        self.assertEqual(response.status_code,200,response.text)
        wire.Conversation.model_validate(response.json())
        restarted=ChatStore(self.root)
        with restarted.connection() as c:
            persisted=json.loads(c.execute('SELECT body FROM conversations WHERE id=?',(self.session['id'],)).fetchone()['body'])
        wire.Conversation.model_validate(persisted)
        self.assertEqual(persisted['updated_at'],old_updated)
        self.assertEqual(persisted['release_id'],original['release_id'])
        self.assertEqual(persisted['turns'][0]['result']['citations'],original['turns'][0]['result']['citations'])
        self.assertEqual(self.store.release(self.kb,self.release['id']),self.release)
        self.assertEqual(self.ask('continued').status_code,200)

    def test_real_sixteen_rules_keep_strict_threshold_semantics(self):
        rules=[entry['rule'] for entry in self.release['entries'] if entry['kind']=='rule']
        self.assertEqual(len(rules),16)
        for rule in rules:
            checked=wire.Rule.model_validate(rule)
            normalize_condition(checked.model_dump(exclude_unset=True)['condition'])
        with self.assertRaises(ValueError):
            normalize_condition({'metric':'h1_ratio','ge':0.6,'gt':0.7})
        with self.assertRaises(ValueError):
            normalize_condition({'metric':'h1_ratio','ge':None})


class LegacyConditionRepairTests(unittest.TestCase):
    def test_only_null_comparison_defaults_are_removed(self):
        zero = {'metric':'h1_ratio','ge':0,'gt':None,'le':None,'lt':1}
        duplicate = {'metric':'h1_ratio','ge':0.6,'gt':0.7,'le':None,'lt':None}
        condition = {'all':[{'any':[zero]},duplicate]}
        session = {'turns':[{'query':'原问题','result':{'items':[{'rule':{'condition':condition}}],
                                                        'profile':{'unrelated':None}}}]}
        self.assertTrue(repair_legacy_conditions(session))
        self.assertEqual(zero,{'metric':'h1_ratio','ge':0,'lt':1})
        self.assertEqual(duplicate,{'metric':'h1_ratio','ge':0.6,'gt':0.7})
        self.assertIsNone(session['turns'][0]['result']['profile']['unrelated'])
        with self.assertRaises(ValueError): normalize_condition(condition)
        self.assertFalse(repair_legacy_conditions(session))

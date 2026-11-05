import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.chat import bounded_history, resolve_question


class Embedding:
    def profile(self): return {'mode':'test','model':'fixed','fingerprint':'chat'}
    def matches(self, profile): return profile == self.profile()
    def embed(self, texts): return [[1., .5] for _ in texts]


class Model:
    def __init__(self):
        self.calls = []
        self.identifier = ''
        self.rewrite = {'query':'电机轴承润滑不足的检查和处理措施', 'clarification':None}
        self.fail = False
    def complete(self, messages):
        self.calls.append(messages)
        if self.fail: raise TimeoutError('模拟模型超时')
        payload = json.loads(messages[-1]['content'])
        if 'history' in payload: result = self.rewrite
        else: result = {'paragraphs':[{'text':'应检查润滑油并及时处理。', 'citations':[{'knowledge_id':self.identifier,'quote':'应检查润滑油，必要时更换。'}]}]}
        return {'content':json.dumps(result,ensure_ascii=False)}


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model = Model()
        self.app = create_app(Settings(Path(self.temp.name),'admin-token-123','read-token-123'),embedder=Embedding(),answer_model_factory=lambda:self.model)
        self.store = self.app.state.store
        self.kb = self.store.create_kb('聊天验收')['id']
        upload = self.store.reserve_upload(self.kb,'资料.md',b'source')
        doc = self.store.finish_upload(upload['document_id'],1,{'rules':[],'warnings':[],'blocks':[{'id':'b1','kind':'description','text':'电机轴承润滑不足会加速磨损。应检查润滑油，必要时更换。','locator':'第1段'}]})
        self.store.review(doc['id'],doc['edit_version'],'description','approved')
        self.release = self.store.publish(self.kb,Embedding())
        self.model.identifier = self.release['entries'][0]['id']
        self.client = TestClient(self.app,raise_server_exceptions=False)
        self.client.headers['Authorization'] = 'Bearer read-token-123'
        self.body = {'kb_id':self.kb,'query':'电机轴承润滑不足怎么办？'}
    def tearDown(self):
        self.client.close(); self.temp.cleanup()
    def post(self, **changes): return self.client.post('/api/v1/chat',json={**self.body,**changes})
    def test_first_turn_and_followup_use_current_evidence(self):
        first = self.post().json()
        self.assertEqual(first['status'],'answered')
        self.assertEqual(len(self.model.calls),1)
        history = [{'query':self.body['query'],'answer':first['answer']}]
        response = self.post(query='这种情况呢？',history=history,release_id=first['release_id'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(self.model.calls),3)
        result = response.json()
        self.assertEqual(result['retrieval_query'],self.model.rewrite['query'])
        prompt = json.loads(self.model.calls[-1][-1]['content'])
        self.assertEqual(prompt['question'],'这种情况呢？')
        self.assertEqual(prompt['conversation_context'],history)
        self.assertEqual(result['citations'][0]['source']['document_revision'],1)
    def test_clarification_and_topic_change(self):
        self.model.rewrite = {'query':None,'clarification':'你指的是轴承还是转子？'}
        history = [{'query':'比较两种故障','answer':'轴承与转子问题。'}]
        result = self.post(query='它怎么处理',history=history).json()
        self.assertEqual(result['status'],'clarification_required')
        self.assertEqual(result['items'],[])
        self.assertEqual(result['citations'],[])
        self.model.rewrite = {'query':'水泵汽蚀的原因','clarification':None}
        self.assertEqual(self.post(query='换个话题，水泵汽蚀呢？',history=history).json()['retrieval_query'],'水泵汽蚀的原因')
    def test_fixed_version_survives_new_publication(self):
        old = self.release['id']
        self.store.publish(self.kb,Embedding())
        self.assertEqual(self.post(release_id=old).json()['release_id'],old)
        before = len(self.model.calls)
        self.assertEqual(self.post(release_id='missing').status_code,404)
        self.assertEqual(len(self.model.calls),before)
    def test_auth_validation_failure_and_retry(self):
        self.client.headers.clear()
        self.assertEqual(self.post().status_code,401)
        self.client.headers['Authorization'] = 'Bearer read-token-123'
        self.assertEqual(self.post(history=[{'query':'x'}]).status_code,422)
        self.model.fail = True
        self.assertNotEqual(self.post().status_code,200)
        self.model.fail = False
        self.assertEqual(self.post().json()['status'],'answered')
    def test_bad_rewrite_and_bad_citations_fail(self):
        self.model.rewrite = {'query':'','clarification':None}
        self.assertEqual(self.post(history=[{'query':'原因','answer':'轴承'}]).status_code,400)
        self.model.identifier = 'invented'
        self.assertEqual(self.post().status_code,400)
    def test_no_applicable_evidence_is_not_a_successful_answer(self):
        result = self.post(device_type='fan').json()
        self.assertEqual(result['status'],'insufficient_evidence')
        self.assertEqual(result['items'],[])
        self.assertEqual(result['citations'],[])
        self.assertEqual(self.model.calls,[])
    def test_history_limits_preserve_whole_rounds(self):
        turns = [{'query':str(i),'answer':'x'} for i in range(9)]
        self.assertEqual(bounded_history(turns),turns[-6:])
        self.assertEqual(bounded_history([{'query':'x','answer':'a'*12000}]),[])
        model = Model()
        self.assertEqual(resolve_question('首次问题',[],lambda:model),('首次问题',None))
        self.assertEqual(model.calls,[])


if __name__ == '__main__': unittest.main()

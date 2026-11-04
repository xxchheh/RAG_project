"""Opt-in answer providers do not alter the Agent's source-bound answer contract."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.models import ChatModel, ModelConfig


class Embedding:
    def profile(self): return {'mode':'test', 'model':'fixed', 'fingerprint':'answer-config'}
    def matches(self, profile): return profile == self.profile()
    def embed(self, texts): return [[1., .5] for _ in texts]


class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()


class AnswerModelTests(unittest.TestCase):
    def configured(self, values):
        with patch.dict(os.environ, values, clear=True), patch('rag_service.models.load_dotenv'):
            return ModelConfig.for_answers()

    def test_unconfigured_or_blank_answers_keep_legacy_config(self):
        env = {'LLM_PROVIDER':'deepseek','LLM_MODEL':'legacy-model','LLM_API_KEY':'legacy-key','LLM_TIMEOUT':'22'}
        for extra in ({}, {'ANSWER_LLM_PROVIDER':'','ANSWER_LLM_MODEL':'','ANSWER_LLM_API_KEY':''}):
            config = self.configured({**env, **extra})
            self.assertEqual((config.provider, config.model, config.api_key, config.timeout), ('deepseek','legacy-model','legacy-key',22))

    def test_independent_qwen_never_inherits_deepseek_credentials(self):
        config = self.configured({'LLM_PROVIDER':'deepseek','LLM_BASE_URL':'https://old.example/v1', 'LLM_MODEL':'old-model','LLM_API_KEY':'old-key',
                                  'ANSWER_LLM_PROVIDER':'qwen','ANSWER_LLM_MODEL':'answer-model','ANSWER_LLM_API_KEY':'answer-key'})
        self.assertEqual(config.base_url, 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.assertEqual((config.model, config.api_key), ('answer-model','answer-key'))
        self.assertEqual(config.timeout,90)

    def test_partial_answer_config_requires_its_own_model_and_key(self):
        for extra in ({'ANSWER_LLM_PROVIDER':'qwen'}, {'ANSWER_LLM_MODEL':'new-model'}, {'ANSWER_LLM_TIMEOUT':'30'}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, 'ANSWER_LLM_MODEL.*ANSWER_LLM_API_KEY'):
                self.configured({'LLM_PROVIDER':'deepseek','LLM_MODEL':'old','LLM_API_KEY':'old-key', **extra})

    def test_answer_validation_and_provider_default(self):
        base={'ANSWER_LLM_MODEL':'answer-model','ANSWER_LLM_API_KEY':'answer-key'}
        self.assertEqual(self.configured(base).provider,'deepseek')
        for extra in ({'ANSWER_LLM_PROVIDER':'invalid'}, {'ANSWER_LLM_TIMEOUT':'nan'}, {'ANSWER_LLM_TIMEOUT':'0'},
                      {'ANSWER_LLM_BASE_URL':'http://remote.test/v1'}, {'ANSWER_LLM_BASE_URL':'https://u:p@example.test/v1'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError): self.configured({**base,**extra})

    def test_deepseek_and_qwen_chat_completions_wire_format(self):
        for provider in ('deepseek','qwen'):
            config=self.configured({'ANSWER_LLM_PROVIDER':provider,'ANSWER_LLM_MODEL':'answer-model','ANSWER_LLM_API_KEY':'answer-key'})
            captured=[]
            def call(request,timeout):
                captured.append(request)
                return Response({'choices':[{'message':{'role':'assistant','content':'{"citations":[]}'}}]})
            with patch('rag_service.models.urlopen',side_effect=call):
                message=ChatModel(config).complete([{'role':'user','content':'问题'}])
            request=captured[0]; body=json.loads(request.data)
            self.assertEqual(request.full_url,config.base_url+'/chat/completions')
            self.assertEqual(request.get_header('Authorization'),'Bearer answer-key')
            self.assertEqual(body['model'],'answer-model'); self.assertFalse(body['stream'])
            if provider=='deepseek': self.assertEqual(body['thinking'],{'type':'disabled'})
            else: self.assertFalse(body['enable_thinking'])
            self.assertEqual(message['role'],'assistant')

    def test_answer_endpoint_uses_independent_config_and_validates_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Settings(Path(folder),'admin-token-123','read-token-123'),embedder=Embedding())
            store=app.state.store; kb=store.create_kb('回答配置')['id']
            up=store.reserve_upload(kb,'参考.md',b'reference')
            doc=store.finish_upload(up['document_id'],1,{'rules':[],'warnings':[],'blocks':[{'id':'b1','kind':'description','locator':'第1段','text':'轴承应定期检查润滑状态。'}]})
            store.review(doc['id'],doc['edit_version'],'description','approved')
            release=store.publish(kb,Embedding())
            entry=release['entries'][0]; quote=entry['text']
            class Answer:
                def complete(self,messages):
                    return {'content':json.dumps({'citations':[{'knowledge_id':entry['id'],'quote':quote}]})}
            env={'ANSWER_LLM_PROVIDER':'qwen','ANSWER_LLM_MODEL':'qa-model','ANSWER_LLM_API_KEY':'qa-private'}
            with TestClient(app) as c, patch.dict(os.environ,env,clear=True), patch('rag_service.models.load_dotenv'), patch('rag_service.app.ChatModel',return_value=Answer()) as factory:
                c.headers['Authorization']='Bearer read-token-123'
                result=c.post('/api/v1/answers',json={'kb_id':kb,'release_id':release['id'],'query':'轴承需要如何保养？'})
                self.assertEqual(result.status_code,200,result.text)
                data=result.json(); self.assertEqual(data['status'],'answered')
                self.assertEqual(data['citations'][0]['quote'],quote)
                self.assertEqual(data['release_id'],release['id'])
                self.assertEqual(factory.call_args[0][0].provider,'qwen')
                self.assertNotIn('qa-private',result.text)

    def test_empty_evidence_does_not_construct_answer_model(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(Settings(Path(folder),'admin-token-123','read-token-123'),embedder=Embedding(),answer_model_factory=lambda: self.fail('无依据时不应调用模型'))
            kb=app.state.store.create_kb('空库')['id']; app.state.store.publish(kb,Embedding())
            with TestClient(app) as c:
                c.headers['Authorization']='Bearer read-token-123'
                data=c.post('/api/v1/answers',json={'kb_id':kb,'query':'任意问题'}).json()
                self.assertEqual(data['status'],'insufficient_evidence'); self.assertEqual(data['citations'],[])


if __name__=='__main__': unittest.main()

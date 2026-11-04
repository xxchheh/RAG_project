import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.generated_answers import generate_answer


class Model:
    def __init__(self, result): self.result, self.calls = result, 0
    def complete(self, messages):
        self.calls += 1
        return {'content': json.dumps(self.result, ensure_ascii=False)}


class Embedding:
    def profile(self): return {'mode':'test','model':'fixed','fingerprint':'generated'}
    def matches(self, p): return p == self.profile()
    def embed(self, texts): return [[1., .5] for _ in texts]


class GeneratedAnswerTests(unittest.TestCase):
    def setUp(self):
        self.hit = {'id':'k1','text':'润滑不足会加速磨损。应检查润滑油，必要时更换。',
                    'source':{'document_id':'d1','document_revision':1,'title':'资料.md','sha256':'hash','locator':'第1段','original_quote':'润滑不足会加速磨损。应检查润滑油，必要时更换。'}}

    def result(self):
        return {'paragraphs':[{'text':'从资料来看，应把润滑检查作为排查重点，发现问题后及时处理。',
                               'citations':[{'knowledge_id':'k1','quote':'润滑不足会加速磨损。应检查润滑油，必要时更换。'}]}]}

    def test_synthesis_always_calls_model_and_keeps_literal_sources(self):
        model = Model(self.result())
        result = generate_answer('润滑不足应该如何处理？',[self.hit],lambda:model)
        self.assertEqual(model.calls,1)
        self.assertEqual(result['mode'],'grounded_generation')
        self.assertIn('排查重点',result['answer']); self.assertIn('[1]',result['answer'])
        self.assertEqual(result['citations'][0]['source'],self.hit['source'])
        self.assertIn(result['citations'][0]['quote'],self.hit['text'])

    def test_bad_citation_or_unreferenced_paragraph_is_rejected(self):
        for citations in ([],[{'knowledge_id':'invented','quote':'润滑不足'}],[{'knowledge_id':'k1','quote':'不在原文中的内容'}]):
            result=self.result(); result['paragraphs'][0]['citations']=citations
            with self.subTest(citations=citations), self.assertRaisesRegex(ValueError,'引用校验'):
                generate_answer('问题',[self.hit],lambda:Model(result))

    def test_no_evidence_skips_model_and_model_can_report_insufficient(self):
        def unexpected(): self.fail('空依据不应调用模型')
        self.assertEqual(generate_answer('问题',[],unexpected)['status'],'insufficient_evidence')
        self.assertEqual(generate_answer('问题',[self.hit],lambda:Model({'paragraphs':[]}))['status'],'insufficient_evidence')

    def test_prompt_clipping_cannot_be_bypassed_by_quote_from_unseen_tail(self):
        hit={**self.hit,'text':'前文'*4000+'不在传入证据里的尾部'}
        result=self.result(); result['paragraphs'][0]['citations']=[{'knowledge_id':'k1','quote':'不在传入证据里的尾部'}]
        with self.assertRaises(ValueError): generate_answer('问题',[hit],lambda:Model(result))

    def test_generated_endpoint_keeps_release_auth_and_old_answer_contract(self):
        with tempfile.TemporaryDirectory() as root:
            model=Model(self.result())
            app=create_app(Settings(Path(root),'admin-token-123','read-token-123'),embedder=Embedding(),answer_model_factory=lambda:model)
            store=app.state.store; kb=store.create_kb('综合回答')['id']
            up=store.reserve_upload(kb,'资料.md',b'source')
            doc=store.finish_upload(up['document_id'],1,{'rules':[],'warnings':[],'blocks':[{'id':'b1','kind':'description','text':self.hit['text'],'locator':'第1段'}]})
            store.review(doc['id'],doc['edit_version'],'description','approved'); release=store.publish(kb,Embedding())
            model.result['paragraphs'][0]['citations'][0]['knowledge_id']=release['entries'][0]['id']
            with TestClient(app) as c:
                body={'kb_id':kb,'release_id':release['id'],'query':'润滑不足应该如何处理？'}
                self.assertEqual(c.post('/api/v1/generated-answers',json=body).status_code,401)
                c.headers['Authorization']='Bearer read-token-123'
                response=c.post('/api/v1/generated-answers',json=body)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['release_id'],release['id'])
                self.assertEqual(response.json()['mode'],'grounded_generation')
                self.assertEqual(c.post('/api/v1/generated-answers',json={**body,'release_id':'missing'}).status_code,404)
                self.assertEqual(c.post('/api/v1/generated-answers',json={**body,'extra':True}).status_code,422)


if __name__=='__main__': unittest.main()

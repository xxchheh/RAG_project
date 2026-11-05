import threading
import unittest
import test_chat as fixtures
from rag_service.app import create_app
from rag_service.chat_store import ChatStore
from rag_service.config import Settings
from fastapi.testclient import TestClient
from pathlib import Path


class SessionTests(unittest.TestCase):
    setUp = fixtures.ChatTests.setUp
    tearDown = fixtures.ChatTests.tearDown

    def create(self, **fields):
        r = self.client.post('/api/v1/chat/sessions',json={'kb_id':self.kb, **fields})
        self.assertEqual(r.status_code,201,r.text)
        return r.json()

    def message(self, session, request='first', **fields):
        return self.client.post('/api/v1/chat/sessions/'+session['id']+'/messages',json={'request_id':request,'query':'电机轴承润滑不足怎么办？',**fields})

    def test_title_rename_reopen_delete_and_saved_citations(self):
        session = self.create()
        response = self.message(session)
        self.assertEqual(response.status_code,200,response.text)
        value = response.json()
        self.assertEqual(value['title'],'电机轴承润滑不足怎么办？')
        self.assertEqual(value['turns'][0]['result']['status'],'answered')
        path = '/api/v1/chat/sessions/'+session['id']
        self.assertEqual(self.client.patch(path,json={'title':'维护检查'}).json()['title'],'维护检查')
        self.assertEqual(self.client.patch(path,json={'title':'   '}).status_code,400)
        app = create_app(Settings(Path(self.temp.name),'admin-token-123','read-token-123'),embedder=fixtures.Embedding(),answer_model_factory=lambda:self.model)
        with TestClient(app) as another:
            another.headers['Authorization']='Bearer read-token-123'
            saved=another.get(path).json()
            self.assertEqual(saved['title'],'维护检查')
            self.assertEqual(saved['turns'][0]['result']['citations'],value['turns'][0]['result']['citations'])
            self.assertEqual(another.get('/api/v1/chat/sessions').json()[0]['id'],session['id'])
        self.assertTrue(self.client.delete(path).json()['deleted'])
        self.assertEqual(self.client.get(path).status_code,404)

    def test_tokens_isolate_every_session_operation(self):
        session = self.create()
        path = '/api/v1/chat/sessions/'+session['id']
        self.client.headers['Authorization']='Bearer admin-token-123'
        self.assertEqual(self.client.get('/api/v1/chat/sessions').json(),[])
        for method, body in [('GET',None),('PATCH',{'title':'不能修改'}),('DELETE',None)]:
            self.assertEqual(self.client.request(method,path,json=body).status_code,404)
        self.assertEqual(self.message(session).status_code,404)
        self.client.headers.clear()
        self.assertEqual(self.client.get('/api/v1/chat/sessions').status_code,401)

    def test_fixed_release_followup_and_idempotency(self):
        session=self.create()
        first=self.message(session).json()
        self.store.publish(self.kb,fixtures.Embedding())
        response=self.message(session,request='second',query='这种情况如何处理？').json()
        self.assertEqual(response['release_id'],session['release_id'])
        self.assertEqual(len(response['turns']),2)
        calls=len(self.model.calls)
        repeated=self.message(session,request='second',query='这种情况如何处理？').json()
        self.assertEqual(len(repeated['turns']),2)
        self.assertEqual(len(self.model.calls),calls)
        self.assertEqual(self.message(session,request='second',query='不同的问题').status_code,409)
        self.assertEqual(self.message(first,request='third',retry_turn_id=first['turns'][0]['id']).status_code,409)

    def test_failure_retry_and_restart_recovery(self):
        session=self.create()
        self.model.fail=True
        value=self.message(session).json()
        self.assertEqual(value['turns'][0]['status'],'failed')
        self.model.fail=False
        retried=self.message(session,request='retry',retry_turn_id=value['turns'][0]['id']).json()
        self.assertEqual(len(retried['turns']),1)
        self.assertEqual(retried['turns'][0]['status'],'succeeded')
        chats=ChatStore(Path(self.temp.name));owner=chats.owner('read-token-123')
        chats.begin(owner,session['id'],'后续问题','interrupted')
        recovered=ChatStore(Path(self.temp.name)).get(owner,session['id'])
        self.assertEqual(recovered['turns'][-1]['status'],'failed')
        self.assertIn('重启',recovered['turns'][-1]['error'])

    def test_concurrent_turn_rejected_and_deleted_session_never_resurrects(self):
        session=self.create()
        started, release=threading.Event(),threading.Event()
        original=self.model.complete
        def block(messages):
            started.set();release.wait(5);return original(messages)
        self.model.complete=block
        responses=[]
        thread=threading.Thread(target=lambda:responses.append(self.message(session)))
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            self.assertEqual(self.message(session,request='other').status_code,409)
            path='/api/v1/chat/sessions/'+session['id']
            self.assertEqual(self.client.delete(path).status_code,200)
        finally:
            release.set();thread.join(5)
        self.assertEqual(responses[0].status_code,404)
        self.assertEqual(self.client.get(path).status_code,404)

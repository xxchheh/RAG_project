"""Agent v1 boundary contract. Baseline changes require compatibility review."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.api_contract import Rule


class Embedding:
    def profile(self):
        return {'mode': 'test', 'model': 'fixed', 'fingerprint': 'test'}

    def matches(self, profile):
        return profile == self.profile()

    def embed(self, texts):
        return [[1., 1., .5] for _ in texts]


class EvidenceModel:
    def complete(self, messages):
        evidence = json.loads(messages[-1]['content'])['evidence'][0]
        return {'content': json.dumps({'citations': [{'knowledge_id': evidence['knowledge_id'], 'quote': evidence['text']}]})}


PUBLIC = {
    '/api/v1/health': 'get', '/api/v1/capabilities': 'get',
    '/api/v1/knowledge-bases': 'get', '/api/v1/retrieve': 'post',
    '/api/v1/answers': 'post', '/api/v1/rules': 'get',
    '/api/v1/knowledge/{knowledge_id}': 'get',
}


def contract_snapshot(app, client):
    schema = app.openapi()
    paths = {path: {method: schema['paths'][path][method]} for path, method in PUBLIC.items()}
    models = {}

    def collect(value):
        if isinstance(value, dict):
            ref = value.get('$ref', '')
            if ref.startswith('#/components/schemas/'):
                name = ref.rsplit('/', 1)[-1]
                if name not in models:
                    models[name] = schema['components']['schemas'][name]
                    collect(models[name])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(paths)
    return {'paths': paths, 'schemas': models, 'capabilities': client.get('/api/v1/capabilities').json()}


class AgentContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.embedding = Embedding()
        self.app = create_app(Settings(Path(self.tmp.name), 'admin-token-123', 'read-token-123'), embedder=self.embedding, model=EvidenceModel())
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers['Authorization'] = 'Bearer read-token-123'
        self.store = self.app.state.store
        self.kb = self.store.create_kb('接口契约测试')['id']
        text = '加速度非超额峭度大于等于3.5且小于4.5。'
        body = {'blocks': [{'id': 'b1', 'text': '润滑不足会导致轴承磨损。', 'locator': '第1行', 'kind': 'description'},
                           {'id': 'b2', 'text': text, 'locator': '第2行', 'kind': 'rule'}],
                'rules': [{'id': 'R'+str(i), 'name': '电机轴承规则'+str(i), 'device_type': 'motor', 'original_text': text,
                           'condition': {'all': [{'metric': 'kurtosis', 'ge': 3.5, 'lt': 4.5}]}, 'block_ids': ['b2']} for i in range(3)], 'warnings': []}
        upload = self.store.reserve_upload(self.kb, 'contract.md', b'fixture')
        self.doc = self.store.finish_upload(upload['document_id'], 1, body)
        for target, rule_id in [('description', None)] + [('rule', 'R'+str(i)) for i in range(3)]:
            self.doc = self.store.review(self.doc['id'], self.doc['edit_version'], target, 'approved', rule_id)
        self.release = self.store.publish(self.kb, self.embedding)

    def test_openapi_and_capabilities_match_reviewed_baseline(self):
        baseline = Path(__file__).parent / 'fixtures/agent_v1_contract.json'
        self.assertEqual(contract_snapshot(self.app, self.client), json.loads(baseline.read_text()))

    def test_public_shapes_complete_rules_and_citations(self):
        bases = self.client.get('/api/v1/knowledge-bases').json()
        self.assertIsInstance(bases, list)
        self.assertEqual(bases[0]['current_release'], self.release['id'])
        params = {'kb_id': self.kb, 'release_id': self.release['id'], 'device_type': 'motor'}
        rules = self.client.get('/api/v1/rules', params=params).json()
        self.assertEqual(rules['count'], 3)
        self.assertEqual(len(rules['rules']), 3)
        self.assertEqual(rules['schema_version'], 'equipment-rules-v1')
        for rule in rules['rules']:
            self.assertEqual(rule['units']['kurtosis'], '1')
            self.assertEqual(rule['condition']['all'][0]['ge'], 3.5)
            detail = self.client.get('/api/v1/knowledge/'+rule['knowledge_id'], params=params).json()
            self.assertEqual(detail['item']['rule']['id'], rule['id'])
            self.assertEqual(detail['item']['source'], rule['source'])
        body = {'kb_id': self.kb, 'query': '润滑', 'top_k': 1, 'release_id': self.release['id']}
        result = self.client.post('/api/v1/retrieve', json=body).json()
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['release_id'], self.release['id'])
        answer = self.client.post('/api/v1/answers', json=body).json()
        self.assertEqual(answer['status'], 'answered')
        citation = answer['citations'][0]
        self.assertIn(citation['quote'], answer['items'][0]['text'])
        self.assertNotIn(self.tmp.name, json.dumps(answer))

    def test_explicit_version_does_not_follow_new_publication(self):
        self.store.disable_document(self.doc['id'], True)
        newer = self.store.publish(self.kb, self.embedding)
        self.assertEqual(self.client.get('/api/v1/rules', params={'kb_id': self.kb}).json()['rules'], [])
        old = self.client.get('/api/v1/rules', params={'kb_id': self.kb, 'release_id': self.release['id']}).json()
        self.assertEqual(old['count'], 3)
        self.assertEqual(old['release_id'], self.release['id'])
        self.assertNotEqual(newer['id'], old['release_id'])
        body = {'kb_id': self.kb, 'query': '润滑'}
        result = self.client.post('/api/v1/answers', json=body)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'insufficient_evidence')
        self.assertEqual(result.json()['citations'], [])

    def test_errors_auth_and_strict_requests(self):
        cases = [self.client.get('/api/v1/rules', params={'kb_id': self.kb, 'release_id': 'missing'}),
                 self.client.post('/api/v1/retrieve', json={'kb_id': self.kb, 'query': 'x', 'unexpected': 1}),
                 self.client.post('/api/v1/retrieve', json={'kb_id': self.kb, 'query': ' '}),
                 self.client.post('/api/v1/admin/publish', json={'kb_id': self.kb})]
        for response, status, code in zip(cases, [404, 422, 400, 403], ['NOT_FOUND', 'VALIDATION_ERROR', 'BAD_REQUEST', 'FORBIDDEN']):
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(response.json()['code'], code)
            self.assertIn('detail', response.json())
        self.client.headers.clear()
        self.assertEqual(self.client.get('/api/v1/health').status_code, 200)
        for path in ['/knowledge-bases', '/capabilities', '/rules?kb_id='+self.kb, '/knowledge/x?kb_id='+self.kb]:
            self.assertEqual(self.client.get('/api/v1'+path).json()['code'], 'UNAUTHORIZED')
        for path in ['/retrieve', '/answers']:
            self.assertEqual(self.client.post('/api/v1'+path, json={'kb_id': self.kb, 'query': 'x'}).json()['code'], 'UNAUTHORIZED')

    def test_internal_fields_are_not_accidentally_published(self):
        records = self.store.list_kbs()
        records[0]['internal_storage_path'] = '/private/internal'
        with patch.object(self.store, 'list_kbs', return_value=records):
            self.assertNotIn('internal_storage_path', self.client.get('/api/v1/knowledge-bases').json()[0])
        rule = self.client.get('/api/v1/rules', params={'kb_id': self.kb}).json()['rules'][0]
        rule['internal_debug'] = 'private'
        self.assertNotIn('internal_debug', Rule.model_validate(rule).model_dump())
        rule['units']['kurtosis'] = 'mm/s'
        with self.assertRaises(ValueError):
            Rule.model_validate(rule)

    def test_bad_internal_output_fails_instead_of_success(self):
        with TestClient(self.app, raise_server_exceptions=False) as client:
            client.headers.update(self.client.headers)
            with patch.object(self.store, 'list_kbs', return_value=[{'id': 'broken'}]):
                result = client.get('/api/v1/knowledge-bases')
            self.assertEqual(result.status_code, 500)
            self.assertEqual(result.json(), {'code': 'INTERNAL_ERROR', 'detail': '服务内部错误，请联系服务提供方'})

    def test_rule_tree_contract_rejects_unknown_operator_and_invalid_metric(self):
        rule = self.client.get('/api/v1/rules', params={'kb_id': self.kb}).json()['rules'][0]
        for condition in [{'xor': [{'metric': 'kurtosis', 'ge': 3.5}]}, {'all': []},
                          {'metric': 'unknown', 'ge': 1}, {'metric': 'kurtosis', 'ge': True}]:
            candidate = copy.deepcopy(rule)
            candidate['condition'] = condition
            with self.assertRaises(ValueError):
                Rule.model_validate(candidate)


if __name__ == '__main__':
    unittest.main()

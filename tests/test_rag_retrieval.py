import io
import json
import os
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import numpy as np

from rag_service.embeddings import EmbeddingConfig, EmbeddingProvider, split_text, validate_vectors
from rag_service.retrieval import answer_question, build_index, retrieve


class FakeEmbedder:
    def __init__(self, profile='test'):
        self.marker = profile
        self.calls = []

    def profile(self):
        return {'fingerprint': self.marker, 'mode': 'test', 'model': 'fixed'}

    def matches(self, profile):
        return profile.get('fingerprint') == self.marker

    def embed(self, texts):
        self.calls.append(texts)
        return [[1., 0.] if any(word in text for word in ('轴承', '润滑')) else [0., 1.] for text in texts]


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return json.dumps(self.data).encode()


class Model:
    def __init__(self, result):
        self.result = result

    def complete(self, messages):
        return {'content': json.dumps(self.result, ensure_ascii=False)}


class EmbeddingTests(unittest.TestCase):
    def test_profile_excludes_secret_and_changes_for_model_or_endpoint(self):
        one = EmbeddingProvider(EmbeddingConfig(mode='api', model='m', base_url='https://example.test/v1', api_key='secret-one'))
        two = EmbeddingProvider(EmbeddingConfig(mode='api', model='m', base_url='https://example.test/v1', api_key='secret-two'))
        self.assertEqual(one.profile(), two.profile())
        self.assertNotIn('secret', json.dumps(one.profile()))
        changed = EmbeddingProvider(EmbeddingConfig(mode='api', model='n', base_url='https://example.test/v1', api_key='key'))
        self.assertFalse(changed.matches(one.profile()))
        changed = EmbeddingProvider(EmbeddingConfig(mode='api', model='m', base_url='https://other.test/v1', api_key='key'))
        self.assertFalse(changed.matches(one.profile()))

    def test_configuration_validation_and_env(self):
        for kwargs in ({'mode': 'other'}, {'model': ''}, {'timeout': float('nan')}, {'mode': 'api', 'model': 'm', 'base_url': 'https://u:p@example.com', 'api_key': 'key'},
                       {'mode': 'api', 'model': 'm', 'base_url': 'http://remote.test', 'api_key': 'key'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                EmbeddingConfig(**kwargs)
        with patch.dict(os.environ, {'EMBEDDING_MODE': 'api', 'EMBEDDING_MODEL': 'text-model', 'EMBEDDING_BASE_URL': 'http://127.0.0.1:8001/v1/', 'EMBEDDING_API_KEY': 'key'}, clear=True):
            self.assertEqual(EmbeddingConfig.from_env().base_url, 'http://127.0.0.1:8001/v1')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(EmbeddingConfig.from_env().mode, 'local')

    def test_api_wire_format_and_index_order(self):
        config = EmbeddingConfig(mode='api', model='text-model', base_url='https://example.test/v1', api_key='private-key')
        captured = []
        def request(req, timeout):
            captured.append(req)
            return Response({'data': [{'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]}]})
        with patch('rag_service.embeddings.urlopen', side_effect=request):
            vectors = EmbeddingProvider(config).embed(['one', 'two'])
        self.assertEqual(vectors, [[1, 0], [0, 1]])
        self.assertEqual(captured[0].full_url, 'https://example.test/v1/embeddings')
        self.assertEqual(captured[0].get_header('Authorization'), 'Bearer private-key')
        self.assertEqual(json.loads(captured[0].data)['input'], ['one', 'two'])

    def test_api_failures_are_sanitized_and_never_fall_back(self):
        provider = EmbeddingProvider(EmbeddingConfig(mode='api', model='m', base_url='https://example.test', api_key='top-secret'))
        for error in (HTTPError('https://example.test', 401, 'top-secret', {}, io.BytesIO(b'top-secret')), URLError('top-secret'), TimeoutError('top-secret')):
            with self.subTest(error=type(error).__name__), patch('rag_service.embeddings.urlopen', side_effect=error), self.assertRaises(ValueError) as caught:
                provider.embed(['text'])
            self.assertNotIn('top-secret', str(caught.exception))
        for data in ([{'index': 0, 'embedding': [0, 0]}], [{'index': 0, 'embedding': [True, 1]}], [{'index': 0, 'embedding': [1, float('nan')]}], [{'index': 1, 'embedding': [1, 0]}]):
            with patch('rag_service.embeddings.urlopen', return_value=Response({'data': data})), self.assertRaises(ValueError):
                provider.embed(['text'])

    def test_local_model_is_lazy_and_cpu_only(self):
        class Local:
            loaded = []
            def __init__(self, **kwargs):
                self.loaded.append(kwargs)
            def embed(self, texts, **kwargs):
                return [np.asarray([1., 2.]) for _ in texts]
        with patch.dict('sys.modules', {'fastembed': types.SimpleNamespace(TextEmbedding=Local)}):
            provider = EmbeddingProvider()
            self.assertEqual(Local.loaded, [])
            self.assertEqual(provider.embed(['one']), [[1., 2.]])
            provider.embed(['two'])
            self.assertEqual(len(Local.loaded), 1)
            self.assertEqual(Local.loaded[0]['providers'], ['CPUExecutionProvider'])

    def test_vector_validation_and_chunk_overlap(self):
        for values in ([[0, 0]], [[float('inf'), 1]], [[1], [1, 2]], [[True, 1]], [[]]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_vectors(values)
        text = '轴承润滑检查' * 100
        chunks = split_text(text, 100, 10)
        self.assertEqual(chunks[0][-10:], chunks[1][:10])
        self.assertTrue(all(len(chunk) <= 100 for chunk in chunks))
        self.assertEqual(split_text('  '), [])

    def test_local_token_overflow_covers_all_input_without_truncation(self):
        class Encoding:
            def __init__(self, offsets, overflowing=()):
                self.offsets, self.overflowing = offsets, overflowing
        overflow = Encoding([(3, 4), (4, 5), (5, 6)])
        encoded = Encoding([(0, 1), (1, 2), (2, 3)], [overflow])
        captured = []
        class Local:
            model = types.SimpleNamespace(tokenize=lambda texts: [encoded])
            def __init__(self, **kwargs):
                pass
            def embed(self, texts, **kwargs):
                captured.extend(texts)
                return [np.asarray([1., 0.]), np.asarray([0., 1.])]
        with patch.dict('sys.modules', {'fastembed': types.SimpleNamespace(TextEmbedding=Local)}):
            self.assertEqual(EmbeddingProvider().embed(['轴承润滑检查']), [[.5, .5]])
        self.assertEqual(''.join(captured), '轴承润滑检查')


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            {'id': 'k1', 'kind': 'description', 'text': '轴承润滑不足会造成磨损。应检查润滑油。', 'device_type': 'motor', 'source': {'document_id': 'doc1', 'document_revision': 1, 'locator': '段落1'}},
            {'id': 'k2', 'kind': 'description', 'text': '水泵进口堵塞需要检查过滤器。', 'device_type': 'pump', 'source': {'document_id': 'doc2', 'document_revision': 1, 'locator': '第1页'}},
        ]
        self.embedder = FakeEmbedder()
        self.index = build_index(self.entries, self.embedder)

    def test_hybrid_semantic_keyword_and_device_filter(self):
        hits = retrieve('轴承润滑', self.entries, self.index, self.embedder)
        self.assertEqual(hits[0]['id'], 'k1')
        self.assertEqual(hits[0]['source'], self.entries[0]['source'])
        self.assertEqual(hits[0]['semantic_score'], 1)
        self.assertGreater(hits[0]['keyword_score'], 0)
        pump_hits = retrieve('检查过滤器', self.entries, self.index, self.embedder, device_type='pump')
        self.assertEqual([item['id'] for item in pump_hits], ['k2'])
        hits[0]['source']['locator'] = 'mutated'
        self.assertEqual(self.entries[0]['source']['locator'], '段落1')

    def test_historical_profile_dimension_and_entry_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, '不能混用'):
            retrieve('轴承', self.entries, self.index, FakeEmbedder('changed'))
        broken = {**self.index, 'dimension': 3}
        with self.assertRaises(ValueError):
            retrieve('轴承', self.entries, broken, self.embedder)
        with self.assertRaises(ValueError):
            retrieve('轴承', self.entries[:1], self.index, self.embedder)
        for top_k in (0, True, 51):
            with self.assertRaises(ValueError):
                retrieve('轴承', self.entries, self.index, self.embedder, top_k=top_k)

    def test_empty_index_requires_no_embedding_calls(self):
        provider = FakeEmbedder()
        index = build_index([], provider)
        self.assertEqual(index['dimension'], 0)
        self.assertEqual(retrieve('轴承', [], index, provider), [])
        self.assertEqual(provider.calls, [])

    def test_source_bound_answers_and_insufficient_evidence(self):
        result = answer_question('原因是什么', self.entries, Model({'citations': [{'knowledge_id': 'k1', 'quote': '轴承润滑不足会造成磨损。'}]}))
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['citations'][0]['source']['document_id'], 'doc1')
        self.assertIn('[1]', result['answer'])
        self.assertEqual(answer_question('问题', [], None)['status'], 'insufficient_evidence')
        self.assertEqual(answer_question('问题', self.entries, Model({'citations': []}))['status'], 'insufficient_evidence')
        for item in ({'knowledge_id': 'made-up', 'quote': '轴承'}, {'knowledge_id': 'k1', 'quote': '健康评分100分'}):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, '来源校验'):
                answer_question('问题', self.entries, Model({'citations': [item]}))


if __name__ == '__main__':
    unittest.main()

"""Rule manuals are fault units; reference documents are safe section chunks."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from docx import Document
from docx.oxml.ns import qn
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.documents import parse_document
from rag_service.embeddings import EmbeddingProvider
from rag_service.retrieval import retrieve, answer_question
from rag_service.store import Store

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT/'diagnostic_agent/assets/rules_reference.docx'


class FixedEmbedding:
    def profile(self): return {'mode': 'test', 'model': 'constant', 'fingerprint': 'constant'}
    def matches(self, profile): return profile == self.profile()
    def embed(self, texts): return [[1., 1.] for text in texts]


class RuleDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.kb = self.store.create_kb('规则与参考资料')['id']
        self.emb = FixedEmbedding()

    def upload(self, content=None, filename='rules.docx', mode='rules'):
        content = content or REFERENCE.read_bytes()
        parsed = parse_document(content, filename, document_type=mode)
        result = self.store.reserve_upload(self.kb, filename, content, document_type=mode)
        return self.store.finish_upload(result['document_id'], result['revision'], parsed)

    def approve(self, doc, descriptions=True, rules=True):
        if descriptions:
            doc = self.store.review(doc['id'], doc['edit_version'], 'description', 'approved')
        if rules:
            for rule in doc['payload']['rules']:
                doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved', rule['id'])
        return doc

    def test_sixteen_complete_units_and_all_forty_eight_questions(self):
        doc = self.approve(self.upload())
        expected = json.loads((ROOT/'diagnostic_agent/config/rules.json').read_text())['rules']
        self.assertEqual(len(doc['payload']['rules']), 16)
        self.assertTrue(all(r['valid'] for r in doc['payload']['rules']))
        release = self.store.publish(self.kb, self.emb)
        release = self.store.release(self.kb, release['id'], include_index=True)
        self.assertEqual(len(release['entries']), 16)  # no cover, TOC or duplicate prose
        for name in [r['name'] for r in expected]:
            for suffix, key in [('有哪些原因', 'causes'), ('有哪些处理措施', 'actions'), ('判断条件是什么', 'original_text')]:
                question = name+suffix
                hits = retrieve(question, release['entries'], release['index'], self.emb, 5)
                self.assertEqual(hits[0]['rule']['name'], name)
                answer = answer_question(question, hits, None)
                self.assertEqual(answer['status'], 'answered', question)
                self.assertTrue(answer['citations'])
                self.assertIn(answer['citations'][0]['quote'], hits[0]['text'])
                self.assertIn(answer['citations'][0]['quote'], hits[0]['source']['original_quote'])
                self.assertTrue(hits[0]['rule'][key])
        # Parse values from the Word, not from the seed JSON.
        amended = Document(REFERENCE)
        for paragraph in amended.paragraphs:
            if '机理规则：' in paragraph.text and '0.6' in paragraph.text:
                paragraph.text = paragraph.text.replace('0.6', '0.65', 1)
                break
        buf = io.BytesIO(); amended.save(buf)
        changed = parse_document(buf.getvalue(), 'changed.docx', document_type='rules')
        rule = next(r for r in changed['rules'] if r['name'] == '电机转子不平衡')
        self.assertEqual(rule['condition']['all'][0]['ge'], .65)

    def test_numeric_style_ids_and_toc_exclusion(self):
        document = Document()
        toc_style = document.styles.add_style('toc 1', 1)
        document.add_paragraph('电机转子不平衡3', toc_style)
        document.add_heading('电机转子不平衡', 2)
        document.add_paragraph('机理原因：转子腐蚀。')
        document.add_paragraph('机理规则：加速度非超额峭度大于等于3.5。')
        style = document.styles['Heading 2']
        old = style.style_id
        style.element.set(qn('w:styleId'), '777')
        for paragraph in document.paragraphs:
            if paragraph._p.pPr is not None and paragraph._p.pPr.pStyle is not None and paragraph._p.pPr.pStyle.val == old:
                paragraph._p.pPr.pStyle.val = '777'
        output = io.BytesIO(); document.save(output)
        doc = self.upload(output.getvalue())
        self.assertTrue(doc['payload']['blocks'][0]['excluded'])
        self.assertTrue(doc['payload']['blocks'][1]['heading'])
        self.assertEqual(doc['payload']['rules'][0]['causes'], ['转子腐蚀。'])
        self.approve(doc)
        text = json.dumps(self.store.publish(self.kb, self.emb), ensure_ascii=False)
        self.assertNotIn('电机转子不平衡3', text)

    def test_description_only_then_rule_only_cannot_bypass_reviews(self):
        doc = self.approve(self.upload(), rules=False)
        prose = self.store.publish(self.kb, self.emb)
        text = '\n'.join(e['text'] for e in prose['entries'])
        self.assertIn('制造误差', text)
        self.assertNotIn('机理规则：', text)
        self.assertTrue(all(e['kind'] == 'description' for e in prose['entries']))
        self.approve(doc, descriptions=False)
        self.assertEqual(len(self.store.publish(self.kb, self.emb)['entries']), 16)
        # Independent document: rule approval must not expose unreviewed prose.
        other = self.store.create_kb('单独审核')['id']; self.kb = other
        doc = self.approve(self.upload(), descriptions=False)
        release = self.store.publish(other, self.emb)
        self.assertTrue(all(not e['rule']['causes'] for e in release['entries']))
        self.assertNotIn('制造误差', json.dumps(release, ensure_ascii=False))

    def test_reference_sections_keep_context_and_quarantine_thresholds(self):
        doc = self.upload('# 润滑管理\n\n检查润滑油。\n\n检查轴承温度。\n\n机理规则：速度有效值超过4.5。\n\n# 安装管理\n\n检查底座。'.encode(), 'manual.md', 'reference')
        self.approve(doc, rules=False)
        rel = self.store.publish(self.kb, self.emb)
        self.assertEqual(len(rel['entries']), 2)
        self.assertIn('润滑管理\n检查润滑油。\n检查轴承温度。', rel['entries'][0]['text'])
        self.assertNotIn('4.5', json.dumps(rel, ensure_ascii=False))

    def test_missing_reviewed_field_returns_insufficient_without_model(self):
        doc = self.approve(self.upload(), descriptions=False)
        rel = self.store.publish(self.kb, self.emb)
        rel = self.store.release(self.kb, rel['id'], include_index=True)
        hits = retrieve('电机转子不平衡有哪些原因', rel['entries'], rel['index'], self.emb)
        self.assertEqual(answer_question('电机转子不平衡有哪些原因', hits, None)['status'], 'insufficient_evidence')

    def test_exclusion_metadata_cannot_be_changed_to_publish_toc(self):
        doc = self.upload()
        blocks = copy.deepcopy(doc['payload']['blocks'])
        excluded = [b for b in blocks if b.get('excluded')]
        # The bundled fixture may have a TOC or cover; inject no new source blocks.
        self.assertTrue(excluded)
        excluded[0]['excluded'] = False
        edited = self.store.edit_document(doc['id'], doc['edit_version'], blocks=blocks)
        block = next(b for b in edited['payload']['blocks'] if b['id'] == excluded[0]['id'])
        self.assertTrue(block['excluded'])

    def test_unknown_or_conditions_stay_invalid(self):
        doc = self.upload('# 电机特殊故障\n\n机理规则：温度大于等于70，速度有效值大于等于3。'.encode(), 'unknown.md')
        self.assertFalse(doc['payload']['rules'][0]['valid'])
        self.approve(doc, rules=False)
        self.assertNotIn('70', json.dumps(self.store.publish(self.kb, self.emb)))

    def test_reprocess_creates_revision_and_keeps_release_and_public_contract(self):
        app = create_app(Settings(Path(self.tmp.name)/'http', 'admin-token-123', 'read-token-123'), embedder=self.emb, model_factory=lambda: None)
        with TestClient(app) as client:
            client.headers['Authorization'] = 'Bearer admin-token-123'
            kb = client.post('/api/v1/admin/knowledge-bases', json={'name': 'HTTP'}).json()['id']
            result = client.post('/api/v1/admin/documents', data={'kb_id': kb, 'document_type': 'reference'}, files={'file': ('rules.docx', REFERENCE.read_bytes())}).json()
            identifier = result['document_id']; store = app.state.store
            doc = store.document(identifier)
            doc = store.review(identifier, doc['edit_version'], 'description', 'approved')
            old = store.publish(kb, self.emb)
            result = client.post('/api/v1/admin/documents/'+identifier+'/reprocess', json={'document_type': 'rules'})
            self.assertEqual(result.status_code, 202, result.text)
            task = client.get('/api/v1/admin/tasks/'+result.json()['task_id']).json()
            self.assertEqual(task['status'], 'succeeded', task)
            doc = store.document(identifier)
            self.assertEqual(doc['latest_revision'], 2)
            self.assertEqual(doc['description_status'], 'pending')
            self.assertEqual(len(doc['payload']['rules']), 16)
            self.assertTrue(all(r['review_status'] == 'pending' for r in doc['payload']['rules']))
            self.assertEqual(client.get('/api/v1/knowledge-bases').json()[0]['current_release'], old['id'])
            self.assertEqual(store.release(kb, old['id']), old)
            client.headers['Authorization'] = 'Bearer read-token-123'
            self.assertEqual(client.post('/api/v1/admin/documents/'+identifier+'/reprocess', json={'document_type': 'rules'}).status_code, 403)


if __name__ == '__main__': unittest.main()

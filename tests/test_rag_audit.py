"""Integration regressions discovered during the publication-isolation audit."""
import tempfile
import unittest

from rag_service.store import Store


class Embedder:
    def profile(self):
        return {'mode': 'test', 'model': 'fixed', 'fingerprint': 'audit-test'}

    def embed(self, texts):
        return [[1., 0.] for _ in texts]


class RagAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.kb = self.store.create_kb('发布审查')['id']
        self.embedder = Embedder()

    def description_document(self):
        reserved = self.store.reserve_upload(self.kb, 'description.md', b'first', external_id='doc-source')
        doc = self.store.finish_upload(reserved['document_id'], 1, {
            'blocks': [{'id': 'b1', 'text': '检查轴承润滑油。', 'kind': 'description', 'locator': '行1'}],
            'rules': [], 'warnings': [],
        })
        return self.store.review(doc['id'], doc['edit_version'], 'description', 'approved')

    def test_restored_manifest_stays_active_until_new_changes(self):
        doc = self.description_document()
        old = self.store.publish(self.kb, self.embedder)
        self.store.disable_document(doc['id'], True)
        self.assertEqual(self.store.publish(self.kb, self.embedder)['entry_count'], 0)
        restored = self.store.publish(self.kb, self.embedder, restore_id=old['id'])
        self.assertEqual(restored['entries'], old['entries'])
        repeated = self.store.publish(self.kb, self.embedder)
        self.assertEqual(repeated['entries'], old['entries'])

    def test_retired_rule_does_not_quarantine_new_revision_block(self):
        reserved = self.store.reserve_upload(self.kb, 'rule.md', b'rule-version')
        original = '加速度非超额峭度大于等于3.5。'
        doc = self.store.finish_upload(reserved['document_id'], 1, {
            'blocks': [{'id': 'b1', 'text': original, 'kind': 'rule', 'locator': '行1'}],
            'rules': [{'id': 'R001', 'name': '电机轴承规则', 'device_type': 'motor', 'original_text': original,
                       'condition': {'metric': 'kurtosis', 'ge': 3.5}, 'block_ids': ['b1']}],
            'warnings': [],
        })
        doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved', rule_id='R001')
        self.store.publish(self.kb, self.embedder)
        reserved = self.store.reserve_upload(self.kb, 'rule.md', b'description-version', document_id=doc['id'])
        doc = self.store.finish_upload(doc['id'], reserved['revision'], {
            'blocks': [{'id': 'b1', 'text': '新版只介绍润滑油检查方法。', 'kind': 'description', 'locator': '行1'}],
            'rules': [], 'warnings': [],
        })
        doc = self.store.review(doc['id'], doc['edit_version'], 'description', 'approved')
        doc = self.store.review(doc['id'], doc['edit_version'], 'rule', 'approved', rule_id='R001')
        result = self.store.publish(self.kb, self.embedder)
        self.assertEqual(len(result['entries']), 1)
        self.assertEqual(result['entries'][0]['kind'], 'description')
        self.assertEqual(result['entries'][0]['source']['document_revision'], 2)

    def test_restart_recovers_document_and_allows_external_retry(self):
        reserved = self.store.reserve_upload(self.kb, 'pending.md', b'pending', external_id='external-pending')
        task_id = self.store.new_task()
        recovered = Store(self.temp.name, recover_tasks=True)
        self.assertEqual(recovered.task(task_id)['status'], 'failed')
        self.assertEqual(recovered.document(reserved['document_id'])['status'], 'failed')
        retried = recovered.reserve_upload(self.kb, 'pending.md', b'retried', external_id='external-pending')
        self.assertEqual(retried['document_id'], reserved['document_id'])
        self.assertEqual(retried['revision'], 2)


if __name__ == '__main__':
    unittest.main()

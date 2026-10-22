import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from diagnostic_agent.knowledge import KnowledgeStore
from diagnostic_agent.rules import analyze
from diagnostic_agent.tools import ToolRuntime
from diagnostic_agent.rule_validation import normalize_rule


class KnowledgeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root/'kb.db')
        self.runtime = ToolRuntime(self.root/'out', knowledge_store=self.store)

    def tearDown(self):
        self.temp.cleanup()

    def update(self):
        rule = self.store.get_rule('R001')
        rule['condition']['all'][0]['ge'] = 4.2
        rule['original_text'] = '机理规则：峭度大于等于4.2且小于4.5，低频冲击8至16，峰值因子至少4。'
        rule['actions'] = ['新版本维护措施']
        return self.store.create_draft(rule)

    def analyze(self):
        did = self.runtime.get_device_data(device_id='motor-16')['dataset_id']
        return self.runtime.analyze_device(did)

    def test_draft_publish_disable_and_old_report(self):
        old = self.analyze()
        draft = self.update()
        self.assertEqual(self.analyze()['rule_snapshot_id'], old['rule_snapshot_id'])
        self.store.publish('R001', draft['revision'])
        new = self.analyze()
        self.assertNotIn('R001', new['matched_rule_ids'])
        self.assertNotEqual(old['rule_snapshot_id'], new['rule_snapshot_id'])
        result = self.runtime.generate_report(old['analysis_id'])
        doc = Document(result['report_path'])
        text = '\n'.join(p.text for p in doc.paragraphs)
        self.assertIn('R001 V1', text)
        self.assertNotIn('新版本维护措施', text)
        self.store.disable('R001')
        current = self.analyze()
        self.assertNotIn('R001', [r['id'] for r in current['rules']])
        self.assertNotIn('R001', [h['rule_id'] for h in current['window_rule_hits']])

    def test_window_uses_one_snapshot_even_if_published_mid_analysis(self):
        draft = self.update()
        calls = []
        def hook(features, device_type, rule_snapshot=None):
            if not calls:
                self.store.publish('R001', draft['revision'])
            calls.append(rule_snapshot['snapshot_id'])
            return analyze(features, device_type, rule_snapshot=rule_snapshot)
        with patch('diagnostic_agent.tools.analyze', side_effect=hook):
            result = self.analyze()
        self.assertEqual(len(calls), 31)
        self.assertEqual(len(set(calls)), 1)
        self.assertIn('R001', result['matched_rule_ids'])
        self.assertEqual(next(h['sample_count'] for h in result['window_rule_hits'] if h['rule_id'] == 'R001'), 30)
        self.assertNotIn('R001', self.analyze()['matched_rule_ids'])

    def test_explanation_updates_and_knowledge_tool_are_versioned(self):
        old = self.analyze()
        rule = self.store.get_rule('R001')
        rule['actions'].append('新增的润滑检查说明')
        self.store.create_draft(rule)
        self.assertEqual(self.runtime.search_knowledge('新增的润滑')['items'], [])
        self.store.publish('R001')
        self.assertEqual(self.runtime.search_knowledge('新增的润滑')['items'][0]['revision'], 2)
        new = self.analyze()
        self.assertEqual(old['matched_rule_ids'], new['matched_rule_ids'])
        self.assertNotEqual(old['rule_snapshot_id'], new['rule_snapshot_id'])

    def test_report_includes_confirmed_interpretation_from_snapshot(self):
        rule = self.store.get_rule('R001')
        rule['review_notes'] = ['本版低频冲击指标采用demo_index，待现场复核。']
        self.store.create_draft(rule)
        self.store.publish('R001', confirm_notes=True)
        analysis = self.analyze()
        # Later edits cannot replace the interpretation used for this analysis.
        rule['review_notes'] = ['后续草稿解释']
        self.store.create_draft(rule)
        result = self.runtime.generate_report(analysis['analysis_id'])
        text = '\n'.join(p.text for p in Document(result['report_path']).paragraphs)
        self.assertIn('发布时确认的解释／需现场复核事项：本版低频冲击指标采用demo_index', text)
        self.assertNotIn('后续草稿解释', text)

    def test_disabled_rules_are_not_reseeded(self):
        for rule in self.store.snapshot('motor')['rules']:
            self.store.disable(rule['id'])
        result = self.analyze()
        self.assertEqual(result['status'], 'unsupported_device')
        self.assertEqual(result['rules'], [])
        self.assertEqual(KnowledgeStore(self.store.path).snapshot('motor')['rules'], [])

    def test_validator_rejects_unexecutable_conditions_and_refreshes_label(self):
        rule = self.store.get_rule('R001')
        rule['condition']['all'][0]['ge'] = 3.8
        self.assertIn('3.8', normalize_rule(rule)['condition']['all'][0]['label'])
        bad_conditions = [{'all': []}, {'metric': 'kurtosis'}, {'metric': 'kurtosis', 'eq': 1},
                          {'metric': 'kurtosis', 'ge': 4.5, 'lt': 4.5}, {'metric': 'kurtosis', 'ge': True}]
        for condition in bad_conditions:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                normalize_rule({**rule, 'condition': condition})


if __name__ == '__main__':
    unittest.main()

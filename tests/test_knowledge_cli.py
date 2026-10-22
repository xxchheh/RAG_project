import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from diagnostic_agent.cli import main
from diagnostic_agent.knowledge import KnowledgeStore


class KnowledgeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'knowledge.db'

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, *command):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(['knowledge', '--db', str(self.db), *command])
        return code, output.getvalue(), error.getvalue()

    def test_offline_maintenance_and_backup_workflow(self):
        code, output, _ = self.run_command('init')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['rules'], 16)
        store = KnowledgeStore(self.db)
        rule = store.get_rule('R001')
        rule['condition']['all'][0]['ge'] = 3.8
        rule['review_notes'] = ['演示阈值变更，需要核对。']
        draft_path = self.root / 'draft.json'
        draft_path.write_text(json.dumps(rule, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(self.run_command('draft', str(draft_path), '--rule-id', 'R001')[0], 0)
        self.assertEqual(store.snapshot('motor')['rules'][0]['revision'], 1)
        difference = json.loads(self.run_command('diff', 'R001')[1])
        self.assertTrue(any(c['path'].endswith('.ge') and c['after'] == 3.8 for c in difference['changes']))
        self.assertEqual(self.run_command('publish', 'R001')[0], 1)
        self.assertEqual(self.run_command('publish', 'R001', '--confirm-notes')[0], 0)
        self.assertEqual(json.loads(self.run_command('show', 'R001')[1])['revision'], 2)
        self.assertEqual(json.loads(self.run_command('show', 'R001', '--revision', '1')[1])['revision'], 1)
        self.assertEqual(len(json.loads(self.run_command('history', 'R001')[1])), 2)
        rows = json.loads(self.run_command('list', '--type', 'motor')[1])
        self.assertEqual(len(rows), 4)
        self.assertTrue(all('source' not in row for row in rows))
        self.assertEqual(self.run_command('disable', 'R001')[0], 0)
        self.assertNotIn('R001', [r['id'] for r in store.snapshot('motor')['rules']])
        backup = self.root / 'backup.db'
        self.assertEqual(self.run_command('export', str(backup))[0], 0)
        self.assertEqual(KnowledgeStore(backup).get_rule('R001')['revision'], 2)
        self.assertFalse(KnowledgeStore(backup).get_rule('R001')['enabled'])
        self.assertEqual(self.run_command('export', str(backup))[0], 1)

    def test_bad_draft_and_unknown_rule_fail_without_modifying_store(self):
        self.run_command('init')
        path = self.root / 'bad.json'
        for text in ('not-json', '{"rules": []}', '{"condition": {"metric":"unknown", "gt": 1}}'):
            path.write_text(text, encoding='utf-8')
            code, _, error = self.run_command('draft', str(path))
            self.assertEqual(code, 1)
            self.assertIn('未完成', error)
        self.assertEqual(self.run_command('show', 'R999')[0], 1)
        self.assertEqual(len(KnowledgeStore(self.db).list_rules()), 16)


if __name__ == '__main__':
    unittest.main()

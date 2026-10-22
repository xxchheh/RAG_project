import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document
from diagnostic_agent.common import DemoError
from diagnostic_agent.report import HEADINGS, narrative_selections, validate_report
from diagnostic_agent.tools import ToolRuntime


class BadNarrator:
    def complete(self, *args, **kwargs):
        return {'content': '{"paragraphs":[{"rule_id":"invented","cause_indices":[999],"action_indices":[0]}]}'}


class ReportTests(unittest.TestCase):
    def setup_analysis(self, runtime, device='motor-16'):
        data = runtime.get_device_data(device_id=device)
        result = runtime.analyze_device(data['dataset_id'])
        return result['analysis_id']

    def test_five_device_types_generate_valid_reports(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            for did in ['motor-16', 'coupling-20', 'pump-21', 'fan-26', 'gearbox-31']:
                with self.subTest(did=did):
                    aid = self.setup_analysis(runtime, did)
                    result = runtime.generate_report(aid)
                    doc = Document(result['report_path'])
                    self.assertEqual([p.text for p in doc.paragraphs if p.style.name == 'Heading 1'], HEADINGS)
                    self.assertEqual(len(doc.inline_shapes), 3)
                    self.assertTrue(Path(result['analysis_path']).is_file())

    def test_missing_data_has_no_fabricated_images(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            aid = self.setup_analysis(runtime, 'motor-edge-missing')
            result = runtime.generate_report(aid)
            doc = Document(result['report_path'])
            self.assertEqual(len(doc.inline_shapes), 0)
            self.assertIn('未提供该时间窗口的波形数据', '\n'.join(p.text for p in doc.paragraphs))

    def test_fabricated_rule_and_model_failure_do_not_return_success(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path, model=BadNarrator())
            aid = self.setup_analysis(runtime)
            result = runtime.call('generate_report', {'analysis_id': aid})
            self.assertFalse(result['ok'])
            self.assertEqual(list(Path(path).rglob('report.docx')), [])
            self.assertEqual(runtime.reports, [])

    def test_write_failure_never_creates_success_report(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            aid = self.setup_analysis(runtime)
            with patch('diagnostic_agent.report.validate_report', side_effect=DemoError('validation failed')):
                self.assertFalse(runtime.call('generate_report', {'analysis_id': aid})['ok'])
            self.assertEqual(list(Path(path).rglob('report.docx')), [])


if __name__ == '__main__':
    unittest.main()

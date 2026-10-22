import json
import tempfile
import unittest
from pathlib import Path

from diagnostic_agent.common import DemoError
from diagnostic_agent.data import get_data, search_devices
from diagnostic_agent.tools import ToolRuntime, validate_arguments, compute_trends, scan_window


class ToolTests(unittest.TestCase):
    def test_search_ambiguity_and_unknown(self):
        self.assertEqual(search_devices('16号机器 行走电机')['count'], 1)
        self.assertEqual(search_devices('循环水泵')['count'], 2)
        self.assertEqual(search_devices('不存在设备')['count'], 0)
        with self.assertRaises(DemoError):
            get_data('pump-duplicate-1')

    def test_dates_default_explicit_partial_and_no_data(self):
        data = get_data('motor-16')
        self.assertEqual(len(data['history']), 30)
        self.assertEqual(data['actual_range']['end'], '2026-10-20')
        earlier = get_data('motor-16', start_date='2026-09-01', end_date='2026-09-30')
        self.assertEqual(len(earlier['history']), 30)
        self.assertIsNone(earlier['plots'])
        for kwargs in [dict(start_date='2027-01-01'), dict(start_date='2026-10-10', end_date='2026-09-01'), dict(start_date='nonsense')]:
            with self.assertRaises(DemoError):
                get_data('motor-16', **kwargs)
        partial = get_data('motor-16', start_date='2026-10-01', end_date='2026-10-31')
        self.assertEqual(partial['requested_range']['end'], '2026-10-31')
        self.assertEqual(partial['actual_range']['end'], '2026-10-20')

    def test_previous_window_calculation(self):
        result = compute_trends(get_data('motor-16'))['velocity_rms']
        self.assertGreater(result['previous_change_percent'], 0)
        self.assertEqual(result['previous_sample_count'], 30)

    def test_private_scenario_labels_not_sent_to_model(self):
        result = search_devices('16号机器')
        self.assertNotIn('target_rule_id', result['candidates'][0])
        self.assertNotIn('scenario', result['candidates'][0]['points'][0])
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            data = runtime.get_device_data(device_id='motor-16')
            self.assertNotIn('target_rule_id', data['device'])
            self.assertNotIn('scenario', data['point'])

    def test_history_does_not_hide_past_rule_hit(self):
        data = get_data('motor-16')
        hits = {h['rule_id']: h for h in scan_window(data)}
        self.assertIn('R004', hits)
        self.assertLess(hits['R004']['last_date'], data['actual_range']['end'])
        self.assertIn('R001', hits)

    def test_schema_rejects_bad_calls(self):
        for name, args in [('unknown', {}), ('get_device_data', {}), ('get_device_data', {'device_id': 'x', 'days': True}),
                           ('get_device_data', {'device_id': 'x', 'days': 0}), ('search_devices', {'query': 'x', 'sql': 'delete'}),
                           ('analyze_device', []), ('analyze_device', {'dataset_id': 3})]:
            with self.assertRaises(DemoError):
                validate_arguments(name, args)

    def test_handles_and_trace_and_reset(self):
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            self.assertFalse(runtime.call('analyze_device', {'dataset_id': 'invented'})['ok'])
            data = runtime.call('get_device_data', {'device_id': 'motor-16'})['result']
            result = runtime.call('analyze_device', {'dataset_id': data['dataset_id']})['result']
            self.assertEqual(result['matched_rule_ids'], ['R001'])
            self.assertNotIn('series', result['trends']['velocity_rms'])
            self.assertEqual(len(Path(runtime.trace_path).read_text().splitlines()), 3)
            runtime.clear()
            self.assertFalse(runtime.call('generate_report', {'analysis_id': result['analysis_id']})['ok'])


if __name__ == '__main__':
    unittest.main()

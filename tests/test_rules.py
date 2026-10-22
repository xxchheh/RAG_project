import copy
import math
import unittest
from collections import Counter

from diagnostic_agent.data import get_data, sample_catalog, sample_features
from diagnostic_agent.rules import analyze, evaluate_condition, load_rules, metric_value


class RuleTests(unittest.TestCase):
    def test_all_sixteen_rules_have_positive_samples(self):
        rules = load_rules()['rules']
        self.assertEqual(Counter(r['device_type'] for r in rules), {'motor': 4, 'coupling': 1, 'pump': 5, 'fan': 5, 'gearbox': 1})
        for device in sample_catalog()[:16]:
            with self.subTest(device=device['id']):
                result = analyze(get_data(device['id'])['features'], device['device_type'])
                self.assertIn(device['target_rule_id'], result['matched_rule_ids'])

    def test_source_and_sections_preserved(self):
        for rule in load_rules()['rules']:
            self.assertTrue(rule['original_text'].startswith('机理规则：'))
            self.assertTrue(rule['name'].startswith({'motor': '电机', 'coupling': '联轴器', 'pump': '水泵', 'fan': '风机', 'gearbox': '齿轮箱'}[rule['device_type']]))
            self.assertTrue(rule['actions'])
            self.assertTrue(rule['causes'])

    def test_motor_bearing_boundaries(self):
        for kurtosis, impact, crest, expected in [(3.5, 8, 4, True), (4.4999, 15.999, 4, True),
                                                   (4.5, 10, 5, False), (4, 16, 5, False), (4, 10, 3.99, False)]:
            features = sample_features('bearing')
            features['kurtosis']['value'] = kurtosis
            features['low_frequency_impact']['value'] = impact
            features['acceleration_peak']['value'] = crest * features['acceleration_rms']['value']
            result = analyze(features, 'motor')
            self.assertEqual('R001' in result['matched_rule_ids'], expected)

    def test_non_motor_or_parentheses_and_missing_branch(self):
        f = sample_features('bearing')
        f['kurtosis']['value'] = 2
        self.assertIn('R006', analyze(f, 'pump')['matched_rule_ids'])
        self.assertNotIn('R001', analyze(f, 'motor')['matched_rule_ids'])
        f.pop('kurtosis')
        self.assertIn('R006', analyze(f, 'pump')['matched_rule_ids'])
        f['acceleration_peak']['value'] = 2
        self.assertNotIn('R006', analyze(f, 'pump')['matched_rule_ids'])

    def test_speed_interval_does_not_mark_high_value_normal(self):
        result = analyze(sample_features('out_of_range'), 'motor')
        self.assertEqual(result['status'], 'no_rule_match')
        self.assertIn('不能据此认定设备正常', result['summary'])
        for value, expected in [(2.8, True), (4.4999, True), (4.5, False), (2.7999, False)]:
            f = sample_features('loose')
            f['velocity_rms']['value'] = value
            self.assertEqual('R004' in analyze(f, 'motor')['matched_rule_ids'], expected)

    def test_invalid_values_units_and_zero(self):
        for kind in ('missing', 'zero', 'wrong_unit'):
            self.assertTrue(analyze(sample_features(kind), 'motor')['issues'])
        for invalid in (True, float('nan'), float('inf'), -1):
            f = sample_features('bearing')
            f['acceleration_peak']['value'] = invalid
            self.assertIsNone(metric_value(f, 'crest_factor')[0])

    def test_energy_uses_root_sum_squares(self):
        f = sample_features('loose')
        for key, value in [('h3', .3), ('h6', .4), ('h9', 0), ('velocity_rms', 2)]:
            f[key]['value'] = value
        self.assertAlmostEqual(metric_value(f, 'third_ratio')[0], .25)
        self.assertNotIn('R004', analyze(f, 'motor')['matched_rule_ids'])

    def test_multiple_matches_and_unknown(self):
        self.assertEqual(analyze(sample_features('multi'), 'motor')['matched_rule_ids'], ['R002', 'R004'])
        self.assertEqual(analyze({}, 'alien')['status'], 'unsupported_device')


if __name__ == '__main__':
    unittest.main()

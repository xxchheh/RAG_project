import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diagnostic_agent.common import DemoError, ROOT
from diagnostic_agent.data import get_data, public_device, sample_catalog, sample_features, search_devices


class DeviceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "devices.json"
        self.catalog = sample_catalog(ROOT / "config" / "devices.json")
        self.write_catalog(self.catalog)
        self.env = patch.dict(os.environ, {"DIAGNOSTIC_DEVICE_CATALOG": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_catalog(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_existing_ids_and_catalog_are_independent_of_rules(self):
        self.assertEqual(len(self.catalog), 24)
        self.assertEqual(self.catalog[0]["id"], "motor-16")
        self.assertEqual(self.catalog[15]["id"], "gearbox-31")
        with patch("diagnostic_agent.rules.load_rules", return_value={"rules": []}):
            self.assertEqual(sample_catalog(), self.catalog)
        with patch("diagnostic_agent.rules.load_rules", side_effect=AssertionError("规则不应参与目录读取")):
            self.assertEqual(search_devices("16号机器 行走电机")["count"], 1)
            self.assertEqual(get_data("motor-16")["device"]["id"], "motor-16")

    def test_new_same_type_device_is_immediately_searchable_and_readable(self):
        new_device = copy.deepcopy(self.catalog[0])
        new_device.update(id="motor-1000", name="新增行走电机", location="演示工厂/新增车间", aliases=["新设备"])
        new_device.pop("target_rule_id")
        self.write_catalog([*self.catalog, new_device])
        result = search_devices("新增行走电机")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["candidates"][0]["id"], "motor-1000")
        self.assertEqual(get_data("motor-1000")["features"], get_data("motor-16")["features"])
        self.assertEqual(sample_catalog()[0]["id"], "motor-16")

    def test_removed_device_disappears_immediately_without_renumbering(self):
        self.write_catalog(self.catalog[1:])
        self.assertEqual(search_devices("16号机器 行走电机")["count"], 0)
        self.assertEqual(sample_catalog()[0]["id"], self.catalog[1]["id"])
        with self.assertRaisesRegex(DemoError, "未知设备"):
            get_data("motor-16")

    def test_explicit_path_takes_precedence_and_results_are_independent(self):
        self.write_catalog([])
        self.assertEqual(sample_catalog(), [])
        result = sample_catalog(ROOT / "config" / "devices.json")
        result[0]["points"][0]["name"] = "changed"
        self.assertEqual(sample_catalog(ROOT / "config" / "devices.json"), self.catalog)

    def test_invalid_catalog_data_reports_actionable_errors(self):
        examples = []
        examples.append(([self.catalog[0], self.catalog[0]], "重复设备 ID"))
        invalid = copy.deepcopy(self.catalog[0])
        invalid["device_type"] = "unknown"
        examples.append(([invalid], "未知设备类型"))
        invalid = copy.deepcopy(self.catalog[0])
        invalid["points"] = []
        examples.append(([invalid], "非空测点列表"))
        invalid = copy.deepcopy(self.catalog[0])
        invalid["points"].append(copy.deepcopy(invalid["points"][0]))
        examples.append(([invalid], "重复测点 ID"))
        invalid = copy.deepcopy(self.catalog[0])
        invalid["points"][0]["scenario"] = "typo"
        examples.append(([invalid], "未知模拟场景"))
        examples.append(({"devices": []}, "设备列表"))
        for value, error in examples:
            with self.subTest(error=error):
                self.write_catalog(value)
                with self.assertRaisesRegex(DemoError, error):
                    sample_catalog()

    def test_unknown_scenario_cannot_fall_back_to_looseness(self):
        with self.assertRaisesRegex(DemoError, "未知模拟场景"):
            sample_features("misspelled")

    def test_public_catalog_hides_fixture_metadata(self):
        device = public_device(self.catalog[0])
        self.assertNotIn("target_rule_id", device)
        self.assertNotIn("scenario", device["points"][0])
        self.assertIn("scenario", self.catalog[0]["points"][0])


if __name__ == "__main__":
    unittest.main()

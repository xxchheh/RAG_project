import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from diagnostic_agent.common import DemoError, ROOT, read_json
from diagnostic_agent.knowledge import KnowledgeStore


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "knowledge.db"
        self.store = KnowledgeStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def revised(self):
        rule = self.store.get_rule("R001")
        rule["condition"]["all"][0]["ge"] = 3.8
        return rule

    def test_initial_migration_is_idempotent(self):
        self.assertEqual(len(self.store.list_rules()), 16)
        self.assertEqual(len(self.store.snapshot()["rules"]), 16)
        second = KnowledgeStore(self.path)
        self.assertEqual(second.snapshot(), self.store.snapshot())
        for rule in second.list_rules():
            self.assertEqual(rule["revision"], 1)
            self.assertEqual(rule["status"], "published")
        original = read_json(ROOT / "config/rules.json")["rules"][0]
        stored = second.get_rule("R001")
        self.assertEqual(stored["original_text"], original["original_text"])
        self.assertEqual(stored["source"]["excerpt"], original["original_text"])

    def test_draft_does_not_change_executable_snapshot(self):
        before = self.store.snapshot()
        draft = self.store.create_draft(self.revised())
        self.assertEqual(draft["revision"], 2)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["published_revision"], 1)
        self.assertEqual(before, self.store.snapshot())
        changed = self.store.diff("R001")["changes"]
        self.assertIn({"path": "condition.all[0].ge", "before": 3.5, "after": 3.8}, changed)

    def test_publish_and_disable_persist_across_startup(self):
        self.store.create_draft(self.revised())
        published = self.store.publish("R001")
        self.assertEqual(published["revision"], 2)
        self.assertTrue(published["active"])
        self.assertEqual(self.store.snapshot("motor")["rules"][0]["condition"]["all"][0]["ge"], 3.8)
        self.store.disable("R001")
        restarted = KnowledgeStore(self.path)
        self.assertFalse(restarted.get_rule("R001")["enabled"])
        self.assertNotIn("R001", [rule["id"] for rule in restarted.snapshot()["rules"]])

    def test_history_body_and_old_snapshot_are_immutable(self):
        before = self.store.snapshot()
        old = self.store.get_rule("R001", 1)
        updated = self.revised()
        self.store.create_draft(updated)
        updated["condition"]["all"][0]["ge"] = 99
        self.store.publish("R001")
        self.assertEqual(self.store.get_rule("R001", 1)["condition"], old["condition"])
        self.assertEqual(before["rules"][0]["condition"]["all"][0]["ge"], 3.5)
        self.assertEqual(self.store.get_rule("R001", 2)["condition"]["all"][0]["ge"], 3.8)
        self.assertEqual([rule["revision"] for rule in self.store.history("R001")], [1, 2])
        with self.assertRaises(DemoError):
            self.store.publish("R001", 1)

    def test_snapshot_filtering_and_stable_hash(self):
        snap = self.store.snapshot("motor")
        self.assertEqual(len(snap["rules"]), 4)
        self.assertEqual({rule["device_type"] for rule in snap["rules"]}, {"motor"})
        self.assertEqual(snap["snapshot_id"], self.store.snapshot("motor")["snapshot_id"])
        self.assertNotEqual(snap["snapshot_id"], self.store.snapshot("pump")["snapshot_id"])
        snap["rules"][0]["actions"].append("外部修改")
        self.assertNotIn("外部修改", self.store.snapshot("motor")["rules"][0]["actions"])

    def test_batch_import_rolls_back_completely(self):
        valid = self.revised()
        invalid = copy.deepcopy(valid)
        invalid["id"] = "R002"
        invalid["name"] = "无效规则"
        invalid["condition"] = {"metric": "imaginary_metric", "label": "不支持", "ge": 1}
        with self.assertRaises(DemoError):
            self.store.create_drafts([valid, invalid])
        self.assertEqual(len(self.store.history("R001")), 1)
        self.assertEqual(len(self.store.history("R002")), 1)

    def test_chinese_search_only_uses_active_published_content(self):
        matches = self.store.search("轴承 润滑", "motor")
        self.assertIn("R001", [rule["id"] for rule in matches])
        revised = self.revised()
        revised["actions"].append("仅在草稿中的专有术语")
        self.store.create_draft(revised)
        self.assertEqual(self.store.search("草稿中的专有术语"), [])
        self.store.publish("R001")
        self.assertEqual([rule["id"] for rule in self.store.search("草稿中的专有术语")], ["R001"])
        self.store.disable("R001")
        self.assertEqual(self.store.search("草稿中的专有术语"), [])

    def test_assign_new_id_and_match_existing_identity(self):
        new = self.revised()
        new.pop("id")
        new["name"] = "电机新增诊断规则"
        draft = self.store.create_draft(new)
        self.assertEqual(draft["id"], "R017")
        self.assertEqual(draft["revision"], 1)
        self.assertFalse(draft["enabled"])
        next_draft = self.store.create_draft(new)
        self.assertEqual(next_draft["id"], "R017")
        self.assertEqual(next_draft["revision"], 2)
        self.assertNotIn("R017", [rule["id"] for rule in self.store.snapshot()["rules"]])

    def test_duplicate_name_and_duplicate_batch_rejected(self):
        conflict = self.revised()
        conflict["id"] = "R099"
        with self.assertRaises(DemoError):
            self.store.create_draft(conflict)
        with self.assertRaises(DemoError):
            self.store.create_drafts([self.revised(), self.revised()])
        self.assertEqual(len(self.store.history("R001")), 1)

    def test_ambiguity_requires_explicit_confirmation(self):
        revised = self.revised()
        revised["review_notes"] = ["低频冲击值的单位待确认"]
        self.store.create_draft(revised)
        with self.assertRaises(DemoError):
            self.store.publish("R001")
        self.assertEqual(self.store.get_rule("R001")["published_revision"], 1)
        published = self.store.publish("R001", confirm_notes=True)
        self.assertTrue(published["review_confirmed"])

    def test_publish_revalidates_stored_conditions(self):
        draft = self.store.create_draft(self.revised())
        # Simulate a malformed imported/externally changed database record.
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT body FROM rule_revisions WHERE rule_id='R001' AND revision=?", (draft["revision"],)).fetchone()
            body = json.loads(row[0])
            body["condition"]["all"][0]["metric"] = "unsupported_sensor"
            conn.execute("UPDATE rule_revisions SET body=? WHERE rule_id='R001' AND revision=?", (json.dumps(body), draft["revision"]))
        with self.assertRaises(DemoError):
            self.store.publish("R001")
        self.assertEqual(self.store.get_rule("R001")["published_revision"], 1)

    def test_sources_survive_revision_and_snapshot(self):
        source = {"title": "修订版说明", "date": "2026-10-22", "sha256": "a" * 64, "excerpt": "新的原始描述"}
        draft = self.store.create_draft(self.revised(), source)
        source["title"] = "调用者后来修改"
        self.assertEqual(draft["source"]["title"], "修订版说明")
        self.store.publish("R001")
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["rules"][0]["source"]["title"], "修订版说明")
        self.assertIn("修订版说明", [source["title"] for source in snapshot["sources"]])

    def test_no_seed_keeps_user_store_separate(self):
        path = Path(self.temp.name) / "custom.db"
        custom = KnowledgeStore(path, seed=False)
        new = self.revised()
        new["id"] = "R100"
        custom.create_draft(new)
        restarted = KnowledgeStore(path)
        self.assertEqual([rule["id"] for rule in restarted.list_rules()], ["R100"])

    def test_draft_rename_matches_both_published_and_latest_draft_names(self):
        original = self.store.get_rule("R001")
        renamed = copy.deepcopy(original)
        renamed["name"] = "电机轴承故障修订名"
        before = self.store.snapshot("motor")
        self.store.create_draft(renamed)
        self.assertEqual(self.store.snapshot("motor"), before)
        renamed.pop("id")
        self.assertEqual(self.store.create_draft(renamed)["id"], "R001")
        original.pop("id")
        imported_original = self.store.create_draft(original)
        self.assertEqual(imported_original["id"], "R001")
        self.assertEqual(imported_original["revision"], 4)
        self.assertEqual(len(self.store.list_rules()), 16)

    def test_draft_cannot_move_existing_id_to_another_device_type(self):
        before = self.store.snapshot()
        changed_type = self.revised()
        changed_type["device_type"] = "pump"
        with self.assertRaisesRegex(DemoError, "不能更换设备类型"):
            self.store.create_draft(changed_type)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(len(self.store.history("R001")), 1)
        original = self.store.get_rule("R001")
        original.pop("id")
        self.assertEqual(self.store.create_draft(original)["id"], "R001")

    def test_all_type_rules_can_be_disabled_without_reseeding(self):
        for rule in self.store.snapshot("motor")["rules"]:
            self.store.disable(rule["id"])
        restarted = KnowledgeStore(self.path)
        self.assertEqual(restarted.snapshot("motor")["rules"], [])
        self.assertEqual(restarted.search("", "motor"), [])
        self.assertEqual(len(restarted.snapshot()["rules"]), 12)
        self.assertEqual(len(restarted.list_rules("motor")), 4)

    def test_published_and_latest_draft_names_prevent_duplicate_ids(self):
        renamed = self.revised()
        renamed["name"] = "电机轴承修订名"
        self.store.create_draft(renamed)
        for name in ("电机滚动轴承故障", "电机轴承修订名"):
            other = self.revised()
            other.update(id="R099", name=name)
            with self.assertRaises(DemoError):
                self.store.create_draft(other)

    def test_cannot_publish_older_draft_when_its_name_is_now_in_use(self):
        revised = self.revised()
        revised["name"] = "临时诊断名称"
        self.store.create_draft(revised)
        revised["name"] = "最终诊断名称"
        self.store.create_draft(revised)
        new = self.revised()
        new.update(id="R017", name="临时诊断名称")
        self.store.create_draft(new)
        with self.assertRaisesRegex(DemoError, "名称.*冲突"):
            self.store.publish("R001", 2)
        self.assertEqual(self.store.get_rule("R001")["published_revision"], 1)
        self.store.publish("R001", 3)
        self.assertEqual(self.store.snapshot("motor")["rules"][0]["name"], "最终诊断名称")

    def test_unknown_rules_and_versions_are_actionable(self):
        for operation in (
            lambda: self.store.get_rule("R999"),
            lambda: self.store.get_rule("R001", 999),
            lambda: self.store.get_rule("R001", True),
            lambda: self.store.publish("R999"),
            lambda: self.store.disable("R999"),
            lambda: self.store.history("R999"),
        ):
            with self.assertRaises(DemoError):
                operation()


if __name__ == "__main__":
    unittest.main()

import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from collections import Counter
from pathlib import Path

from docx import Document

from diagnostic_agent.common import DemoError, ROOT
from diagnostic_agent.knowledge import KnowledgeStore
from diagnostic_agent.knowledge_import import extract_sections, import_document


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.batches = []

    def create_drafts(self, rules, source=None):
        self.batches.append(copy.deepcopy(rules))
        return rules


class FakeModel:
    def __init__(self, results):
        self.results = list(results)
        self.messages = []

    def complete(self, messages, tools=None):
        self.messages.append(copy.deepcopy(messages))
        value = self.results.pop(0)
        return {"role": "assistant", "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}


class KnowledgeImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = MemoryStore(self.root / "knowledge.sqlite3")

    def tearDown(self):
        self.directory.cleanup()

    def document(self, threshold="3.8", extra=None, name="电机滚动轴承故障", filename="rules.docx"):
        document = Document()
        document.add_heading("1.1. " + name, 2)
        document.add_paragraph("机理现象：轴承温度逐渐升高。")
        document.add_paragraph("机理原因：润滑不足。")
        document.add_paragraph("解决措施：检查润滑。")
        document.add_paragraph(f"机理规则：加速度非超额峭度大于等于{threshold}且小于4.5。")
        if extra:
            document.add_paragraph(extra)
        path = self.root / filename
        document.save(path)
        return path

    def rule(self, threshold=3.8):
        return {"device_type": "motor", "name": "电机滚动轴承故障",
                "original_text": f"机理规则：加速度非超额峭度大于等于{threshold}且小于4.5。",
                "phenomena": ["轴承温度逐渐升高。"], "causes": ["润滑不足。"],
                "actions": ["检查润滑。"], "notes": [], "review_notes": [],
                "condition": {"all": [{"metric": "kurtosis", "ge": threshold, "lt": 4.5}]}}

    def test_changed_threshold_is_from_document_and_source_archived(self):
        path = self.document()
        original = path.read_bytes()
        model = FakeModel([self.rule()])
        result = import_document(path, self.store, model)
        self.assertEqual(result[0]["condition"]["all"][0]["ge"], 3.8)
        source = result[0]["source"]
        self.assertEqual(source["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(Path(source["file"]).read_bytes(), original)
        self.assertTrue(Path(source["file"]).is_absolute())
        self.assertEqual(path.read_bytes(), original)
        self.assertIn("段落", source["section"])
        self.assertIn("机理规则", source["excerpt"])
        self.assertIn("非可信业务数据", model.messages[0][0]["content"])
        self.assertEqual(len(self.store.batches), 1)

    def test_single_code_fence_allowed_and_id_explicit_only(self):
        proposed = self.rule()
        proposed["id"] = "R999"
        model = FakeModel(["```json\n" + json.dumps(proposed, ensure_ascii=False) + "\n```"])
        result = import_document(self.document(), self.store, model, rule_id="R001")
        self.assertEqual(result[0]["id"], "R001")
        another = import_document(self.root / "rules.docx", self.store, FakeModel([proposed]))
        self.assertNotIn("id", another[0])
        self.assertEqual(len(list((self.root / "sources").glob("*.docx"))), 1)

    def test_invalid_json_unknown_metric_invented_number_no_store_write(self):
        path = self.document()
        unknown = self.rule()
        unknown["condition"] = {"metric": "temperature", "ge": 3.8}
        invented = self.rule()
        invented["condition"]["all"][0]["ge"] = 999
        for value in ("not json", unknown, invented, "```python\nprint('bad')\n```", "{\"x\":NaN}"):
            with self.subTest(value=value), self.assertRaises(DemoError):
                import_document(path, self.store, FakeModel([value]))
        self.assertEqual(self.store.batches, [])

    def test_description_and_name_must_be_locatable(self):
        path = self.document()
        for field, value in (("causes", ["电流过大。"]), ("name", "电机不存在故障"),
                             ("original_text", "机理规则：虚构文字")):
            rule = self.rule()
            rule[field] = value
            with self.subTest(field=field), self.assertRaises(DemoError):
                import_document(path, self.store, FakeModel([rule]))
        self.assertEqual(self.store.batches, [])

    def test_unverified_model_notes_become_review_notes_and_block_publication(self):
        rule = self.rule()
        rule["notes"] = ["轴承温度逐渐升高。", "设备健康评分98分，可以正常运行。"]
        store = KnowledgeStore(self.root / "publication.sqlite3", seed=False)
        result = import_document(self.document(), store, FakeModel([rule]))[0]
        self.assertEqual(result["notes"], ["轴承温度逐渐升高。"])
        self.assertIn("模型提出的备注，须复核：设备健康评分98分，可以正常运行。", result["review_notes"])
        with self.assertRaisesRegex(DemoError, "复核"):
            store.publish(result["id"])

    def test_labelled_descriptions_are_complete_even_when_model_omits_lines(self):
        document = Document()
        document.add_heading("电机滚动轴承故障", 2)
        for paragraph in ("机理现象：", "1、轴承温度逐渐升高。", "2、振动上升。",
                          "机理原因：润滑不足。", "疲劳剥落。", "解决措施：检查润滑。", "必要时更换轴承。",
                          "机理规则：加速度非超额峭度大于等于3.8且小于4.5。"):
            document.add_paragraph(paragraph)
        path = self.root / "complete.docx"
        document.save(path)
        rule = self.rule()
        rule.update(phenomena=[], causes=["润滑不足。"], actions=["检查润滑。"])
        result = import_document(path, self.store, FakeModel([rule]))[0]
        self.assertEqual(result["phenomena"], ["1、轴承温度逐渐升高。", "2、振动上升。"])
        self.assertEqual(result["causes"], ["润滑不足。", "疲劳剥落。"])
        self.assertEqual(result["actions"], ["检查润滑。", "必要时更换轴承。"])

    def test_unlabelled_description_keeps_locatable_model_quote(self):
        document = Document()
        document.add_heading("电机滚动轴承故障", 2)
        document.add_paragraph("轴承温度逐渐升高。")
        document.add_paragraph("机理规则：加速度非超额峭度大于等于3.8且小于4.5。")
        path = self.root / "unlabelled.docx"
        document.save(path)
        rule = self.rule()
        rule.update(causes=[], actions=[])
        result = import_document(path, self.store, FakeModel([rule]))[0]
        self.assertEqual(result["phenomena"], ["轴承温度逐渐升高。"])

    def test_metric_local_evidence_does_not_accept_other_metric_number(self):
        document = Document()
        document.add_heading("电机滚动轴承故障", 2)
        original = "机理规则：加速度非超额峭度大于等于3.8，低频冲击值大于等于8。"
        document.add_paragraph(original)
        path = self.root / "local.docx"
        document.save(path)
        rule = self.rule()
        rule.update(original_text=original, phenomena=[], causes=[], actions=[],
                    condition={"metric": "kurtosis", "ge": 8})
        with self.assertRaises(DemoError):
            import_document(path, self.store, FakeModel([rule]))
        self.assertEqual(self.store.batches, [])

    def test_ambiguous_grouping_and_model_notes_are_retained(self):
        document = Document()
        document.add_heading("水泵滚动轴承故障", 2)
        original = "机理规则：加速度峭度大于等于3.8且小于4.5，或者低频冲击值大于等于8，峰值因子大于等于4。"
        document.add_paragraph(original)
        path = self.root / "ambiguous.docx"
        document.save(path)
        rule = self.rule()
        rule.update(device_type="pump", name="水泵滚动轴承故障", original_text=original,
                    phenomena=[], causes=[], actions=[], review_notes=["原文未说明低频冲击算法。"],
                    condition={"all": [{"any": [{"metric": "kurtosis", "ge": 3.8, "lt": 4.5},
                                                {"metric": "low_frequency_impact", "ge": 8}]},
                                       {"metric": "crest_factor", "ge": 4}]})
        result = import_document(path, self.store, FakeModel([rule]))[0]
        self.assertIn("原文未说明低频冲击算法。", result["review_notes"])
        self.assertTrue(any("条件分组" in note for note in result["review_notes"]))
        self.assertTrue(any("非超额" in note for note in result["review_notes"]))

    def test_wrong_comparator_creates_review_note(self):
        rule = self.rule()
        rule["condition"]["all"][0].pop("ge")
        rule["condition"]["all"][0]["gt"] = 3.8
        result = import_document(self.document(), self.store, FakeModel([rule]))[0]
        self.assertTrue(any("边界" in note for note in result["review_notes"]))

    def test_omitted_explicit_upper_bound_is_rejected(self):
        rule = self.rule()
        rule["condition"] = {"metric": "kurtosis", "ge": 3.8}
        with self.assertRaisesRegex(DemoError, "缺失"):
            import_document(self.document(), self.store, FakeModel([rule]))
        self.assertEqual(self.store.batches, [])

    def test_explicit_and_replaced_by_any_requires_review(self):
        rule = self.rule()
        rule["condition"] = {"any": [{"metric": "kurtosis", "ge": 3.8}, {"metric": "kurtosis", "lt": 4.5}]}
        result = import_document(self.document(), self.store, FakeModel([rule]))[0]
        self.assertTrue(any("逻辑关系不一致" in note for note in result["review_notes"]))

    def test_whole_explicit_metric_omission_is_rejected(self):
        document = Document()
        document.add_heading("电机滚动轴承故障", 2)
        original = "机理规则：加速度非超额峭度大于等于3.8且小于4.5，低频冲击值大于等于8。"
        document.add_paragraph(original)
        path = self.root / "missing-metric.docx"
        document.save(path)
        rule = self.rule()
        rule.update(original_text=original, phenomena=[], causes=[], actions=[])
        with self.assertRaisesRegex(DemoError, "low_frequency_impact.*缺失"):
            import_document(path, self.store, FakeModel([rule]))
        self.assertEqual(self.store.batches, [])

    def test_explicit_or_replaced_by_all_requires_review(self):
        document = Document()
        document.add_heading("电机滚动轴承故障", 2)
        original = "机理规则：加速度非超额峭度大于等于3.8或峰值因子大于等于4。"
        document.add_paragraph(original)
        path = self.root / "missing-or.docx"
        document.save(path)
        rule = self.rule()
        rule.update(original_text=original, phenomena=[], causes=[], actions=[],
                    condition={"all": [{"metric": "kurtosis", "ge": 3.8}, {"metric": "crest_factor", "ge": 4}]})
        result = import_document(path, self.store, FakeModel([rule]))[0]
        self.assertTrue(any("逻辑关系可能被遗漏" in note for note in result["review_notes"]))

    def test_reference_has_sixteen_business_sections_not_toc(self):
        sections = extract_sections(ROOT / "assets/rules_reference.docx")
        self.assertEqual(len(sections), 16)
        self.assertEqual(Counter(section["device_type"] for section in sections),
                         {"motor": 4, "coupling": 1, "pump": 5, "fan": 5, "gearbox": 1})
        self.assertEqual(sections[0]["title"], "电机滚动轴承故障")
        self.assertEqual(sections[-1]["title"], "齿轮箱滚动轴承故障")
        self.assertEqual(sections[0]["text"].count("机理规则"), 1)
        self.assertNotIn("目  录", sections[0]["text"])

    def test_reference_sixteen_proposals_can_be_imported_together(self):
        proposals = json.loads((ROOT / "config/rules.json").read_text(encoding="utf-8"))["rules"]
        result = import_document(ROOT / "assets/rules_reference.docx", self.store, FakeModel(proposals))
        self.assertEqual(len(result), 16)
        self.assertEqual(len(self.store.batches), 1)
        self.assertEqual(result[0]["phenomena"], ["1、电机端各振测点振动幅值上升明显；",
                                                "2、借助听诊设备，可探听到摩擦冲击声；",
                                                "3、轴承温度逐渐升高。"])
        self.assertEqual(result[0]["actions"], ["检查润滑油理化情况，选择更换润滑油，严重时更换轴承。"])
        self.assertEqual(len(list((self.root / "sources").glob("*.docx"))), 1)

    def test_table_paragraph_order_and_single_unstyled_document(self):
        document = Document()
        document.add_paragraph("1.1. 电机滚动轴承故障")
        table = document.add_table(rows=3, cols=2)
        for row, label, value in ((0, "机理原因", "润滑不足。"), (1, "解决措施", "检查润滑。"),
                                  (2, "机理规则", "加速度非超额峭度大于等于3.8且小于4.5。")):
            table.cell(row, 0).text = label
            table.cell(row, 1).text = value
        path = self.root / "table.docx"
        document.save(path)
        sections = extract_sections(path)
        self.assertEqual(len(sections), 1)
        self.assertLess(sections[0]["text"].index("机理原因"), sections[0]["text"].index("解决措施"))
        self.assertIn("表格", sections[0]["locator"])
        rule = self.rule()
        rule["phenomena"] = []
        rule["original_text"] = "机理规则\n加速度非超额峭度大于等于3.8且小于4.5。"
        result = import_document(path, self.store, FakeModel([rule]))
        self.assertEqual(len(result), 1)

    def test_empty_missing_marker_and_multi_section_id_rejected(self):
        empty = Document()
        path = self.root / "empty.docx"
        empty.save(path)
        with self.assertRaises(DemoError):
            extract_sections(path)
        empty.add_paragraph("文档只包含说明")
        empty.save(path)
        with self.assertRaises(DemoError):
            extract_sections(path)
        with self.assertRaises(DemoError):
            import_document(ROOT / "assets/rules_reference.docx", self.store, FakeModel([]), rule_id="R001")
        self.assertEqual(self.store.batches, [])

    def test_malformed_and_oversized_files_are_rejected_without_model_call(self):
        malformed = self.root / "broken.docx"
        malformed.write_bytes(b"not a Word archive")
        with self.assertRaises(DemoError):
            import_document(malformed, self.store, FakeModel([]))
        path = self.document()
        with patch("diagnostic_agent.knowledge_import.MAX_FILE_BYTES", 5), self.assertRaises(DemoError):
            import_document(path, self.store, FakeModel([]))
        self.assertEqual(self.store.batches, [])

    def test_all_candidates_validated_before_atomic_batch(self):
        document = Document()
        for name in ("电机滚动轴承故障", "水泵滚动轴承故障"):
            document.add_heading(name, 2)
            document.add_paragraph("机理规则：加速度非超额峭度大于等于3.8且小于4.5。")
        path = self.root / "two.docx"
        document.save(path)
        first = self.rule()
        first.update(phenomena=[], causes=[], actions=[])
        with self.assertRaises(DemoError):
            import_document(path, self.store, FakeModel([first, "invalid"]))
        self.assertEqual(self.store.batches, [])


if __name__ == "__main__":
    unittest.main()

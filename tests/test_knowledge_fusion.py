"""Fusion regressions use temporary databases and deterministic models."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.knowledge_fusion.pipeline import (
    candidates,
    fuse,
    validate_organization,
)
from rag_service.knowledge_fusion.schema import Organization
from rag_service.knowledge_fusion.evaluation import evaluate
from rag_service.knowledge_fusion.sources import windows


class Embedding:
    def profile(self):
        return {"mode": "test", "model": "fusion", "fingerprint": "fusion-test"}

    def embed(self, texts):
        return [[1.0, 0.3] for text in texts]

    def matches(self, p):
        return p == self.profile()


class Model:
    def __init__(self):
        self.calls = []
        self.bad_quote = False
        self.fail_relation = False

    def complete(self, messages):
        data = json.loads(messages[-1]["content"])
        self.calls.append(data)
        title = data["schema"]["title"]
        if title == "Extraction":
            result = {
                "claims": [
                    dict(
                        concept="缓存",
                        knowledge_type="fact",
                        statement=s["text"],
                        conditions=["读取时"],
                        qualifiers=[],
                        evidence=[
                            dict(
                                source_span_id=s["id"],
                                quote="捏造引用" if self.bad_quote else s["text"],
                            )
                        ],
                    )
                    for s in data["spans"]
                ]
            }
        elif title == "Judgment":
            if self.fail_relation:
                raise ValueError("模拟关系请求失败")
            a, b = data["claims"]
            relation = "equivalent" if a["statement"] == b["statement"] else "conflict"
            result = dict(
                relation=relation,
                reason="测试语义判断",
                conditions_comparison="相同适用条件",
                conditions_match=True,
                merge_recommended=relation == "equivalent",
                broader_claim_id=None,
            )
        elif title == "Organization":
            ids = [k["id"] for k in data["knowledge"]]
            result = dict(
                concepts=[dict(id="concept", name="缓存", knowledge_ids=ids)],
                topics=[
                    dict(id="root", name="缓存机制", parent_id=None, knowledge_ids=[]),
                    dict(
                        id="child", name="读取行为", parent_id="root", knowledge_ids=ids
                    ),
                ],
            )
        elif title == "Article":
            result = dict(
                title="缓存专题",
                paragraphs=[
                    dict(
                        text="资料对读取行为存在分歧，应结合各自证据理解。",
                        knowledge_ids=[k["id"] for k in data["knowledge"]],
                    )
                ],
                insufficient_evidence=False,
            )
        else:
            raise AssertionError(title)
        return {"content": json.dumps(result, ensure_ascii=False)}


class FusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"RAG_FUSION_ENABLED": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sleep = patch("rag_service.knowledge_fusion.pipeline.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.model = Model()
        self.settings = Settings(
            Path(self.tmp.name), "admin-token-123", "read-token-123"
        )
        self.app = create_app(self.settings, embedder=Embedding(), model=self.model)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers["Authorization"] = "Bearer admin-token-123"
        self.base = "/api/v1/admin/knowledge-fusion"
        self.source = self.app.state.store
        self.docs = []
        for i, text in enumerate(
            [
                "缓存能够复用已有结果。",
                "缓存能够复用已有结果。",
                "每次读取均重新计算结果。",
            ]
        ):
            kb = self.source.create_kb("来源" + str(i))["id"]
            u = self.source.reserve_upload(kb, "source.md", text.encode())
            d = self.source.finish_upload(
                u["document_id"],
                1,
                dict(
                    blocks=[
                        dict(
                            id="b1",
                            text=text,
                            locator="行1",
                            kind="uncertain",
                            section="读取",
                        )
                    ],
                    rules=[],
                    warnings=[],
                ),
            )
            self.docs.append(d)
        self.project = self.client.post(
            self.base + "/projects", json={"name": "融合试验"}
        ).json()["id"]

    def start(self):
        body = {
            "documents": [
                dict(
                    document_id=d["id"],
                    revision=d["latest_revision"],
                    edit_version=d["edit_version"],
                )
                for d in self.docs
            ]
        }
        r = self.client.post(
            self.base + "/projects/" + self.project + "/runs", json=body
        )
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["id"]

    def get(self, rid, name=""):
        r = self.client.get(self.base + "/runs/" + rid + ("/" + name if name else ""))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_full_cross_library_pipeline_summary_and_labels(self):
        rid = self.start()
        self.assertEqual(self.get(rid)["status"], "succeeded")
        claims = self.get(rid, "claims")
        knowledge = self.get(rid, "knowledge")
        self.assertEqual(len(claims), 3)
        self.assertEqual(len(knowledge), 2)
        self.assertEqual(len(self.get(rid, "relations")), 3)
        topic = next(t for t in self.get(rid, "topics") if t["parent_id"] is None)
        response = self.client.post(
            self.base + "/runs/" + rid + "/summaries", json={"topic_id": topic["id"]}
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.get(rid)["status"], "succeeded")
        self.assertEqual(len(self.get(rid, "summaries")), 1)
        rows = self.get(rid, "annotations")
        labels = [
            dict(claim_a=r["claim_a"], claim_b=r["claim_b"], relation=r["prediction"])
            for r in rows
        ]
        r = self.client.put(
            self.base + "/runs/" + rid + "/annotations", json={"labels": labels}
        )
        self.assertEqual(r.status_code, 200, r.text)
        metrics = self.get(rid, "evaluation")
        self.assertEqual(metrics["f1"], 1)
        self.assertEqual(metrics["traceable_ratio"], 1)
        text = self.client.get(self.base + "/runs/" + rid + "/annotations.jsonl").text
        self.assertEqual(len([json.loads(l) for l in text.splitlines()]), 3)
        self.assertTrue(all(self.source.document(d["id"]) == d for d in self.docs))

    def test_invalid_quotes_fail_and_retry_keeps_run_identity(self):
        self.model.bad_quote = True
        rid = self.start()
        self.assertEqual(self.get(rid)["status"], "failed")
        self.assertEqual(self.get(rid, "claims"), [])
        self.model.bad_quote = False
        r = self.client.post(self.base + "/runs/" + rid + "/retry")
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.get(rid)["status"], "succeeded")

    def test_relation_failure_preserves_extraction_checkpoints(self):
        self.model.fail_relation = True
        rid = self.start()
        self.assertEqual(self.get(rid)["status"], "failed")
        calls = sum(x["schema"]["title"] == "Extraction" for x in self.model.calls)
        self.model.fail_relation = False
        self.client.post(self.base + "/runs/" + rid + "/retry")
        self.assertEqual(self.get(rid)["status"], "succeeded")
        self.assertEqual(
            sum(x["schema"]["title"] == "Extraction" for x in self.model.calls), calls
        )

    def test_version_conflict_and_duplicate_selection(self):
        d = self.docs[0]
        entry = dict(document_id=d["id"], revision=1, edit_version=1)
        r = self.client.post(
            self.base + "/projects/" + self.project + "/runs",
            json={"documents": [entry]},
        )
        self.assertEqual(r.status_code, 409)
        entry["edit_version"] = d["edit_version"]
        r = self.client.post(
            self.base + "/projects/" + self.project + "/runs",
            json={"documents": [entry, entry]},
        )
        self.assertEqual(r.status_code, 400)

    def test_history_original_is_frozen_after_replacement(self):
        rid = self.start()
        d = self.docs[0]
        old = self.client.get(
            self.base + "/runs/" + rid + "/documents/" + d["id"] + "/original"
        ).content
        u = self.source.reserve_upload(
            d["kb_id"], "new.md", b"new version", document_id=d["id"]
        )
        self.source.finish_upload(
            d["id"],
            u["revision"],
            dict(
                blocks=[
                    dict(id="b1", text="新资料", locator="行1", kind="description")
                ],
                rules=[],
                warnings=[],
            ),
        )
        self.assertEqual(
            self.client.get(
                self.base + "/runs/" + rid + "/documents/" + d["id"] + "/original"
            ).content,
            old,
        )

    def test_admin_auth_and_feature_disabled(self):
        self.client.headers["Authorization"] = "Bearer read-token-123"
        self.assertEqual(self.client.get(self.base + "/projects").status_code, 403)
        self.client.headers.clear()
        self.assertEqual(self.client.get(self.base + "/status").status_code, 401)
        with patch.dict(os.environ, {"RAG_FUSION_ENABLED": "false"}):
            app = create_app(
                Settings(
                    Path(self.tmp.name) / "off", "admin-token-123", "read-token-123"
                ),
                embedder=Embedding(),
            )
            with TestClient(app) as c:
                c.headers["Authorization"] = "Bearer admin-token-123"
                self.assertFalse(c.get(self.base + "/status").json()["enabled"])
                self.assertEqual(c.get(self.base + "/projects").status_code, 404)
            self.assertFalse((Path(self.tmp.name) / "off/fusion").exists())

    def test_active_task_uniqueness_and_restart_recovery(self):
        rid = self.start()
        store = self.app.state.fusion_store
        store.update(rid, status="queued")
        with self.assertRaises(ValueError):
            store.create(
                self.project,
                dict(documents=[], spans=[], profile={}, top_k=5, same_document=False),
            )
        from rag_service.knowledge_fusion.store import FusionStore

        recovered = FusionStore(self.settings.data_dir)
        self.assertEqual(recovered.get(rid)["status"], "interrupted")
        self.client.post(self.base + "/runs/" + rid + "/retry")
        self.assertEqual(self.get(rid)["status"], "succeeded")

    def test_profile_change_rejects_resume(self):
        rid = self.start()
        store = self.app.state.fusion_store
        run = store.get(rid)
        run["profile"]["model"]["model"] = "old"
        store.update(rid, status="failed", profile=run["profile"])
        self.assertEqual(
            self.client.post(self.base + "/runs/" + rid + "/retry").status_code, 409
        )

    def test_unlabeled_metrics_and_missing_candidate_false_negative(self):
        rid = self.start()
        self.assertIsNone(self.get(rid, "evaluation")["f1"])
        store = self.app.state.fusion_store
        run = store.get(rid)
        pair = next(r for r in run["relations"] if r["relation"] == "equivalent")
        store.update(
            rid,
            relations=[],
            labels=[
                dict(
                    claim_a=pair["claim_a"],
                    claim_b=pair["claim_b"],
                    relation="equivalent",
                )
            ],
        )
        self.assertEqual(self.get(rid, "evaluation")["false_negative"], 1)
        self.assertEqual(self.get(rid, "evaluation")["recall"], 0)

    def test_pagination_and_subset_export(self):
        rid = self.start()
        r = self.client.get(self.base + "/runs/" + rid + "/claims?offset=1&limit=1")
        self.assertEqual(len(r.json()), 1)
        ids = [c["id"] for c in self.get(rid, "claims")]
        r = self.client.get(
            self.base + "/runs/" + rid + "/annotations",
            params=[("claim_ids", i) for i in ids],
        )
        self.assertEqual(len(r.json()), 3)

    def test_summary_invalid_citations_fail_then_resume(self):
        rid = self.start()
        topic = self.get(rid, "topics")[0]["id"]
        original = self.model.complete

        def broken(messages):
            data = json.loads(messages[-1]["content"])
            if data["schema"]["title"] == "Article":
                return {
                    "content": json.dumps(
                        dict(
                            title="错误专题",
                            paragraphs=[dict(text="无来源", knowledge_ids=["unknown"])],
                            insufficient_evidence=False,
                        )
                    )
                }
            return original(messages)

        self.model.complete = broken
        self.client.post(
            self.base + "/runs/" + rid + "/summaries", json={"topic_id": topic}
        )
        self.assertEqual(self.get(rid)["status"], "failed")
        self.assertEqual(self.get(rid, "summaries"), [])
        self.model.complete = original
        self.client.post(self.base + "/runs/" + rid + "/retry")
        self.assertEqual(self.get(rid)["status"], "succeeded")
        self.assertEqual(len(self.get(rid, "summaries")), 1)

    def test_exclusion_long_spans_and_original_text(self):
        from rag_service.knowledge_fusion.sources import snapshots
        from rag_service.knowledge_fusion.routes import Selection

        d = self.docs[0]
        text = "表头\n" + ("一行数据\n" * 1500)
        u = self.source.reserve_upload(
            d["kb_id"], "table.md", text.encode(), document_id=d["id"]
        )
        doc = self.source.finish_upload(
            d["id"],
            u["revision"],
            dict(
                blocks=[
                    dict(
                        id="table",
                        text=text,
                        locator="行1",
                        kind="description",
                        content_types=["table"],
                    ),
                    dict(
                        id="formula",
                        text="未核对公式",
                        locator="行2",
                        kind="uncertain",
                        content_types=["formula"],
                    ),
                    dict(
                        id="excluded",
                        text="人工排除",
                        locator="行3",
                        kind="description",
                        excluded=True,
                    ),
                ],
                rules=[],
                warnings=[],
            ),
        )
        _, spans = snapshots(
            self.source,
            [
                Selection(
                    document_id=doc["id"],
                    revision=doc["latest_revision"],
                    edit_version=doc["edit_version"],
                )
            ],
        )
        pieces = [x for x in spans if x["block_id"] == "table"]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(x["text"] for x in pieces), text)
        self.assertTrue(all(x["excluded"] for x in spans if x["block_id"] != "table"))
        self.assertEqual(pieces[-1]["end"], len(text))

    def test_multiple_organization_batches_are_globally_merged(self):
        for index in range(5):
            text = ("知识内容" + str(index)) * 600
            kb = self.source.create_kb("长测试资料" + str(index))["id"]
            upload = self.source.reserve_upload(kb, "batch.md", text.encode())
            self.docs.append(
                self.source.finish_upload(
                    upload["document_id"],
                    1,
                    dict(
                        blocks=[
                            dict(
                                id="b1", text=text, locator="段落1", kind="description"
                            )
                        ],
                        rules=[],
                        warnings=[],
                    ),
                )
            )
        rid = self.start()
        self.assertEqual(self.get(rid)["status"], "succeeded")
        self.assertEqual(len(self.get(rid, "concepts")), 1)
        self.assertTrue(any("draft_concepts" in call for call in self.model.calls))

    def test_invalid_and_contradictory_annotations_are_rejected(self):
        rid = self.start()
        rows = self.get(rid, "annotations")
        first = dict(
            claim_a=rows[0]["claim_a"],
            claim_b=rows[0]["claim_b"],
            relation="equivalent",
        )
        opposite = dict(first, relation="conflict")
        response = self.client.put(
            self.base + "/runs/" + rid + "/annotations",
            json={"labels": [first, opposite]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.get(rid, "evaluation")["labeled_pairs"], 0)
        first["claim_b"] = "missing"
        self.assertEqual(
            self.client.put(
                self.base + "/runs/" + rid + "/annotations", json={"labels": [first]}
            ).status_code,
            400,
        )

    def test_blank_project_and_empty_evidence_are_rejected(self):
        from rag_service.knowledge_fusion.schema import Evidence

        self.assertEqual(
            self.client.post(self.base + "/projects", json={"name": "   "}).status_code,
            422,
        )
        with self.assertRaises(ValueError):
            Evidence(source_span_id="span", quote="   ")

    def test_windows_keep_coverage_and_isolate_documents(self):
        spans = [
            dict(
                id=str(i),
                document_id=str(i // 3),
                section="s",
                text="x" * 2700,
                excluded=False,
            )
            for i in range(6)
        ]
        groups = windows(spans)
        self.assertEqual({s["id"] for w in groups for s in w}, {s["id"] for s in spans})
        self.assertTrue(all(len({s["document_id"] for s in w}) == 1 for w in groups))

    def test_conservative_clique_and_condition_guard(self):
        claims = [
            dict(
                id=i,
                concept="c",
                statement=i,
                knowledge_type="fact",
                conditions=[],
                qualifiers=[],
                evidence=[],
            )
            for i in ["a", "b", "c"]
        ]
        relations = [
            dict(
                claim_a=a,
                claim_b=b,
                relation="equivalent",
                conditions_match=True,
                merge_recommended=True,
            )
            for a, b in [("a", "b"), ("b", "c")]
        ]
        self.assertEqual(len(fuse(claims, relations)), 2)
        relations[0]["conditions_match"] = False
        self.assertEqual(len(fuse(claims, relations)), 2)
        for relation in [
            "complementary",
            "broader_narrower",
            "conflict",
            "unrelated",
            "uncertain",
        ]:
            self.assertEqual(
                len(
                    fuse(
                        claims,
                        [
                            dict(
                                claim_a="a",
                                claim_b="b",
                                relation=relation,
                                conditions_match=True,
                                merge_recommended=False,
                            )
                        ],
                    )
                ),
                3,
            )

    def test_candidates_prioritize_cross_document_and_optionally_same_document(self):
        claims = [dict(id=str(i), document_id=str(i // 2)) for i in range(4)]
        vectors = [[1.0, 0.5]] * 4
        cross = candidates(claims, vectors, 1)
        self.assertTrue(
            all(
                claims[int(p["claim_a"])]["document_id"]
                != claims[int(p["claim_b"])]["document_id"]
                for p in cross
            )
        )
        self.assertGreater(len(candidates(claims, vectors, 1, True)), len(cross))

    def test_topic_cycles_unknown_references_and_missing_coverage(self):
        base = dict(
            concepts=[dict(id="c", name="c", knowledge_ids=["k"])],
            topics=[
                dict(id="a", name="a", parent_id="b", knowledge_ids=["k"]),
                dict(id="b", name="b", parent_id="a", knowledge_ids=[]),
            ],
        )
        with self.assertRaises(ValueError):
            validate_organization(Organization.model_validate(base), {"k"})
        base["topics"][0]["parent_id"] = None
        base["topics"][1]["parent_id"] = "a"
        base["topics"][0]["knowledge_ids"] = ["fake"]
        with self.assertRaises(ValueError):
            validate_organization(Organization.model_validate(base), {"k"})


if __name__ == "__main__":
    unittest.main()

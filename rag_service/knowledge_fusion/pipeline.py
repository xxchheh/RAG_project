"""Checkpointed extraction, pair classification and conservative fusion."""

import hashlib
import json
import logging
from pathlib import Path
import time
import numpy as np
from .schema import Extraction, Judgment, Organization, Article
from .sources import windows
from .store import identifier
from ..embeddings import validate_vectors
from ..common import DemoError

LOG = logging.getLogger(__name__)
PROMPTS = Path(__file__).parent / "prompts"


def profile(model, embedder):
    config = getattr(model, "config", None)
    return {
        "model": {
            key: getattr(config, key, "custom")
            for key in ("provider", "model", "base_url")
        },
        "embedding": embedder.profile(),
        "prompts": {
            p.stem: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(PROMPTS.glob("*.md"))
        },
    }


def ask(model, prompt, schema, payload, validate=lambda value: None):
    system = (PROMPTS / (prompt + ".md")).read_text()
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {"schema": schema.model_json_schema(), **payload}, ensure_ascii=False
            ),
        },
    ]
    for attempt in range(3):
        try:
            message = model.complete(messages)
            if message.get("tool_calls"):
                raise ValueError("需要 JSON 结果")
            text = message["content"].strip()
            if text.startswith("```json") and text.endswith("```"):
                text = text[7:-3].strip()
            value = schema.model_validate_json(text)
            validate(value)
            return value.model_dump()
        except Exception as exc:
            LOG.warning(
                "fusion stage=%s attempt=%d error_type=%s",
                prompt,
                attempt + 1,
                type(exc).__name__,
            )
            if attempt == 2:
                detail = (
                    str(exc) if isinstance(exc, DemoError) else "模型或证据校验失败"
                )
                raise ValueError(
                    f"{prompt} 阶段经过3次尝试仍未完成。{detail}"
                ) from None
            time.sleep(attempt + 1)


def candidates(claims, vectors, top_k=5, same_document=False):
    if not claims:
        return []
    matrix = np.asarray(validate_vectors(vectors, len(claims)), dtype=np.float64)
    matrix /= np.max(np.abs(matrix), axis=1, keepdims=True)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    found = {}
    # Row-wise scores bound memory; only selected pairs reach the LLM.
    for i, claim in enumerate(claims):
        scores = matrix @ matrix[i]
        groups = [False, True] if same_document else [False]
        for same in groups:
            eligible = [
                j
                for j, other in enumerate(claims)
                if i != j and (other["document_id"] == claim["document_id"]) == same
            ]
            for j in sorted(eligible, key=lambda j: (-scores[j], claims[j]["id"]))[
                :top_k
            ]:
                a, b = sorted((claim["id"], claims[j]["id"]))
                found[(a, b)] = dict(
                    id=a + "__" + b,
                    claim_a=a,
                    claim_b=b,
                    similarity=float(np.clip(scores[j], -1, 1)),
                )
    return [found[key] for key in sorted(found)]


def fuse(claims, relations):
    judgments = {tuple(sorted((r["claim_a"], r["claim_b"]))): r for r in relations}
    groups = []
    for claim in sorted(claims, key=lambda c: c["id"]):
        destination = None
        for group in groups:
            checks = [
                judgments.get(tuple(sorted((claim["id"], member["id"]))), {})
                for member in group
            ]
            if all(
                r.get("relation") == "equivalent"
                and r.get("conditions_match")
                and r.get("merge_recommended")
                for r in checks
            ):
                destination = group
                break
        if destination is None:
            groups.append([claim])
        else:
            destination.append(claim)
    result = []
    for group in groups:
        representative = max(group, key=lambda c: (len(c["statement"]), c["id"]))
        result.append(
            dict(
                id="knowledge-" + group[0]["id"],
                concept=representative["concept"],
                knowledge_type=representative["knowledge_type"],
                statement=representative["statement"],
                conditions=representative["conditions"],
                qualifiers=representative["qualifiers"],
                claim_ids=[c["id"] for c in group],
                evidence=[e for c in group for e in c["evidence"]],
            )
        )
    return result


def validate_organization(value, knowledge_ids, existing_ids=()):
    topics = {t.id: t for t in value.topics}
    if len(topics) != len(value.topics) or len({c.id for c in value.concepts}) != len(
        value.concepts
    ):
        raise ValueError("目录编号重复")
    allowed = set(knowledge_ids) | set(existing_ids)
    for nodes in (value.concepts, value.topics):
        if any(not set(n.knowledge_ids) <= allowed for n in nodes):
            raise ValueError("目录引用未知知识")
        covered = {k for node in nodes for k in node.knowledge_ids}
        if not set(knowledge_ids) <= covered:
            raise ValueError("目录缺少知识关联")
    for topic in value.topics:
        seen, current = set(), topic
        while current.parent_id:
            if current.id in seen or current.parent_id not in topics:
                raise ValueError("目录循环或父节点不存在")
            seen.add(current.id)
            current = topics[current.parent_id]
        descendants = {topic.id}
        for _ in range(len(topics)):
            descendants.update(t.id for t in value.topics if t.parent_id in descendants)
        if not any(topics[key].knowledge_ids for key in descendants):
            raise ValueError("空目录节点")


def batches(items, limit=18000):
    group, size = [], 0
    for item in items:
        length = len(json.dumps(item, ensure_ascii=False))
        if group and size + length > limit:
            yield group
            group, size = [], 0
        group.append(item)
        size += length
    if group:
        yield group


class Pipeline:
    def __init__(self, store, embedder, model_factory):
        self.store, self.embedder, self.model_factory = store, embedder, model_factory

    def run(self, run_id):
        try:
            run = self.store.update(run_id, status="processing", error=None)
            model = self.model_factory()
            if profile(model, self.embedder) != run["profile"]:
                raise ValueError(
                    "历史模型或 Prompt 配置不可用，请恢复对应配置或创建新运行"
                )
            if run.get("pending_topic"):
                self.summary(run, model)
                return
            if "extraction" not in run["checkpoints"]:
                self.store.update(run_id, stage="extraction")
                for number, window in enumerate(windows(run["spans"])):
                    key = "window-" + str(number)
                    if key in run["checkpoints"]:
                        continue
                    by_id = {s["id"]: s for s in window}

                    def validate(value):
                        for claim in value.claims:
                            for evidence in claim.evidence:
                                if (
                                    evidence.source_span_id not in by_id
                                    or evidence.quote
                                    not in by_id[evidence.source_span_id]["text"]
                                ):
                                    raise ValueError("引用无法在输入原文中定位")

                    output = ask(
                        model, "extraction", Extraction, {"spans": window}, validate
                    )
                    for raw in output["claims"]:
                        document_id = by_id[raw["evidence"][0]["source_span_id"]][
                            "document_id"
                        ]
                        digest = hashlib.sha256(
                            json.dumps(
                                [document_id, raw], sort_keys=True, ensure_ascii=False
                            ).encode()
                        ).hexdigest()[:24]
                        claim = dict(
                            id="claim-" + digest, document_id=document_id, **raw
                        )
                        if claim["id"] not in {c["id"] for c in run["claims"]}:
                            run["claims"].append(claim)
                    run["checkpoints"][key] = True
                    run = self.store.update(
                        run_id, claims=run["claims"], checkpoints=run["checkpoints"]
                    )
                run["checkpoints"]["extraction"] = True
                run = self.store.update(run_id, checkpoints=run["checkpoints"])
            if "matching" not in run["checkpoints"]:
                self.store.update(run_id, stage="matching")
                texts = [
                    "\n".join(
                        [
                            c["concept"],
                            c["statement"],
                            *c["conditions"],
                            *c["qualifiers"],
                        ]
                    )
                    for c in run["claims"]
                ]
                vectors = self.embedder.embed(texts) if texts else []
                pairs = candidates(
                    run["claims"], vectors, run["top_k"], run["same_document"]
                )
                run["checkpoints"]["matching"] = True
                run = self.store.update(
                    run_id,
                    vectors=vectors,
                    candidates=pairs,
                    checkpoints=run["checkpoints"],
                )
            if "relations" not in run["checkpoints"]:
                self.store.update(run_id, stage="relations")
                by_id = {c["id"]: c for c in run["claims"]}
                done = {r["id"] for r in run["relations"]}
                span_map = {s["id"]: s for s in run["spans"]}
                for pair in run["candidates"]:
                    if pair["id"] in done:
                        continue
                    selected = [by_id[pair["claim_a"]], by_id[pair["claim_b"]]]
                    evidence = [
                        span_map[e["source_span_id"]]
                        for c in selected
                        for e in c["evidence"]
                    ]

                    def validate(value):
                        if (
                            value.relation == "broader_narrower"
                            and value.broader_claim_id
                            not in (pair["claim_a"], pair["claim_b"])
                        ):
                            raise ValueError("概括方向缺失")
                        if value.merge_recommended and (
                            value.relation != "equivalent" or not value.conditions_match
                        ):
                            raise ValueError("合并建议与关系判断矛盾")

                    judgment = ask(
                        model,
                        "relation",
                        Judgment,
                        {"claims": selected, "spans": evidence},
                        validate,
                    )
                    run["relations"].append(dict(**pair, **judgment))
                    run = self.store.update(run_id, relations=run["relations"])
                run["checkpoints"]["relations"] = True
                run = self.store.update(run_id, checkpoints=run["checkpoints"])
            if "fusion" not in run["checkpoints"]:
                run["checkpoints"]["fusion"] = True
                run = self.store.update(
                    run_id,
                    stage="fusion",
                    knowledge=fuse(run["claims"], run["relations"]),
                    checkpoints=run["checkpoints"],
                )
            if "organization" not in run["checkpoints"]:
                self.store.update(run_id, stage="organization")
                # Batches persist cumulative organization; prior memberships remain immutable.
                chunks = list(batches(run["knowledge"]))
                for number, chunk in enumerate(chunks):
                    key = "organization-" + str(number)
                    if key in run["checkpoints"]:
                        continue
                    ids = {k["id"] for k in chunk}
                    output = ask(
                        model,
                        "organization",
                        Organization,
                        {"knowledge": chunk},
                        lambda v: validate_organization(v, ids),
                    )
                    prefix = f"batch-{number}-"
                    for concept in output["concepts"]:
                        concept["id"] = prefix + concept["id"]
                    for topic in output["topics"]:
                        topic["id"] = prefix + topic["id"]
                        if topic["parent_id"]:
                            topic["parent_id"] = prefix + topic["parent_id"]
                    run["concepts"].extend(output["concepts"])
                    run["topics"].extend(output["topics"])
                    run["checkpoints"][key] = True
                    run = self.store.update(
                        run_id,
                        concepts=run["concepts"],
                        topics=run["topics"],
                        checkpoints=run["checkpoints"],
                    )
                if len(chunks) > 1:
                    compact = [
                        {
                            key: k[key]
                            for key in (
                                "id",
                                "concept",
                                "statement",
                                "conditions",
                                "qualifiers",
                            )
                        }
                        for k in run["knowledge"]
                    ]
                    if len(json.dumps(compact, ensure_ascii=False)) > 80000:
                        raise ValueError(
                            "本轮 Demo 的全局主题输入超过80000字，请缩小所选短资料范围"
                        )
                    merged = ask(
                        model,
                        "organization",
                        Organization,
                        {
                            "knowledge": compact,
                            "draft_concepts": run["concepts"],
                            "draft_topics": run["topics"],
                        },
                        lambda v: validate_organization(v, {k["id"] for k in compact}),
                    )
                    run = self.store.update(
                        run_id, concepts=merged["concepts"], topics=merged["topics"]
                    )
                run["checkpoints"]["organization"] = True
                run = self.store.update(run_id, checkpoints=run["checkpoints"])
            self.store.update(run_id, status="succeeded", stage="complete")
        except Exception as exc:
            LOG.warning("fusion run=%s error_type=%s", run_id, type(exc).__name__)
            self.store.update(
                run_id,
                status="failed",
                error=(
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "处理异常，请检查服务日志及依赖环境"
                ),
            )

    def summary(self, run, model):
        topic_id = run["pending_topic"]
        descendants = {topic_id}
        for _ in run["topics"]:
            descendants.update(
                t["id"] for t in run["topics"] if t["parent_id"] in descendants
            )
        ids = {
            k
            for t in run["topics"]
            if t["id"] in descendants
            for k in t["knowledge_ids"]
        }
        selected = [k for k in run["knowledge"] if k["id"] in ids]
        mapping = {c: k["id"] for k in selected for c in k["claim_ids"]}
        conflicts = [
            r
            for r in run["relations"]
            if r["relation"] == "conflict"
            and (r["claim_a"] in mapping or r["claim_b"] in mapping)
        ]
        # Include the other side even when taxonomy placed it outside the chosen topic.
        for relation in conflicts:
            for claim_id in (relation["claim_a"], relation["claim_b"]):
                for knowledge in run["knowledge"]:
                    if (
                        claim_id in knowledge["claim_ids"]
                        and knowledge["id"] not in ids
                    ):
                        selected.append(knowledge)
                        ids.add(knowledge["id"])
                    if claim_id in knowledge["claim_ids"]:
                        mapping[claim_id] = knowledge["id"]
        conflicts = [
            {
                **r,
                "knowledge_a": mapping[r["claim_a"]],
                "knowledge_b": mapping[r["claim_b"]],
            }
            for r in conflicts
        ]
        span_ids = {e["source_span_id"] for k in selected for e in k["evidence"]}
        evidence = [s for s in run["spans"] if s["id"] in span_ids]

        def validate(article):
            cited = {i for p in article.paragraphs for i in p.knowledge_ids}
            if not cited <= ids or (selected and not article.paragraphs):
                raise ValueError("专题引用无效")
            for r in conflicts:
                if not {mapping[r["claim_a"]], mapping[r["claim_b"]]} <= cited:
                    raise ValueError("专题未覆盖冲突双方")

        drafts = []
        for chunk in batches(selected):
            chunk_ids = {k["id"] for k in chunk}

            def validate_draft(article):
                if any(
                    not set(p.knowledge_ids) <= chunk_ids for p in article.paragraphs
                ):
                    raise ValueError("分批专题引用无效")

            drafts.append(
                ask(
                    model,
                    "summary",
                    Article,
                    {"knowledge": chunk, "conflicts": conflicts},
                    validate_draft,
                )
            )
        # The final synthesis uses concise grounded drafts plus all canonical statements.
        compact = [
            {
                key: k[key]
                for key in ("id", "statement", "conditions", "qualifiers", "evidence")
            }
            for k in selected
        ]
        if (
            len(
                json.dumps(
                    {"knowledge": compact, "drafts": drafts, "conflicts": conflicts},
                    ensure_ascii=False,
                )
            )
            > 80000
        ):
            raise ValueError("本轮 Demo 的专题输入超过80000字，请选择更小主题")
        article = ask(
            model,
            "summary",
            Article,
            {"knowledge": compact, "drafts": drafts, "conflicts": conflicts},
            validate,
        )
        saved = dict(
            id=identifier("summary"), topic_id=topic_id, **article, evidence=evidence
        )
        self.store.update(
            run["id"],
            summaries=[*run["summaries"], saved],
            pending_topic=None,
            status="succeeded",
            stage="complete",
        )

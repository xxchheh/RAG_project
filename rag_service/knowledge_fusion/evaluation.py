from collections import Counter
from itertools import combinations
from ..store import StoreError
from .schema import RelationType
from typing import get_args


def annotations(run, claim_ids=None):
    claims = run["claims"]
    by_id = {c["id"]: c for c in claims}
    if claim_ids:
        if len(claim_ids) > 30 or not set(claim_ids) <= set(by_id):
            raise StoreError("全配对标注请选择至多30条有效 Claim")
        pairs = [
            (a, b)
            for a, b in combinations(sorted(set(claim_ids)), 2)
            if by_id[a]["document_id"] != by_id[b]["document_id"]
        ]
    else:
        pairs = [(p["claim_a"], p["claim_b"]) for p in run["candidates"]]
    predictions = {
        tuple(sorted((r["claim_a"], r["claim_b"]))): r["relation"]
        for r in run["relations"]
    }
    labels = {
        tuple(sorted((r["claim_a"], r["claim_b"]))): r["relation"]
        for r in run["labels"]
    }
    return [
        dict(
            claim_a=a,
            claim_b=b,
            statement_a=by_id[a]["statement"],
            statement_b=by_id[b]["statement"],
            document_a=by_id[a]["document_id"],
            document_b=by_id[b]["document_id"],
            prediction=predictions.get((a, b)),
            relation=labels.get((a, b)),
        )
        for a, b in pairs
    ]


def validate_labels(run, labels):
    ids = {c["id"] for c in run["claims"]}
    output = {}
    for row in labels:
        a, b, relation = row["claim_a"], row["claim_b"], row["relation"]
        if a == b or not {a, b} <= ids or relation not in get_args(RelationType):
            raise StoreError("标注包含无效 Claim 或关系")
        key = tuple(sorted((a, b)))
        if key in output and output[key]["relation"] != relation:
            raise StoreError("同一配对的标注相互矛盾")
        output[key] = dict(claim_a=key[0], claim_b=key[1], relation=relation)
    return list(output.values())


def evaluate(run):
    predictions = {
        tuple(sorted((r["claim_a"], r["claim_b"]))): r["relation"]
        for r in run["relations"]
    }
    tp = fp = fn = 0
    for label in run["labels"]:
        predicted = (
            predictions.get(tuple(sorted((label["claim_a"], label["claim_b"]))))
            == "equivalent"
        )
        actual = label["relation"] == "equivalent"
        tp += int(predicted and actual)
        fp += int(predicted and not actual)
        fn += int(not predicted and actual)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    spans = {s["id"]: s for s in run["spans"]}
    claims = {c["id"]: c for c in run["claims"]}

    def supported(c):
        return bool(c["evidence"]) and all(
            e["source_span_id"] in spans
            and e["quote"] in spans[e["source_span_id"]]["text"]
            for e in c["evidence"]
        )

    valid = sum(
        bool(k["claim_ids"])
        and all(i in claims and supported(claims[i]) for i in k["claim_ids"])
        for k in run["knowledge"]
    )
    counts = Counter(r["relation"] for r in run["relations"])
    return dict(
        documents=len(run["documents"]),
        raw_claims=len(claims),
        candidate_pairs=len(run["candidates"]),
        relation_counts={r: counts[r] for r in get_args(RelationType)},
        merged_groups=sum(len(k["claim_ids"]) > 1 for k in run["knowledge"]),
        merged_claim_reduction=len(claims) - len(run["knowledge"]),
        canonical_knowledge=len(run["knowledge"]),
        traceable_ratio=valid / len(run["knowledge"]) if run["knowledge"] else None,
        labeled_pairs=len(run["labels"]),
        true_positive=tp,
        false_positive=fp,
        false_negative=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        evaluation_scope="仅已人工标注的配对；未召回的等价配对计为漏检。来源链完整率不表示语义准确率。",
    )

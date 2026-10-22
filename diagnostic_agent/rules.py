"""Deterministic rule evaluation; no eval(), model, or inferred thresholds."""
from __future__ import annotations

import math
from .common import ROOT, read_json

METRICS = {
    "velocity_rms": ("速度有效值", "mm/s"),
    "acceleration_rms": ("加速度有效值", "m/s²"),
    "acceleration_peak": ("加速度绝对峰值", "m/s²"),
    "kurtosis": ("加速度非超额峭度", "1"),
    "low_frequency_impact": ("低频冲击值", "demo_index"),
    **{f"h{i}": (f"{i}倍转频速度RMS幅值", "mm/s") for i in range(1, 10)},
    "bpf1": ("一倍叶片通过频率速度RMS幅值", "mm/s"),
    "bpf2": ("二倍叶片通过频率速度RMS幅值", "mm/s"),
}
DERIVED = {
    "crest_factor": (["acceleration_peak"], "acceleration_rms"),
    "h1_ratio": (["h1"], "velocity_rms"),
    "h2_ratio": (["h2"], "velocity_rms"),
    "even_ratio": (["h2", "h4", "h6", "h8"], "velocity_rms"),
    "third_ratio": (["h3", "h6", "h9"], "velocity_rms"),
    "bpf_ratio": (["bpf1", "bpf2"], "velocity_rms"),
}


def load_rules(store=None, device_type=None):
    # Local import keeps the executable metric registry independent of storage.
    if store is None:
        from .knowledge import KnowledgeStore
        store = KnowledgeStore()
    return store.snapshot(device_type=device_type)


def metric_value(features, key):
    if key in DERIVED:
        numerators, denominator = DERIVED[key]
        values = [metric_value(features, k) for k in numerators + [denominator]]
        issues = [issue for _, errs in values for issue in errs]
        if issues:
            return None, list(dict.fromkeys(issues))
        if values[-1][0] <= 0:
            return None, [f"{denominator} 必须大于零，无法计算 {key}"]
        result = math.hypot(*(v for v, _ in values[:-1])) / values[-1][0]
        return (result, []) if math.isfinite(result) else (None, [f"{key} 计算溢出"])
    item = features.get(key)
    if not isinstance(item, dict) or item.get("value") is None:
        return None, [f"缺少指标 {key}"]
    value = item["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None, [f"{key} 数值无效"]
    if item.get("unit") != METRICS[key][1]:
        return None, [f"{key} 单位应为 {METRICS[key][1]}，实际为 {item.get('unit')}"]
    return float(value), []


def evaluate_condition(node, features):
    if "all" in node or "any" in node:
        operator = "all" if "all" in node else "any"
        children = [evaluate_condition(c, features) for c in node[operator]]
        states = [c["satisfied"] for c in children]
        # Three-valued logic: false AND unknown = false; true OR unknown = true.
        if operator == "all":
            state = False if False in states else (None if None in states else True)
        else:
            state = True if True in states else (None if None in states else False)
        return {"operator": operator, "satisfied": state, "children": children}
    actual, issues = metric_value(features, node["metric"])
    state = None
    if actual is not None:
        state = all({"ge": actual >= limit, "gt": actual > limit,
                     "le": actual <= limit, "lt": actual < limit}[op]
                    for op, limit in node.items() if op in ("ge", "gt", "le", "lt"))
    return {"metric": node["metric"], "label": node["label"], "actual": actual,
            "condition": {k: v for k, v in node.items() if k in ("ge", "gt", "le", "lt")},
            "satisfied": state, "issues": issues}


def leaves(result):
    if "children" in result:
        return [leaf for child in result["children"] for leaf in leaves(child)]
    return [result]


def analyze(features, device_type, rule_snapshot=None):
    config = rule_snapshot if rule_snapshot is not None else load_rules(device_type=device_type)
    results = []
    for rule in config["rules"]:
        if rule["device_type"] != device_type:
            continue
        evidence = evaluate_condition(rule["condition"], features)
        state = evidence["satisfied"]
        results.append({**rule, "status": "matched" if state is True else "not_matched" if state is False else "insufficient_data",
                        "evidence": evidence})
    matches = [r for r in results if r["status"] == "matched"]
    issues = list(dict.fromkeys(issue for r in results for leaf in leaves(r["evidence"]) for issue in leaf["issues"]))
    status = "suspected_fault" if matches else "insufficient_data" if issues else "no_rule_match"
    if not results:
        status = "unsupported_device"
    summary = "命中演示规则，疑似：" + "、".join(r["name"] for r in matches) if matches else {
        "insufficient_data": "数据不足，无法完整判断。",
        "no_rule_match": "未命中现有规则，不能据此认定设备正常。",
        "unsupported_device": "没有适用规则，无法判断。",
    }[status]
    return {"status": status, "summary": summary, "rules": results, "issues": issues,
            "matched_rule_ids": [r["id"] for r in matches],
            "rule_version": config["version"], "conventions": config["conventions"], "source": config["source"],
            "rule_snapshot_id": config.get('snapshot_id', config['version']),
            "rule_revisions": {r['id']: r.get('revision', 1) for r in config['rules'] if r['device_type'] == device_type},
            "rule_sources": config.get('sources', [])}

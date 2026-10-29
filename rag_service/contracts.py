"""Validate executable knowledge without evaluating user supplied code."""
from __future__ import annotations

import copy
import math
import re

class DemoError(ValueError):
    """Invalid knowledge contract."""

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


DEVICE_TYPES = {'motor', 'pump', 'fan', 'coupling', 'gearbox'}
DERIVED_LABELS = {
    'crest_factor': '加速度绝对峰值 / 加速度有效值',
    'h1_ratio': '一倍转频幅值 / 速度有效值',
    'h2_ratio': '二倍转频幅值 / 速度有效值',
    'even_ratio': 'sqrt(A2²+A4²+A6²+A8²) / 速度有效值',
    'third_ratio': 'sqrt(A3²+A6²+A9²) / 速度有效值',
    'bpf_ratio': 'sqrt(BPF1²+BPF2²) / 速度有效值',
}


def condition_label(node):
    metric = node['metric']
    label = METRICS[metric][0] if metric in METRICS else DERIVED_LABELS[metric]
    symbols = {'ge': '≥', 'gt': '>', 'le': '≤', 'lt': '<'}
    parts = [f'{label} {symbols[key]} {node[key]:g}' for key in ('ge', 'gt', 'le', 'lt') if key in node]
    unit = METRICS[metric][1] if metric in METRICS else '1'
    return ' 且 '.join(parts) + (f' {unit}' if unit not in ('1', 'demo_index') else '')


def normalize_condition(condition):
    count = 0

    def visit(node, depth):
        nonlocal count
        count += 1
        if count > 200 or depth > 12 or not isinstance(node, dict):
            raise DemoError('规则条件结构无效或过于复杂')
        if 'all' in node or 'any' in node:
            if set(node) not in ({'all'}, {'any'}):
                raise DemoError('条件组只能含一个all或any字段')
            operator = next(iter(node))
            if not isinstance(node[operator], list) or not node[operator]:
                raise DemoError('all/any条件组不能为空')
            return {operator: [visit(child, depth+1) for child in node[operator]]}
        if set(node) - {'metric', 'label', 'ge', 'gt', 'le', 'lt'}:
            raise DemoError('规则包含不支持的条件字段或运算符')
        metric = node.get('metric')
        if not isinstance(metric, str) or metric not in METRICS.keys() | DERIVED.keys():
            raise DemoError('规则包含不支持的指标：' + str(metric))
        limits = {k: v for k, v in node.items() if k in ('ge', 'gt', 'le', 'lt')}
        if not limits or {'ge', 'gt'} <= limits.keys() or {'le', 'lt'} <= limits.keys():
            raise DemoError('规则缺少比较条件，或同侧边界重复')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in limits.values()):
            raise DemoError('规则阈值必须是有限数值')
        lower = limits.get('ge', limits.get('gt'))
        upper = limits.get('le', limits.get('lt'))
        if lower is not None and upper is not None and (lower > upper or (lower == upper and ('gt' in limits or 'lt' in limits))):
            raise DemoError('规则上下限矛盾，区间为空')
        result = {'metric': metric, **limits}
        result['label'] = condition_label(result)
        return result

    return visit(condition, 0)


def normalize_rule(rule):
    if not isinstance(rule, dict):
        raise DemoError('每条规则必须为JSON对象')
    result = copy.deepcopy(rule)
    if 'id' in result and (not isinstance(result['id'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,63}', result['id'])):
        raise DemoError('规则编号格式无效')
    if result.get('device_type') not in DEVICE_TYPES:
        raise DemoError('规则设备类型必须为motor、pump、fan、coupling或gearbox')
    for key in ('name', 'original_text'):
        if not isinstance(result.get(key), str) or not result[key].strip() or len(result[key]) > 50000:
            raise DemoError(f'规则缺少有效的{key}')
        result[key] = result[key].strip()
    result['condition'] = normalize_condition(result.get('condition'))
    for key in ('phenomena', 'causes', 'actions', 'notes', 'review_notes'):
        value = result.get(key, [])
        if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, str) or not v.strip() or len(v) > 10000 for v in value):
            raise DemoError(f'{key}必须为非空字符串组成的列表')
        result[key] = [v.strip() for v in value]
    result.setdefault('source_section', result['name'])
    if not isinstance(result['source_section'], str):
        raise DemoError('规则来源章节无效')
    if 'source' in result and not isinstance(result['source'], dict):
        raise DemoError('规则来源必须为JSON对象')
    return result

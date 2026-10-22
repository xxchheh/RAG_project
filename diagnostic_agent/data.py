"""Read-only sample adapter. Replace this boundary to connect a real system."""
from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime, timedelta

from .common import DemoError, TZ
from .rules import METRICS, load_rules

DEMO_DATE = date(2026, 10, 20)
TYPES = {"motor": "电机", "coupling": "联轴器", "pump": "水泵", "fan": "风机", "gearbox": "齿轮箱"}


def sample_features(kind):
    values = {"velocity_rms": 3.2, "acceleration_rms": 2., "acceleration_peak": 6.,
              "kurtosis": 2.7, "low_frequency_impact": 4.,
              **{f"h{i}": .06 for i in range(1, 10)}, "bpf1": .2, "bpf2": .1}
    values.update(h1=.7, h2=.3)
    if kind == "bearing":
        values.update(velocity_rms=5., kurtosis=4., low_frequency_impact=10., acceleration_peak=10.)
    elif kind == "imbalance":
        values.update(h1=2.2, h2=.15, h3=.55, h6=.55, h9=.55)
    elif kind == "misalignment":
        values.update(h2=1.2)
    elif kind == "wear":
        values.update(kurtosis=3.3)
    elif kind == "cavitation":
        values.update(bpf1=1.8, bpf2=.8, h3=.55, h6=.55, h9=.55)
    elif kind == "not_matched":
        values.update(velocity_rms=2.)
    elif kind == "multi":
        values.update(h1=2.2, h2=.15, kurtosis=3.2)
    elif kind == "out_of_range":
        values.update(velocity_rms=7.2)
    elif kind == "zero":
        values.update(velocity_rms=0., acceleration_rms=0., acceleration_peak=0.)
    if kind == "missing":
        return {}
    result = {key: {"value": value, "unit": METRICS[key][1], "source": "模拟上游特征值"} for key, value in values.items()}
    if kind == "wrong_unit":
        result["acceleration_rms"]["unit"] = "mm/s"
    return result


def rule_kind(name):
    return next(kind for suffix, kind in [("故障", "bearing"), ("不平衡", "imbalance"), ("不对中", "misalignment"),
                                        ("松动", "loose"), ("磨损", "wear"), ("汽蚀", "cavitation")] if name.endswith(suffix))


def sample_catalog():
    devices = []
    for index, rule in enumerate(load_rules()["rules"]):
        dtype = rule["device_type"]
        kind = rule_kind(rule["name"])
        did = "motor-16" if index == 0 else f"{dtype}-{index+16}"
        name = "行走电机" if index == 0 else f"{TYPES[dtype]}{index+16}号"
        devices.append({"id": did, "name": name, "device_type": dtype,
                        "location": f"知源智能/演示工厂/10号产线/{index+16}号机器",
                        "aliases": [f"{index+16}号机器{TYPES[dtype]}", f"{rule['name']}样例"],
                        "points": [{"id": "DE-H", "name": "驱动端", "direction": "水平径向", "scenario": kind}],
                        "target_rule_id": rule["id"], "is_simulated": True})
    for index, kind in enumerate(["not_matched", "missing", "multi", "out_of_range", "zero", "wrong_unit"]):
        devices.append({"id": f"motor-edge-{kind}", "name": f"边界电机{index+1}", "device_type": "motor",
                        "location": "演示工厂/边界场景", "aliases": [kind],
                        "points": [{"id": "DE-H", "name": "驱动端", "direction": "水平径向", "scenario": kind}],
                        "is_simulated": True})
    # Two identical names make clarification behavior observable.
    for n in (1, 2):
        devices.append({"id": f"pump-duplicate-{n}", "name": "循环水泵", "device_type": "pump",
                        "location": f"演示工厂/{n}号车间", "aliases": [f"{n}号车间循环水泵"],
                        "points": [{"id": "DE-H", "name": "驱动端", "direction": "水平径向", "scenario": "bearing"},
                                   {"id": "NDE-V", "name": "非驱动端", "direction": "垂直径向", "scenario": "missing"}],
                        "is_simulated": True})
    return devices


def _normalize(text):
    return re.sub(r"[\s/\-_，。]+", "", text.lower())


def public_device(device):
    result = {k: copy.deepcopy(v) for k, v in device.items() if k != 'target_rule_id'}
    for point in result['points']:
        point.pop('scenario', None)
    return result


def search_devices(query):
    tokens = [_normalize(t) for t in re.split(r"[\s/,，]+", query.strip()) if t]
    candidates = []
    for device in sample_catalog():
        haystack = _normalize(" ".join([device["id"], device["name"], device["location"], TYPES[device["device_type"]], *device["aliases"]]))
        if all(token in haystack for token in tokens):
            candidates.append(public_device(device))
    return {"candidates": candidates, "count": len(candidates), "demo_clock": DEMO_DATE.isoformat(),
            "is_simulated": True, "needs_clarification": len(candidates) > 1}


def make_history(kind):
    latest = sample_features(kind)
    history = []
    for day in range(90):
        features = copy.deepcopy(latest)
        # Fixed upstream-style features; plots are illustrations, not their computational source.
        for key in ("velocity_rms", "acceleration_rms", "acceleration_peak"):
            if key in features:
                features[key]["value"] = round(features[key]["value"] * (.6 + .4 * day / 89), 6)
        history.append({"date": (DEMO_DATE - timedelta(days=89-day)).isoformat(), "features": features})
    return history


def date_value(value):
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TZ).date() if parsed.tzinfo is None else parsed.astimezone(TZ).date()
    except (ValueError, TypeError, AttributeError):
        raise DemoError("日期必须为 YYYY-MM-DD 或 ISO8601 时间") from None


def get_data(device_id, point_id=None, start_date=None, end_date=None, days=30):
    device = next((d for d in sample_catalog() if d["id"] == device_id), None)
    if not device:
        raise DemoError("未知设备，请先调用 search_devices 查询设备 ID")
    points = device["points"]
    if point_id is None:
        if len(points) != 1:
            raise DemoError("存在多个测点，请让用户选择 point_id：" + ", ".join(p["id"] for p in points))
        point_id = points[0]["id"]
    point = next((p for p in points if p["id"] == point_id), None)
    if point is None:
        raise DemoError("该设备没有指定测点")
    if start_date and not end_date:
        end = DEMO_DATE
    else:
        end = date_value(end_date) if end_date else DEMO_DATE
    start = date_value(start_date) if start_date else end - timedelta(days=days-1)
    if start > end:
        raise DemoError("开始日期不能晚于结束日期")
    full_history = make_history(point["scenario"])
    history = [item for item in full_history if start <= date.fromisoformat(item["date"]) <= end]
    if not history:
        raise DemoError(f"指定时间范围没有数据；样本可用范围 {full_history[0]['date']} 至 {full_history[-1]['date']}")
    current = history[-1]
    previous_start = start - timedelta(days=(end-start).days+1)
    previous = [item for item in full_history if previous_start <= date.fromisoformat(item["date"]) < start]
    plot = None
    # The retained segment has its own explicit timestamp. Never reuse it for an earlier query.
    if current["date"] == DEMO_DATE.isoformat() and point["scenario"] != "missing":
        fs = 6400
        waveform = [round(1.5*math.sin(2*math.pi*25*i/fs) + .25*math.sin(2*math.pi*50*i/fs), 6) for i in range(1280)]
        plot = {"date": current["date"], "sample_rate_hz": fs, "unit": "m/s²", "values": waveform,
                "spectrum": [{"frequency_hz": i*25, "value": a} for i, a in enumerate([0, 1.5, .25, 0, 0])],
                "spectrum_unit": "m/s² peak", "note": "独立模拟加速度图谱片段，不作为上游特征值或速度倍频的计算来源。"}
    return {"device": device, "point": point, "is_simulated": True,
            "demo_clock": DEMO_DATE.isoformat(), "requested_range": {"start": start.isoformat(), "end": end.isoformat()},
            "available_range": {"start": full_history[0]["date"], "end": full_history[-1]["date"]},
            "actual_range": {"start": history[0]["date"], "end": current["date"]},
            "features": current["features"], "history": history, "previous_history": previous,
            "previous_requested_range": {"start": previous_start.isoformat(), "end": (start-timedelta(days=1)).isoformat()},
            "plots": plot, "operating_conditions": {"actual_speed_rpm": 1500, "load": "固定模拟负载", "blade_count": 6}}

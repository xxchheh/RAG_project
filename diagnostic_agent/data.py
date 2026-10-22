"""Read-only sample adapter. Replace this boundary to connect a real system."""
from __future__ import annotations

import copy
import json
import math
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .common import DemoError, ROOT, TZ
from .rules import METRICS

DEMO_DATE = date(2026, 10, 20)
TYPES = {"motor": "电机", "coupling": "联轴器", "pump": "水泵", "fan": "风机", "gearbox": "齿轮箱"}
SCENARIOS = {"bearing", "imbalance", "misalignment", "loose", "wear", "cavitation",
             "not_matched", "missing", "multi", "out_of_range", "zero", "wrong_unit"}


def sample_features(kind):
    if kind not in SCENARIOS:
        raise DemoError(f"未知模拟场景：{kind}")
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


def sample_catalog(path=None):
    """Load the independent device catalog afresh so additions/deletions take effect immediately.

    ``target_rule_id`` is retained only as test-fixture metadata; it does not select rules.
    A real-system adapter can replace this boundary without changing the rule repository.
    """
    catalog_path = Path(path or os.environ.get("DIAGNOSTIC_DEVICE_CATALOG") or ROOT / "config" / "devices.json")
    try:
        devices = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemoError(f"无法读取设备目录 {catalog_path}：{exc}") from None
    if not isinstance(devices, list):
        raise DemoError("设备目录必须是设备列表")
    device_ids = set()
    for index, device in enumerate(devices):
        label = f"设备目录第 {index + 1} 项"
        if not isinstance(device, dict):
            raise DemoError(f"{label}必须是对象")
        for key in ("id", "name", "device_type", "location"):
            if not isinstance(device.get(key), str) or not device[key].strip():
                raise DemoError(f"{label}的 {key} 必须是非空字符串")
        if device["id"] in device_ids:
            raise DemoError(f"设备目录存在重复设备 ID：{device['id']}")
        device_ids.add(device["id"])
        if device["device_type"] not in TYPES:
            raise DemoError(f"设备 {device['id']} 使用未知设备类型：{device['device_type']}")
        aliases = device.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise DemoError(f"设备 {device['id']} 的 aliases 必须是字符串列表")
        device["aliases"] = aliases
        points = device.get("points")
        if not isinstance(points, list) or not points:
            raise DemoError(f"设备 {device['id']} 的 points 必须是非空测点列表")
        point_ids = set()
        for point in points:
            if not isinstance(point, dict):
                raise DemoError(f"设备 {device['id']} 的测点必须是对象")
            for key in ("id", "name", "direction", "scenario"):
                if not isinstance(point.get(key), str) or not point[key].strip():
                    raise DemoError(f"设备 {device['id']} 的测点 {key} 必须是非空字符串")
            if point["id"] in point_ids:
                raise DemoError(f"设备 {device['id']} 存在重复测点 ID：{point['id']}")
            point_ids.add(point["id"])
            if point["scenario"] not in SCENARIOS:
                raise DemoError(f"设备 {device['id']} 测点 {point['id']} 使用未知模拟场景：{point['scenario']}")
    return copy.deepcopy(devices)


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

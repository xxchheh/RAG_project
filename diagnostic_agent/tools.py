from __future__ import annotations

import copy
import math
from pathlib import Path
from uuid import uuid4

from .common import DemoError, now, write_json
from .data import get_data, search_devices, public_device, date_value
from .rules import analyze, metric_value, METRICS


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


STRING = {'type': 'string', 'minLength': 1}
TOOL_SCHEMAS = [
    schema('search_devices', '按设备ID、名称、设备类型或位置关键词查询；用简短关键词，多个词以空格分隔。空字符串列出全部；多个候选须请用户选择。',
           {'query': {'type': 'string'}}, ['query']),
    schema('get_device_data', '读取一个设备一个测点的模拟数据。日期为YYYY-MM-DD。省略日期时以样本最新日期为基准；明确日期不改写；多个测点须指定。',
           {'device_id': STRING, 'point_id': STRING, 'start_date': STRING, 'end_date': STRING,
            'days': {'type': 'integer', 'minimum': 1, 'maximum': 366}}, ['device_id']),
    schema('analyze_device', '执行机理规则并计算趋势、前一等长窗口对比。仅接收此前get_device_data返回的dataset_id。',
           {'dataset_id': STRING}, ['dataset_id']),
    schema('generate_report', '使用已有analysis_id生成Word报告和分析JSON；只有返回成功文件路径才算生成报告。',
           {'analysis_id': STRING}, ['analysis_id']),
]


def validate_arguments(name, args):
    spec = next((s['function']['parameters'] for s in TOOL_SCHEMAS if s['function']['name'] == name), None)
    if spec is None:
        raise DemoError('未知工具名称')
    if not isinstance(args, dict):
        raise DemoError('工具参数必须是 JSON 对象')
    if set(args) - set(spec['properties']):
        raise DemoError('工具包含未定义参数')
    if set(spec['required']) - set(args):
        raise DemoError('工具缺少必要参数：' + ', '.join(set(spec['required']) - set(args)))
    for key, value in args.items():
        rule = spec['properties'][key]
        if rule['type'] == 'string' and (not isinstance(value, str) or len(value.strip()) < rule.get('minLength', 0) or len(value) > 1000):
            raise DemoError(f'{key} 必须为有效字符串')
        if rule['type'] == 'integer' and (isinstance(value, bool) or not isinstance(value, int) or not rule['minimum'] <= value <= rule['maximum']):
            raise DemoError(f'{key} 必须为 {rule["minimum"]} 到 {rule["maximum"]} 的整数')


def numeric_series(history, key):
    result = []
    for entry in history:
        value, _ = metric_value(entry['features'], key)
        if value is not None:
            result.append((entry['date'], value))
    return result


def compute_trends(dataset):
    result = {}
    expected_count = (date_value(dataset['requested_range']['end']) - date_value(dataset['requested_range']['start'])).days + 1
    for key in ('velocity_rms', 'acceleration_rms', 'kurtosis'):
        series = numeric_series(dataset['history'], key)
        previous = numeric_series(dataset['previous_history'], key)
        if not series:
            continue
        first, current = series[0][1], series[-1][1]
        avg = math.fsum(value for _, value in series) / len(series)
        prev = math.fsum(value for _, value in previous) / len(previous) if previous else None
        result[key] = {'label': METRICS[key][0], 'unit': METRICS[key][1], 'first': first, 'current': current,
                       'change_percent': round((current-first)/first*100, 4) if first else None,
                       'mean': avg, 'previous_mean': prev,
                       'previous_change_percent': round((avg-prev)/prev*100, 4) if prev else None,
                       'sample_count': len(series), 'previous_sample_count': len(previous),
                       'previous_coverage_complete': len(previous) == expected_count,
                       'series': series}
    return result


def scan_window(dataset):
    """Keep past rule hits even if the latest snapshot no longer matches."""
    hits = {}
    for entry in dataset['history']:
        result = analyze(entry['features'], dataset['device']['device_type'])
        for rule in result['rules']:
            if rule['status'] != 'matched':
                continue
            if rule['id'] not in hits:
                hits[rule['id']] = {'rule_id': rule['id'], 'name': rule['name'], 'dates': [],
                                    'first_evidence': rule['evidence']}
            hits[rule['id']]['dates'].append(entry['date'])
            hits[rule['id']]['last_evidence'] = rule['evidence']
    return [{**item, 'sample_count': len(item['dates']), 'first_date': item['dates'][0], 'last_date': item['dates'][-1]}
            for item in hits.values()]


class ToolRuntime:
    def __init__(self, output_dir='outputs', model=None):
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.datasets = {}
        self.analyses = {}
        self.reports = []
        self.trace_path = self.output_dir / 'tool-calls.jsonl'

    def clear(self):
        self.datasets.clear()
        self.analyses.clear()
        self.reports.clear()

    def call(self, name, arguments):
        try:
            validate_arguments(name, arguments)
            result = getattr(self, name)(**arguments)
            envelope = {'ok': True, 'result': result}
        except (DemoError, ValueError) as exc:
            envelope = {'ok': False, 'error': str(exc)}
        except Exception:
            envelope = {'ok': False, 'error': '工具执行失败，未完成本次操作；请检查本地运行环境或重试。'}
        import json
        with self.trace_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': now(), 'tool': name, 'arguments': arguments, **envelope}, ensure_ascii=False, allow_nan=False) + '\n')
        return envelope

    def search_devices(self, query):
        return search_devices(query)

    def get_device_data(self, **kwargs):
        data = get_data(**kwargs)
        key = 'data-' + uuid4().hex[:12]
        self.datasets[key] = copy.deepcopy(data)
        missing = [key for key in ('kurtosis', 'acceleration_rms', 'acceleration_peak', 'low_frequency_impact', 'velocity_rms') if key not in data['features']]
        return {'dataset_id': key, **{k: data[k] for k in ('is_simulated', 'demo_clock', 'requested_range', 'actual_range', 'available_range', 'features', 'operating_conditions')},
                'device': public_device(data['device']), 'point': {k: v for k, v in data['point'].items() if k != 'scenario'},
                'sample_count': len(data['history']), 'missing_metrics': missing,
                'plot_available': data['plots'] is not None,
                'note': '所有数据为模拟。默认最近30天，以演示时钟为基准。图谱为独立模拟片段。'}

    def analyze_device(self, dataset_id):
        if dataset_id not in self.datasets:
            raise DemoError('dataset_id 不存在或会话已清空，请重新获取数据')
        dataset = copy.deepcopy(self.datasets[dataset_id])
        result = analyze(dataset['features'], dataset['device']['device_type'])
        key = 'analysis-' + uuid4().hex[:12]
        result.update(analysis_id=key, dataset_id=dataset_id, analyzed_at=now(), is_simulated=True,
                      device=dataset['device'], point=dataset['point'], actual_range=dataset['actual_range'],
                      requested_range=dataset['requested_range'], trends=compute_trends(dataset),
                      previous_requested_range=dataset['previous_requested_range'],
                      latest_sample_date=dataset['history'][-1]['date'], window_rule_hits=scan_window(dataset))
        self.analyses[key] = result
        # Exclude full trend series from model context while keeping exact calculated summaries.
        output = copy.deepcopy(result)
        output['device'] = public_device(output['device'])
        output['point'].pop('scenario', None)
        for trend in output['trends'].values():
            trend.pop('series', None)
        return output

    def generate_report(self, analysis_id):
        if analysis_id not in self.analyses:
            raise DemoError('analysis_id 不存在或会话已清空，请先分析数据')
        from .report import generate_report
        analysis = copy.deepcopy(self.analyses[analysis_id])
        dataset = copy.deepcopy(self.datasets[analysis['dataset_id']])
        result = generate_report(analysis, dataset, self.output_dir, model=self.model)
        self.reports.append(result)
        return result

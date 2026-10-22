from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .agent import Agent
from .common import DemoError, write_json
from .data import DEMO_DATE, TYPES, sample_catalog
from .model import ChatModel, ModelConfig
from .tools import ToolRuntime

PROGRESS = {'search_devices': '查找设备', 'get_device_data': '读取模拟数据',
            'analyze_device': '执行机理分析', 'generate_report': '生成并校验 Word 报告'}


def build_agent(output):
    model = ChatModel(ModelConfig.from_env())
    runtime = ToolRuntime(output, model=model)
    return Agent(model, runtime, progress=lambda name: print('  → ' + PROGRESS.get(name, '检查工具调用'), flush=True))


def offline_demo(output, device_id, point_id=None):
    runtime = ToolRuntime(output)
    data = runtime.call('get_device_data', {'device_id': device_id, **({'point_id': point_id} if point_id else {})})
    if not data['ok']:
        raise DemoError(data['error'])
    analysis = runtime.call('analyze_device', {'dataset_id': data['result']['dataset_id']})
    if not analysis['ok']:
        raise DemoError(analysis['error'])
    report = runtime.call('generate_report', {'analysis_id': analysis['result']['analysis_id']})
    if not report['ok']:
        raise DemoError(report['error'])
    print('离线工具演示，非真实模型 Agent 验收。')
    print(report['result']['summary'])
    print(report['result']['report_path'])


def live_smoke(output):
    agent = build_agent(output)
    root = agent.runtime.output_dir
    results = []
    prompts = [
        ('设备歧义澄清', '请分析循环水泵，生成报告。'),
        ('一句话生成报告', '改为分析16号机器的行走电机驱动端最近一个月的数据，生成Word报告。'),
        ('复用证据追问', '为什么认为可能是轴承问题？请引用规则编号和实际值。'),
        ('切换设备', '换成pump-25这台水泵的驱动端，分析最近30天并生成报告。'),
    ]
    for title, prompt in prompts:
        before = (len(agent.runtime.datasets), len(agent.runtime.analyses), len(agent.runtime.reports))
        reply = agent.turn(prompt)
        after = (len(agent.runtime.datasets), len(agent.runtime.analyses), len(agent.runtime.reports))
        print(f'\n[{title}]\n{reply}', flush=True)
        if title == '设备歧义澄清':
            passed = after == before and ('？' in reply or '?' in reply or '选择' in reply or '哪' in reply)
        elif title == '复用证据追问':
            passed = after == before and 'R001' in reply and ('4' in reply or '5' in reply) and '任务未完成' not in reply
        else:
            passed = after[0] > before[0] and after[1] > before[1] and after[2] > before[2]
            if passed:
                latest = list(agent.runtime.datasets.values())[-1]
                passed = len(latest['history']) == 30 and latest['actual_range']['start'] == '2026-09-21'
        results.append({'scenario': title, 'passed': passed, 'prompt': prompt, 'reply': reply})
    record = {'real_api': True, 'provider': agent.model.config.provider, 'model': agent.model.config.model,
              'all_passed': all(r['passed'] for r in results), 'scenarios': results,
              'reports': agent.runtime.reports}
    write_json(root / 'live-acceptance.json', record)
    write_json(root / 'conversation.json', agent.messages)
    if not record['all_passed']:
        raise DemoError('真实 API 验收部分场景未通过，详情见 live-acceptance.json')
    print('\n真实 API 验收通过：' + str(root / 'live-acceptance.json'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='设备诊断 Agent Demo（全部数据均为模拟）')
    sub = parser.add_subparsers(dest='command', required=True)
    chat = sub.add_parser('chat', help='启动真实模型终端连续对话')
    chat.add_argument('--output', default='outputs/chat-' + uuid4().hex[:8])
    ask = sub.add_parser('ask', help='用真实模型完成一条自然语言请求')
    ask.add_argument('prompt')
    ask.add_argument('--output', default='outputs/ask-' + uuid4().hex[:8])
    demo = sub.add_parser('demo', help='离线调用工具并生成报告，不调用模型')
    demo.add_argument('--device-id', default='motor-16')
    demo.add_argument('--point-id')
    demo.add_argument('--output', default='outputs/offline')
    sub.add_parser('devices', help='查看模拟设备与测点')
    sub.add_parser('check-config', help='检查本地配置，不输出密钥，不调用模型')
    smoke = sub.add_parser('smoke', help='调用真实API完成歧义澄清、报告、追问和切换设备验收')
    smoke.add_argument('--output', default='outputs/live-' + uuid4().hex[:8])
    args = parser.parse_args(argv)
    try:
        if args.command == 'devices':
            for d in sample_catalog():
                print(d['id'], TYPES[d['device_type']], d['name'], d['location'], ','.join(p['id'] for p in d['points']))
        elif args.command == 'check-config':
            config = ModelConfig.from_env()
            print(f'配置可用；厂商={config.provider}；模型={config.model}；密钥已配置（已隐藏）。尚未验证 API 连通性。')
        elif args.command == 'demo':
            offline_demo(args.output, args.device_id, args.point_id)
        elif args.command == 'smoke':
            live_smoke(args.output)
        else:
            agent = build_agent(args.output)
            print(f'设备诊断 Agent｜模拟数据｜演示时钟 {DEMO_DATE}｜/reset 清空，/exit 退出')
            while True:
                if args.command == 'ask':
                    text = args.prompt
                else:
                    try:
                        text = input('\n你 > ').strip()
                    except (EOFError, KeyboardInterrupt):
                        print('\n会话结束。')
                        break
                if not text:
                    continue
                if text.lower() in ('/exit', 'exit', 'quit'):
                    break
                if text.lower() == '/reset':
                    agent.reset()
                    write_json(agent.runtime.output_dir / 'conversation.json', agent.messages)
                    print('已清空会话与数据引用。')
                    continue
                reply = agent.turn(text)
                print('\nAgent > ' + reply)
                write_json(agent.runtime.output_dir / 'conversation.json', agent.messages)
                if args.command == 'ask':
                    return 1 if reply.startswith('任务未完成') else 0
    except (DemoError, OSError) as error:
        print(f'未完成：{error}', file=sys.stderr)
        return 1
    return 0

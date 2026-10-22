import json
import tempfile
import unittest

from diagnostic_agent.agent import Agent
from diagnostic_agent.common import DemoError
from diagnostic_agent.tools import ToolRuntime


def tool(name, args, identifier='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class SequenceModel:
    def __init__(self, responses):
        self.responses = iter(responses)
    def complete(self, messages, tools=None):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response(messages) if callable(response) else response


class AgentTests(unittest.TestCase):
    def test_unknown_tool_and_bad_json_can_recover(self):
        bad = tool('search_devices', {})
        bad['tool_calls'][0]['function']['arguments'] = '{'
        model = SequenceModel([bad, tool('unknown', {}, 'c2'), {'role': 'assistant', 'content': '请补充设备名称？'}])
        with tempfile.TemporaryDirectory() as path:
            agent = Agent(model, ToolRuntime(path))
            self.assertIn('设备名称', agent.turn('分析设备'))
            replies = [json.loads(m['content']) for m in agent.messages if m['role'] == 'tool']
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(not r['ok'] for r in replies))

    def test_protocol_handles_multiple_tools(self):
        calls = tool('search_devices', {'query': '不存在'})
        calls['tool_calls'].extend(tool('search_devices', {'query': '循环水泵'}, 'c2')['tool_calls'])
        model = SequenceModel([calls, {'role': 'assistant', 'content': '请选择哪台设备？'}])
        with tempfile.TemporaryDirectory() as path:
            agent = Agent(model, ToolRuntime(path))
            agent.turn('分析')
            self.assertEqual([m['tool_call_id'] for m in agent.messages if m['role'] == 'tool'], ['c1', 'c2'])

    def test_false_report_claim_rejected(self):
        model = SequenceModel([{'role': 'assistant', 'content': '报告已生成，可以下载报告。'}])
        with tempfile.TemporaryDirectory() as path:
            result = Agent(model, ToolRuntime(path)).turn('生成报告')
            self.assertIn('尚未成功', result)

    def test_timeout_and_reset_and_round_limit(self):
        with tempfile.TemporaryDirectory() as path:
            model = SequenceModel([DemoError('连接超时'), {'role': 'assistant', 'content': '请选择设备？'}])
            agent = Agent(model, ToolRuntime(path))
            self.assertIn('任务未完成', agent.turn('分析'))
            self.assertEqual(len(agent.messages), 1)
            self.assertIn('选择', agent.turn('重试'))
            agent.reset()
            self.assertEqual(len(agent.messages), 1)
            endless = SequenceModel([tool('search_devices', {'query': ''})] * 2)
            agent = Agent(endless, ToolRuntime(path), max_rounds=2)
            self.assertIn('上限', agent.turn('分析'))
            self.assertEqual(len(agent.messages), 1)

    def test_duplicate_call_ids_fail_without_executing(self):
        calls = tool('search_devices', {'query': ''})
        calls['tool_calls'] *= 2
        with tempfile.TemporaryDirectory() as path:
            agent = Agent(SequenceModel([calls]), ToolRuntime(path))
            self.assertIn('ID无效', agent.turn('分析'))

    def test_malformed_function_does_not_crash(self):
        message = tool('search_devices', {'query': ''})
        message['tool_calls'][0]['function'] = []
        with tempfile.TemporaryDirectory() as path:
            agent = Agent(SequenceModel([message]), ToolRuntime(path))
            self.assertIn('结构无效', agent.turn('分析'))

    def test_relative_dates_use_program_arithmetic(self):
        calls = tool('get_device_data', {'device_id': 'motor-16', 'start_date': '2026-09-20', 'end_date': '2026-10-20'})
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            agent = Agent(SequenceModel([calls, {'role': 'assistant', 'content': '已读取模拟数据。'}]), runtime)
            agent.turn('请分析最近30天')
            data = next(iter(runtime.datasets.values()))
            self.assertEqual(len(data['history']), 30)
            self.assertEqual(data['actual_range']['start'], '2026-09-21')

    def test_explicit_dates_preserved(self):
        calls = tool('get_device_data', {'device_id': 'motor-16', 'start_date': '2026-09-20', 'end_date': '2026-10-20'})
        with tempfile.TemporaryDirectory() as path:
            runtime = ToolRuntime(path)
            agent = Agent(SequenceModel([calls, {'role': 'assistant', 'content': '已读取模拟数据。'}]), runtime)
            agent.turn('请分析2026-09-20至2026-10-20')
            self.assertEqual(len(next(iter(runtime.datasets.values()))['history']), 31)


if __name__ == '__main__':
    unittest.main()

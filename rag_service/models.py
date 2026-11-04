"""Small Chat Completions adapter. Only the standard library is required."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .common import DemoError


def load_dotenv(path=Path('.env')):
    """Read literal KEY=VALUE pairs, never execute or expand shell syntax."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.removeprefix('export ').partition('=')
        if separator and key.strip().replace('_', '').isalnum():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


@dataclass
class ModelConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    timeout: float = 90

    @classmethod
    def from_env(cls, prefix='LLM'):
        load_dotenv()
        provider = os.environ.get(prefix+'_PROVIDER', 'deepseek').strip().lower()
        defaults = {'deepseek': 'https://api.deepseek.com/v1',
                    'qwen': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                    'openai': 'https://api.openai.com/v1'}
        if provider not in defaults:
            raise DemoError(prefix+'_PROVIDER 必须为 deepseek、qwen 或 openai')
        aliases = {'deepseek': 'DEEPSEEK_API_KEY', 'qwen': 'DASHSCOPE_API_KEY', 'openai': 'OPENAI_API_KEY'}
        key = os.environ.get(prefix+'_API_KEY') or (os.environ.get(aliases[provider], '') if prefix == 'LLM' else '')
        model = os.environ.get(prefix+'_MODEL', '')
        if not key or not model:
            raise DemoError(f'请在本地 .env 配置 {prefix}_MODEL 和 {prefix}_API_KEY；可复制 .env.example。')
        base = os.environ.get(prefix+'_BASE_URL') or defaults[provider]
        parsed = urlsplit(base)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise DemoError(prefix+'_BASE_URL 必须是无凭据、无查询参数的服务地址')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise DemoError('模型服务必须使用 HTTPS（本机测试除外）')
        try:
            timeout = float(os.environ.get(prefix+'_TIMEOUT', '90'))
            if not 1 <= timeout <= 300:
                raise ValueError()
        except ValueError:
            raise DemoError(prefix+'_TIMEOUT 必须在1到300秒之间') from None
        return cls(provider, base.rstrip('/'), model, key, timeout)


    @classmethod
    def for_answers(cls):
        """Opt-in answering config; never reuse another provider's credentials."""
        load_dotenv()
        fields = ('PROVIDER', 'BASE_URL', 'MODEL', 'API_KEY', 'TIMEOUT')
        configured = any(os.environ.get('ANSWER_LLM_'+field, '').strip() for field in fields)
        return cls.from_env('ANSWER_LLM' if configured else 'LLM')


class ChatModel:
    def __init__(self, config):
        self.config = config

    def complete(self, messages, tools=None):
        config = self.config
        body = {'model': config.model, 'messages': messages, 'stream': False}
        if tools:
            body.update(tools=tools, tool_choice='auto')
        if config.provider == 'deepseek':
            body['thinking'] = {'type': 'disabled'}
        elif config.provider == 'qwen':
            body['enable_thinking'] = False
        elif config.provider == 'openai' and config.model.startswith('gpt-5.6'):
            body['reasoning_effort'] = 'none'
        endpoint = config.base_url + '/chat/completions'
        request = Request(endpoint, data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config.api_key}, method='POST')
        try:
            with urlopen(request, timeout=config.timeout) as response:
                payload = json.loads(response.read().decode())
            message = payload['choices'][0]['message']
        except HTTPError as error:
            # Do not expose server bodies, headers or credentials through logs/tool messages.
            status = error.code
            error.close()
            raise DemoError(f'模型 API HTTP {status}；请检查服务地址、模型权限、余额和工具调用兼容性。') from None
        except (URLError, TimeoutError, OSError):
            raise DemoError('模型 API 连接失败或超时；本次任务未完成，可以重试。') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise DemoError('模型 API 返回格式异常；需要 Chat Completions choices[0].message。') from None
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            raise DemoError('模型返回了无效的 assistant 消息')
        # Preserve provider reasoning fields when supplied; required by some tool protocols.
        return message

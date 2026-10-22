import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from diagnostic_agent.common import DemoError
from diagnostic_agent.model import ChatModel, ModelConfig, load_dotenv


class ModelTests(unittest.TestCase):
    def test_literal_dotenv_and_no_environment_override(self):
        with tempfile.TemporaryDirectory() as path, patch.dict(os.environ, {'ALREADY': 'keep'}, clear=True):
            env = Path(path) / '.env'
            env.write_text('ALREADY=replace\nLITERAL="$(touch /tmp/nope)"\n# comment\n', encoding='utf-8')
            load_dotenv(env)
            self.assertEqual(os.environ['ALREADY'], 'keep')
            self.assertEqual(os.environ['LITERAL'], '$(touch /tmp/nope)')

    def test_configuration_error_does_not_expose_secret(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'invalid', 'LLM_API_KEY': 'test-secret'}, clear=True), patch('diagnostic_agent.model.load_dotenv'):
            with self.assertRaises(DemoError) as error:
                ModelConfig.from_env()
            self.assertNotIn('test-secret', str(error.exception))

    def test_http_adapter_payload_and_error_sanitization(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append(body)
                if body['model'] == 'fail':
                    self.send_response(401); self.end_headers(); self.wfile.write(b'secret-should-not-leak')
                else:
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}).encode())
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for provider in ('deepseek', 'qwen', 'openai'):
                config = ModelConfig(provider, f'http://127.0.0.1:{server.server_port}/v1', 'test-model', 'secret')
                self.assertEqual(ChatModel(config).complete([{'role': 'user', 'content': 'test'}])['content'], 'ok')
            self.assertEqual(received[0]['thinking'], {'type': 'disabled'})
            self.assertFalse(received[1]['enable_thinking'])
            config.model = 'fail'
            with self.assertRaises(DemoError) as error:
                ChatModel(config).complete([])
            self.assertIn('401', str(error.exception))
            self.assertNotIn('secret', str(error.exception))
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()

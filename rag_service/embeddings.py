"""Local and HTTP dense embedding adapters; credentials never enter index metadata."""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class EmbeddingConfig:
    mode: str = 'local'
    model: str = 'BAAI/bge-small-zh-v1.5'
    base_url: str = ''
    api_key: str = ''
    timeout: float = 90
    cache_dir: str | None = None

    def __post_init__(self):
        if self.mode not in ('local', 'api'):
            raise ValueError('EMBEDDING_MODE 必须是 local 或 api')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('请配置 EMBEDDING_MODEL')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not 1 <= self.timeout <= 300:
            raise ValueError('EMBEDDING_TIMEOUT 必须为1至300秒')
        if self.mode == 'api':
            parts = urlsplit(self.base_url)
            if (not parts.hostname or parts.username or parts.password or parts.query or parts.fragment
                    or (parts.scheme != 'https' and not (parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')))):
                raise ValueError('EMBEDDING_BASE_URL 必须是无凭据和查询参数的HTTPS地址，本机服务可用HTTP')
            if not self.api_key:
                raise ValueError('请配置 EMBEDDING_API_KEY')

    @classmethod
    def from_env(cls):
        mode = os.environ.get('EMBEDDING_MODE', 'local').lower()
        try:
            timeout = float(os.environ.get('EMBEDDING_TIMEOUT', '90'))
        except ValueError:
            raise ValueError('EMBEDDING_TIMEOUT 必须为1至300秒') from None
        return cls(mode=mode, model=os.environ.get('EMBEDDING_MODEL') or ('BAAI/bge-small-zh-v1.5' if mode == 'local' else ''),
                   base_url=os.environ.get('EMBEDDING_BASE_URL', '').rstrip('/'), api_key=os.environ.get('EMBEDDING_API_KEY', ''),
                   timeout=timeout, cache_dir=os.environ.get('EMBEDDING_CACHE_DIR') or None)


def validate_vectors(values, expected_count=None, dimension=None):
    if not isinstance(values, list) or (expected_count is not None and len(values) != expected_count):
        raise ValueError('向量模型返回的条数不正确')
    result = []
    for row in values:
        if not isinstance(row, (list, tuple)) or not row or len(row) > 65536:
            raise ValueError('向量格式或维度无效')
        if dimension is None:
            dimension = len(row)
        if len(row) != dimension:
            raise ValueError('向量维度不一致')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in row):
            raise ValueError('向量包含非有限数值')
        converted = [float(v) for v in row]
        norm = math.hypot(*converted)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError('向量为零或数值溢出')
        result.append(converted)
    return result


class EmbeddingProvider:
    def __init__(self, config=None):
        self.config = config or EmbeddingConfig.from_env()
        self._local = None

    def profile(self):
        config = self.config
        value = {'mode': config.mode, 'model': config.model, 'contract': 'dense-v1'}
        if config.mode == 'local':
            value['pooling'] = 'token-bounded-mean-v1'
        if config.mode == 'api':
            value['base_url'] = config.base_url.rstrip('/')
        value['fingerprint'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        return value

    def matches(self, profile):
        return isinstance(profile, dict) and profile.get('fingerprint') == self.profile()['fingerprint']

    def embed(self, texts):
        if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError('向量输入必须是非空文本列表')
        if not texts:
            return []
        if self.config.mode == 'local':
            try:
                if self._local is None:
                    from fastembed import TextEmbedding
                    self._local = TextEmbedding(model_name=self.config.model, cache_dir=self.config.cache_dir, providers=['CPUExecutionProvider'])
                # FastEmbed enables tokenizer truncation. Recover the overflow
                # spans before embedding so long inputs are never silently lost.
                segments = [self._local_segments(text) for text in texts]
                flat = [part for parts in segments for part in parts]
                embedded = validate_vectors([row.tolist() for row in self._local.embed(flat, batch_size=32)], len(flat))
                rows, offset = [], 0
                for parts in segments:
                    group = embedded[offset:offset + len(parts)]
                    offset += len(parts)
                    rows.append([sum(row[i] for row in group) / len(group) for i in range(len(group[0]))])
            except Exception:
                raise ValueError('本地向量模型加载或执行失败，请检查依赖、模型下载及缓存；未切换其他模型') from None
            return validate_vectors(rows, len(texts))
        rows = []
        for offset in range(0, len(texts), 32):
            rows.extend(self._api_embed(texts[offset:offset + 32]))
        return validate_vectors(rows, len(texts))

    def _local_segments(self, text):
        backend = getattr(self._local, 'model', None)
        tokenize = getattr(backend, 'tokenize', None)
        if tokenize is None:
            return split_text(text)
        encoding = tokenize([text])[0]
        overflows = encoding.overflowing
        if not overflows:
            return [text]
        boundaries = sorted({max((end for start, end in item.offsets if end > start), default=0)
                             for item in [encoding, *overflows]})
        boundaries = [end for end in boundaries if 0 < end < len(text)] + [len(text)]
        start, parts = 0, []
        for end in boundaries:
            if text[start:end].strip():
                parts.append(text[start:end])
            start = end
        return parts or [text]

    def _api_embed(self, texts):
        config = self.config
        request = Request(config.base_url.rstrip('/') + '/embeddings',
                          data=json.dumps({'model': config.model, 'input': texts, 'encoding_format': 'float'}, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config.api_key}, method='POST')
        try:
            with urlopen(request, timeout=config.timeout) as response:
                payload = json.loads(response.read(16 * 1024 * 1024).decode())
        except HTTPError as error:
            status = error.code
            error.close()
            raise ValueError(f'向量 API HTTP {status}，请检查配置、模型权限和额度') from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('向量 API 连接失败或超时，本次索引或检索未完成') from None
        except (ValueError, UnicodeError):
            raise ValueError('向量 API 返回的内容不是有效JSON') from None
        try:
            data = payload['data']
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError()
            indices = [item['index'] for item in data]
            if any(isinstance(i, bool) or not isinstance(i, int) for i in indices) or set(indices) != set(range(len(texts))):
                raise ValueError()
            ordered = sorted(data, key=lambda item: item['index'])
            return validate_vectors([item['embedding'] for item in ordered], len(texts))
        except (KeyError, TypeError, ValueError):
            raise ValueError('向量 API 返回的条数、顺序或向量格式无效') from None


def split_text(text, max_length=350, overlap=40):
    """Bound dense-model input size while retaining an overlap for Chinese prose.

    The small default leaves room under the local model's 512-token limit for
    normal Chinese prose. Local embedding also checks actual tokenizer overflow.
    """
    if not isinstance(text, str) or isinstance(max_length, bool) or isinstance(overlap, bool) or not isinstance(max_length, int) or not isinstance(overlap, int) or not 0 <= overlap < max_length:
        raise ValueError('文本切分参数无效')
    if not text.strip():
        return []
    chunks, start = [], 0
    while start < len(text):
        stop = min(len(text), start + max_length)
        chunks.append(text[start:stop])
        if stop == len(text):
            break
        start = stop - overlap
    return chunks

"""Process-isolated Docling adapter with content-addressed page checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
def parser_id():
    return 'docling-2.145.0-rapidocr-3.10.0-v2-formula-'+os.environ.get('DOCLING_FORMULA', 'false').lower()

PARSER = parser_id()


def available():
    return Path(os.environ.get('DOCLING_PYTHON', ROOT/'.venv-docling/bin/python')).exists()


def parse(content, filename, cache_root, progress=None):
    digest = hashlib.sha256(content).hexdigest()
    key = hashlib.sha256((digest+parser_id()).encode()).hexdigest()
    folder = Path(cache_root)/key
    folder.mkdir(parents=True, exist_ok=True)
    source = folder/('source'+Path(filename).suffix.lower())
    if not source.exists(): source.write_bytes(content)
    env = dict(os.environ, HF_HOME=str(Path(cache_root)/'models'), TOKENIZERS_PARALLELISM='false')
    python = str(os.environ.get('DOCLING_PYTHON', ROOT/'.venv-docling/bin/python'))
    log = folder/'worker.log'
    with log.open('a') as stderr:
        process = subprocess.Popen([python, str(ROOT/'scripts/docling_worker.py'), str(source), str(folder)],
                                   stdout=subprocess.PIPE, stderr=stderr, text=True, env=env)
        for line in process.stdout:
            if progress: progress(line.strip())
        code = process.wait()
    manifest = json.loads((folder/'manifest.json').read_text()) if (folder/'manifest.json').exists() else {}
    if code or manifest.get('failed_pages') or len(manifest.get('pages', [])) != manifest.get('total'):
        raise ValueError('Docling解析未完成；失败页：'+str(manifest.get('failed_pages', '初始化失败'))+'。已保存成功页，重新解析将复用检查点。日志编号：'+key)
    blocks = load_blocks(folder, manifest['pages'], filename)
    return {'filename': filename, 'sha256': digest, 'document_type': 'reference', 'suggested_type': 'reference',
            'parser_version': parser_id(), 'artifact_key': key, 'pages': manifest,
            'blocks': blocks, 'rules': [], 'warnings': ['Docling结构化提取；可对照原页抽查OCR及表格，公式等待核对内容暂不进入检索，未生成诊断规则。',
                '公式与插图识别不保证正确；原页面及解析JSON已保留。图片不生成未经来源支持的解释。',
                '待核对图像页：'+str(manifest.get('unresolved_pages', {}))]}


def load_blocks(folder, pages, filename):
    blocks = []
    for page in pages:
        record = json.loads((folder/f'page-{page}.json').read_text())
        for block in record['blocks']:
            block['origin'] = 'ocr' if Path(filename).suffix.lower() == '.pdf' or block.get('extraction') == 'docling_image_ocr' else 'native_office'
            if len(block['text'].strip()) < 12 and block.get('content_types') == ['text']:
                block['excluded'] = True
            blocks.append({'kind': 'description', **block, 'id': f'b{len(blocks)+1}', 'chunk_ready': True})
    return blocks

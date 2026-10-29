"""Real parsed-content -> isolated review/publication -> Agent API acceptance.

Run docling_worker.py on the complete original document first. Approval here is
only an isolated workflow check, NOT a claim that all OCR has been proofread.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.embeddings import EmbeddingProvider, EmbeddingConfig

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--parsed', type=Path, required=True)
parser.add_argument('--output', type=Path, default=Path('outputs/structured-acceptance.json'))
args = parser.parse_args()
manifest = json.loads((args.parsed/'manifest.json').read_text())
assert not manifest['failed_pages'], manifest['failed_pages']
assert len(manifest['pages']) == manifest['total'], '整本尚未完成，不能用代表页冒充全文验收'
from rag_service.structured_reference import load_blocks
blocks = load_blocks(args.parsed, manifest['pages'], args.source.name)
payload = {'blocks': blocks, 'rules':[], 'document_type':'reference', 'warnings':['实际OCR结果需人工校准，验收库发布只验证操作流程。']}
with tempfile.TemporaryDirectory(prefix='structured-reference-acceptance-') as folder:
    embedding = EmbeddingProvider(EmbeddingConfig(cache_dir=str(Path('data/rag_work/models').resolve())))
    app = create_app(Settings(Path(folder), 'accept-admin-12345','accept-read-12345'), embedder=embedding, model_factory=lambda:None)
    store = app.state.store
    kb = store.create_kb('真实资料隔离验收')['id']
    reserve = store.reserve_upload(kb, args.source.name, args.source.read_bytes(), document_type='reference')
    doc = store.finish_upload(reserve['document_id'],1,payload)
    store.review(doc['id'],doc['edit_version'],'description','approved',note='仅验收审核发布流程，不代表全书文字校准')
    release = store.publish(kb, embedding)
    client = TestClient(app); client.headers['Authorization']='Bearer accept-read-12345'
    questions = ['机械振动研究哪些问题', '已知输入和系统参数求系统响应属于什么分析', '临界转速与共振有什么区别', '晚饭吃什么', '如何办理护照']
    if args.source.suffix.lower() == '.pptx': questions = ['如何生成Word报告','没有15号机器时需要确认什么','知识库怎么调用','晚饭吃什么','如何办理护照']
    results = []
    for question in questions:
        response = client.post('/api/v1/retrieve', json={'kb_id':kb,'release_id':release['id'],'query':question,'top_k':3})
        assert response.status_code == 200,response.text
        value=response.json()
        results.append({'question':question,'hits':[{'text':hit['text'],'source':hit['source'],'semantic_score':hit['semantic_score'],'keyword_score':hit['keyword_score']} for hit in value['items']]})
    if args.source.suffix.lower() == '.pdf':
        for result, expected in zip(results[:3], ['PDF物理页 25','PDF物理页 25','PDF物理页 60']):
            assert result['hits'] and expected in result['hits'][0]['source']['locator'], result
    assert all(not result['hits'] for result in results[-2:]), '无关问题仍有误召回'
    assert client.get('/api/v1/rules',params={'kb_id':kb}).json()['count']==0
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'source':args.source.name,'pages':manifest['total'],'published_items':release['entry_count'],
        'source_pages':max(b.get('page', 1) for b in blocks), 'unresolved_pages':manifest.get('unresolved_pages',{}), 'quarantined_formulas':sum('formula' in b.get('content_types',[]) for b in blocks), 'quarantined_entries':sum(b['kind']=='uncertain' for b in blocks), 'results':results},ensure_ascii=False,indent=2))
    print(json.dumps({'published_items':release['entry_count'],'pages':manifest['total'],'result':str(args.output)},ensure_ascii=False))

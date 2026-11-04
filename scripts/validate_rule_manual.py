import sys, tempfile, json, argparse
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from rag_service.config import Settings
from rag_service.app import create_app
from rag_service.embeddings import EmbeddingProvider, EmbeddingConfig
parser = argparse.ArgumentParser(description='在临时库验收标准机理 Word，使用真实本地向量，不需要对话密钥')
parser.add_argument('--document', type=Path, default=ROOT/'rag_service/assets/rules_reference.docx')
parser.add_argument('--cache-dir', type=Path, default=ROOT/'data/rag_work/models')
parser.add_argument('--output', type=Path, default=ROOT/'outputs/rule-manual-validation.json')
args = parser.parse_args()
path = args.document
cache = args.cache_dir
with tempfile.TemporaryDirectory(prefix='rag-manual-acceptance-') as root:
    embedding = EmbeddingProvider(EmbeddingConfig(cache_dir=str(cache)))
    app = create_app(Settings(Path(root), 'accept-admin-12345', 'accept-read-12345'), embedder=embedding, model_factory=lambda: None)
    with TestClient(app) as c:
        c.headers['Authorization']='Bearer accept-admin-12345'
        kb=c.post('/api/v1/admin/knowledge-bases', json={'name':'规则原文验收'}).json()['id']
        upload=c.post('/api/v1/admin/documents', data={'kb_id':kb,'document_type':'rules'}, files={'file':(path.name,path.read_bytes())}).json()
        task=c.get('/api/v1/admin/tasks/'+upload['task_id']).json(); assert task['status']=='succeeded',task
        doc=c.get('/api/v1/admin/documents/'+upload['document_id']).json()
        assert len(doc['payload']['rules'])==16
        # Only approve our isolated acceptance drafts, never production content.
        s=app.state.store
        doc=s.review(doc['id'],doc['edit_version'],'description','approved')
        for rule in doc['payload']['rules']:doc=s.review(doc['id'],doc['edit_version'],'rule','approved',rule['id'])
        release=s.publish(kb,embedding)
        assert len(release['entries'])==16
        c.headers['Authorization']='Bearer accept-read-12345'
        results=[]
        for rule in doc['payload']['rules']:
            for suffix in ['有哪些原因','有哪些处理措施','判断条件是什么']:
                q=rule['name']+suffix
                r=c.post('/api/v1/retrieve',json={'kb_id':kb,'release_id':release['id'],'query':q,'top_k':5})
                assert r.status_code==200,r.text
                hits=r.json()['items'];assert hits[0]['rule']['name']==rule['name'],q
                a=c.post('/api/v1/answers',json={'kb_id':kb,'release_id':release['id'],'query':q}).json()
                assert a['status']=='answered',a
                assert a['citations'][0]['quote'] in hits[0]['source']['original_quote']
                results.append({'question':q,'first_match':hits[0]['rule']['name'],'answer':a['answer']})
        out=args.output;out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'source':path.name,'embedding':embedding.profile(),'entry_count':16,'passed_queries':len(results),'results':results},ensure_ascii=False,indent=2))
        print('PASS: actual Word; 16 fault units; 48 real local embedding HTTP retrieve+answer checks; unchanged Agent response models; no chat model calls.')

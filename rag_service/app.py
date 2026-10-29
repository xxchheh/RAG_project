"""Independent HTTP entry point. Diagnosis is performed by the caller."""
from __future__ import annotations
import secrets
import os
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, BackgroundTasks, Depends, File, Form, UploadFile, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from .config import Settings
from .contracts import METRICS, DERIVED, DEVICE_TYPES, DERIVED_LABELS
from .store import Store, StoreError
from .embeddings import EmbeddingProvider
from .models import ChatModel, ModelConfig


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Name(Input):
    name: str = Field(min_length=1, max_length=100)


class Archive(Input):
    archived: bool = True


class Disable(Input):
    disabled: bool = True


class Edit(Input):
    expected_version: int = Field(ge=1)
    blocks: list[dict] | None = None
    rules: list[dict] | None = None


class Review(Input):
    expected_version: int = Field(ge=1)
    target: Literal['description', 'rule']
    decision: Literal['approved', 'rejected']
    rule_id: str | None = None
    note: str = Field(default='', max_length=2000)


class Publication(Input):
    kb_id: str


class Restore(Publication):
    release_id: str


class Query(Publication):
    query: str = Field(min_length=1, max_length=4000)
    release_id: str | None = None
    device_type: Literal['motor', 'pump', 'fan', 'coupling', 'gearbox'] | None = None
    top_k: int = Field(default=5, ge=1, le=50)


def create_app(settings=None, embedder=None, model=None, model_factory=None):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir, recover_tasks=True)
    embedder = embedder or EmbeddingProvider()
    def get_model():
        return model or (model_factory() if model_factory else ChatModel(ModelConfig.from_env()))
    app = FastAPI(title='专业知识 RAG 服务', version='1.0.0', description='分级审核、版本化发布、来源检索。专业规则由调用方执行。')
    app.state.store, app.state.embedder = store, embedder
    bearer = HTTPBearer(auto_error=False)

    def read_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        token = credentials.credentials if credentials else ''
        if not any(secrets.compare_digest(token.encode(), valid.encode()) for valid in (settings.admin_token, settings.read_token)):
            raise HTTPException(401, '需要有效的管理令牌或只读令牌')
        return token

    def admin_auth(token=Depends(read_auth)):
        if not secrets.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(403, '此操作需要管理令牌')

    @app.exception_handler(StoreError)
    def store_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=error.status)

    @app.exception_handler(ValueError)
    def value_error(request, error):
        return JSONResponse({'detail': str(error)}, status_code=400)

    prefix = '/api/v1'
    read = [Depends(read_auth)]
    admin = [Depends(admin_auth)]

    @app.get(prefix+'/health')
    def health():
        return {'status': 'ok', 'embedding': embedder.profile(), 'rule_schema_version': 'equipment-rules-v1'}

    @app.get(prefix+'/capabilities', dependencies=read)
    def capabilities():
        return {'schema_version': 'equipment-rules-v1', 'device_types': {'motor':'电机','pump':'水泵','fan':'风机','coupling':'联轴器','gearbox':'齿轮箱'},
                'metrics': {k: {'name':v[0], 'unit':v[1]} for k,v in METRICS.items()},
                'derived': {k: {'name':DERIVED_LABELS[k], 'unit':'1', 'numerators':v[0], 'denominator':v[1], 'formula':'sqrt(sum(numerator**2))/denominator'} for k,v in DERIVED.items()},
                'operators':['ge','gt','le','lt'], 'groups':['all','any'], 'formats':['md','docx','pdf'], 'executes_diagnosis':False}

    @app.get(prefix+'/knowledge-bases', dependencies=read)
    def list_kbs():
        return store.list_kbs()

    @app.get(prefix+'/admin/knowledge-bases', dependencies=admin)
    def all_kbs():
        return store.list_kbs(True)

    @app.post(prefix+'/admin/knowledge-bases', dependencies=admin)
    def create_kb(body: Name):
        return store.create_kb(body.name)

    @app.post(prefix+'/admin/knowledge-bases/{kb_id}/archive', dependencies=admin)
    def archive(kb_id: str, body: Archive):
        return store.archive_kb(kb_id, body.archived)

    @app.get(prefix+'/admin/documents', dependencies=admin)
    def documents(kb_id: str):
        return store.documents(kb_id)

    def process_file(task_id, document_id, revision, content, filename):
        try:
            store.set_task(task_id, 'processing', '解析与提取')
            from .documents import parse_document
            try:
                chosen_model = get_model()
            except ValueError:
                configured_key=any(os.environ.get(k) for k in ('LLM_API_KEY','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','OPENAI_API_KEY'))
                if model is not None or model_factory is not None or (configured_key and os.environ.get('LLM_MODEL')):
                    raise
                chosen_model = None
            payload = parse_document(content, filename, chosen_model)
            result = store.finish_upload(document_id, revision, payload)
            store.set_task(task_id, 'succeeded', '待审核', {'document_id':document_id,'revision':revision,'warnings':result['payload'].get('warnings',[])})
        except Exception as exc:
            store.fail_upload(document_id)
            message = str(exc) if isinstance(exc, ValueError) else '文档处理失败，请检查文件格式或服务运行环境'
            store.set_task(task_id, 'failed', '处理失败', error=message)

    async def accept_upload(background, file, kb_id, external_id=None, document_id=None):
        content = await file.read(10*1024*1024+1)
        name = Path((file.filename or '').replace('\\','/')).name
        result = store.reserve_upload(kb_id, name, content, external_id, document_id)
        task_id = store.new_task()
        if result['unchanged']:
            store.set_task(task_id, 'succeeded', '文件未变化', result)
        else:
            background.add_task(process_file, task_id, result['document_id'], result['revision'], content, name)
        return {'task_id':task_id, **result}

    @app.post(prefix+'/admin/documents', dependencies=admin, status_code=202)
    async def upload(background: BackgroundTasks, kb_id: str = Form(...), file: UploadFile = File(...), external_id: str | None = Form(None)):
        return await accept_upload(background, file, kb_id, external_id)

    @app.post(prefix+'/admin/documents/{document_id}/revisions', dependencies=admin, status_code=202)
    async def replace(document_id: str, background: BackgroundTasks, file: UploadFile = File(...)):
        doc = store.document(document_id)
        return await accept_upload(background, file, doc['kb_id'], document_id=document_id)

    @app.get(prefix+'/admin/documents/{document_id}', dependencies=admin)
    def get_document(document_id: str):
        return store.document(document_id)

    @app.get(prefix+'/admin/documents/{document_id}/audits', dependencies=admin)
    def audits(document_id: str):
        return store.audits(document_id)

    @app.get(prefix+'/admin/documents/{document_id}/original', dependencies=admin)
    def original(document_id: str):
        doc=store.document(document_id)
        path=store.root/'sources'/(doc['file_hash']+Path(doc['file_name']).suffix.lower())
        return FileResponse(path, filename=doc['file_name'])

    @app.patch(prefix+'/admin/documents/{document_id}', dependencies=admin)
    def edit(document_id: str, body: Edit):
        return store.edit_document(document_id, body.expected_version, body.blocks, body.rules)

    @app.post(prefix+'/admin/documents/{document_id}/review', dependencies=admin)
    def review(document_id: str, body: Review):
        return store.review(document_id, **body.model_dump())

    @app.post(prefix+'/admin/documents/{document_id}/disable', dependencies=admin)
    def disable(document_id: str, body: Disable):
        return store.disable_document(document_id, body.disabled)

    @app.get(prefix+'/admin/tasks/{task_id}', dependencies=admin)
    def task(task_id: str):
        return store.task(task_id)

    def publish_job(task_id, kb_id, restore_id=None, reindex=False):
        try:
            store.set_task(task_id, 'processing', '准备索引并发布')
            result = store.publish(kb_id, embedder, restore_id, reindex)
            store.set_task(task_id, 'succeeded', '已发布', {k:v for k,v in result.items() if k!='entries'})
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '索引构建或发布失败，原发布版本仍可使用'
            store.set_task(task_id, 'failed', '发布失败', error=message)

    def enqueue_publish(background, kb_id, restore_id=None, reindex=False):
        task_id=store.new_task()
        background.add_task(publish_job, task_id, kb_id, restore_id, reindex)
        return {'task_id':task_id}

    @app.post(prefix+'/admin/publish', dependencies=admin, status_code=202)
    def publish(body: Publication, background: BackgroundTasks):
        return enqueue_publish(background, body.kb_id)

    @app.post(prefix+'/admin/restore', dependencies=admin, status_code=202)
    def restore(body: Restore, background: BackgroundTasks):
        return enqueue_publish(background, body.kb_id, body.release_id)

    @app.post(prefix+'/admin/reindex', dependencies=admin, status_code=202)
    def reindex(body: Publication, background: BackgroundTasks):
        return enqueue_publish(background, body.kb_id, reindex=True)

    @app.get(prefix+'/admin/releases', dependencies=admin)
    def releases(kb_id: str):
        return store.releases(kb_id)

    @app.get(prefix+'/admin/releases/{release_id}', dependencies=admin)
    def release_detail(release_id: str):
        with store.connection() as c:
            row=c.execute('SELECT kb_id FROM releases WHERE id=?',(release_id,)).fetchone()
            if not row:
                raise StoreError('发布版本不存在',404)
        return store.release(row['kb_id'],release_id)

    def active_release(kb_id, release_id=None):
        kbs=store.list_kbs()
        if kb_id not in {k['id'] for k in kbs}:
            raise StoreError('知识库不存在或已归档',404)
        return store.release(kb_id, release_id, include_index=True)

    @app.post(prefix+'/retrieve', dependencies=read)
    def retrieve(body: Query):
        from .retrieval import retrieve as search
        rel=active_release(body.kb_id,body.release_id)
        hits=search(body.query,rel['entries'],rel['index'],embedder,body.top_k,body.device_type)
        return {'release_id':rel['id'],'kb_id':body.kb_id,'profile':rel['profile'],'items':hits}

    @app.post(prefix+'/answers', dependencies=read)
    def answers(body: Query):
        from .retrieval import answer_question
        found=retrieve(body)
        result=answer_question(body.query,found['items'],get_model() if found['items'] else None)
        return {**found,**result}

    @app.get(prefix+'/rules', dependencies=read)
    def rules(kb_id: str, release_id: str | None = None, device_type: str | None = None):
        if device_type is not None and device_type not in DEVICE_TYPES:
            raise StoreError('设备类型不受支持')
        rel=active_release(kb_id,release_id)
        values=[{**e['rule'],'source':e['source'],'knowledge_id':e['id']} for e in rel['entries'] if e['kind']=='rule' and (device_type is None or e.get('device_type')==device_type)]
        return {'kb_id':kb_id,'release_id':rel['id'],'schema_version':'equipment-rules-v1','rules':values,'count':len(values)}

    @app.get(prefix+'/knowledge/{knowledge_id}', dependencies=read)
    def knowledge(knowledge_id: str, kb_id: str, release_id: str | None = None):
        rel=active_release(kb_id,release_id)
        entry=next((e for e in rel['entries'] if e['id']==knowledge_id),None)
        if not entry:
            raise StoreError('该发布版本中不存在此知识条目',404)
        return {'release_id':rel['id'],'item':entry}

    static=Path(__file__).parent/'static'
    app.mount('/static', StaticFiles(directory=static), name='static')
    @app.get('/',include_in_schema=False)
    def home():
        return FileResponse(static/'index.html')
    return app

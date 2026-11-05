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
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import api_contract as wire
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
    excluded_pages: list[int] | None = None


class Review(Input):
    expected_version: int = Field(ge=1)
    target: Literal['description', 'rule']
    decision: Literal['approved', 'rejected']
    rule_id: str | None = None
    note: str = Field(default='', max_length=2000)


class Confirmation(Input):
    include_context: bool = False
    expected_version: int = Field(ge=1)
    target: Literal['description', 'rule']
    rule_id: str | None = None
    note: str = Field(default='', max_length=2000)
    excluded_pages: list[int] | None = Field(default=None, max_length=10000)


class BatchConfirmation(Input):
    expected_version: int = Field(ge=1)
    rule_ids: list[str] = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=2000)


class DocumentPublication(Input):
    expected_version: int = Field(ge=1)


class Reprocess(Input):
    document_type: Literal['auto', 'rules', 'reference'] = 'auto'


class Publication(Input):
    kb_id: str


class Restore(Publication):
    release_id: str


class Query(Publication):
    query: str = Field(min_length=1, max_length=4000)
    release_id: str | None = None
    device_type: wire.DeviceType | None = None
    top_k: int = Field(default=5, ge=1, le=50)


class ChatTurn(Input):
    query: str = Field(min_length=1, max_length=4000)
    answer: str = Field(min_length=1, max_length=20000)


class ChatQuery(Query):
    history: list[ChatTurn] = Field(default_factory=list, max_length=100)


class SessionCreate(Input):
    kb_id: str
    release_id: str | None = None
    device_type: wire.DeviceType | None = None


class SessionRename(Input):
    title: str = Field(min_length=1, max_length=100)


class SessionMessage(Input):
    query: str = Field(min_length=1, max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)
    retry_turn_id: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)


def create_app(settings=None, embedder=None, model=None, model_factory=None, answer_model_factory=None):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir, recover_tasks=True)
    from .chat_store import ChatStore
    chats = ChatStore(settings.data_dir)
    embedder = embedder or EmbeddingProvider()
    def get_model():
        return model or (model_factory() if model_factory else ChatModel(ModelConfig.from_env()))
    def get_answer_model():
        if answer_model_factory is not None:
            return answer_model_factory()
        if model is not None or model_factory is not None:
            return get_model()
        return ChatModel(ModelConfig.for_answers())
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
        return error_response(error.status, str(error))

    @app.exception_handler(ValueError)
    def value_error(request, error):
        return error_response(400, str(error))

    def error_response(status, detail, headers=None):
        return JSONResponse({'detail': detail, 'code': wire.ERROR_CODES.get(status, 'INTERNAL_ERROR' if status >= 500 else 'HTTP_ERROR')}, status_code=status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    def http_error(request, error):
        return error_response(error.status_code, error.detail, error.headers)

    @app.exception_handler(RequestValidationError)
    def invalid_request(request, error):
        # Do not echo credentials or request bodies in validation errors.
        details = [{k: jsonable_encoder(v) for k, v in item.items() if k in ('loc', 'msg', 'type')} for item in error.errors()]
        return error_response(422, details)

    @app.exception_handler(Exception)
    def unexpected_error(request, error):
        return error_response(500, '服务内部错误，请联系服务提供方')

    prefix = '/api/v1'
    read = [Depends(read_auth)]
    admin = [Depends(admin_auth)]

    @app.get(prefix+'/health', response_model=wire.Health, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def health():
        return {'status': 'ok', 'embedding': embedder.profile(), 'rule_schema_version': wire.RULE_SCHEMA_VERSION}

    @app.get(prefix+'/capabilities', dependencies=read, response_model=wire.Capabilities, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def capabilities():
        return {'schema_version': wire.RULE_SCHEMA_VERSION, 'device_types': {'motor':'电机','pump':'水泵','fan':'风机','coupling':'联轴器','gearbox':'齿轮箱'},
                'metrics': {k: {'name':v[0], 'unit':v[1]} for k,v in METRICS.items()},
                'derived': {k: {'name':DERIVED_LABELS[k], 'unit':'1', 'numerators':v[0], 'denominator':v[1], 'formula':'sqrt(sum(numerator**2))/denominator'} for k,v in DERIVED.items()},
                'operators':['ge','gt','le','lt'], 'groups':['all','any'], 'formats':['md','docx','pdf','pptx'], 'executes_diagnosis':False}

    @app.get(prefix+'/knowledge-bases', dependencies=read, response_model=list[wire.KnowledgeBase], response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
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
        return [{**doc, 'publication': store.publication_status(doc['id']), 'tasks': store.document_tasks(doc['id'], doc['latest_revision'])} for doc in store.documents(kb_id)]

    def process_file(task_id, document_id, revision, content, filename, document_type="auto"):
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
            payload = parse_document(content, filename, chosen_model, document_type, progress=lambda stage: store.set_task(task_id, 'processing', stage), artifact_root=store.root/'parsed')
            result = store.finish_upload(document_id, revision, payload)
            store.set_task(task_id, 'succeeded', '待审核', {'document_id':document_id,'revision':revision,'warnings':result['payload'].get('warnings',[])})
        except Exception as exc:
            store.fail_upload(document_id)
            message = str(exc) if isinstance(exc, ValueError) else '文档处理失败，请检查文件格式或服务运行环境'
            store.set_task(task_id, 'failed', '处理失败', error=message)

    async def accept_upload(background, file, kb_id, external_id=None, document_id=None, document_type="auto"):
        content = await file.read(100*1024*1024+1)
        name = Path((file.filename or '').replace('\\','/')).name
        result = store.reserve_upload(kb_id, name, content, external_id, document_id, document_type=document_type)
        task_id = store.new_task(document_id=result['document_id'], revision=result['revision'], operation='parse')
        if result['unchanged']:
            store.set_task(task_id, 'succeeded', '文件未变化', result)
        else:
            background.add_task(process_file, task_id, result['document_id'], result['revision'], content, name, document_type)
        return {'task_id':task_id, **result}

    @app.post(prefix+'/admin/documents', dependencies=admin, status_code=202)
    async def upload(background: BackgroundTasks, kb_id: str = Form(...), file: UploadFile = File(...), external_id: str | None = Form(None), document_type: Literal['auto', 'rules', 'reference'] = Form('auto')):
        return await accept_upload(background, file, kb_id, external_id, document_type=document_type)

    @app.post(prefix+'/admin/documents/{document_id}/revisions', dependencies=admin, status_code=202)
    async def replace(document_id: str, background: BackgroundTasks, file: UploadFile = File(...), document_type: Literal['auto', 'rules', 'reference'] = Form('auto')):
        doc = store.document(document_id)
        return await accept_upload(background, file, doc['kb_id'], document_id=document_id, document_type=document_type if document_type != 'auto' else doc['payload'].get('document_type', 'auto'))

    @app.post(prefix+'/admin/documents/{document_id}/reprocess', dependencies=admin, status_code=202)
    def reprocess(document_id: str, body: Reprocess, background: BackgroundTasks):
        doc = store.document(document_id)
        path = store.root/'sources'/(doc['file_hash']+Path(doc['file_name']).suffix.lower())
        content = path.read_bytes()
        result = store.reserve_upload(doc['kb_id'], doc['file_name'], content,
            document_id=document_id, document_type=body.document_type, force=True)
        task_id = store.new_task(document_id=result['document_id'], revision=result['revision'], operation='parse')
        background.add_task(process_file, task_id, document_id, result['revision'], content, doc['file_name'], body.document_type)
        return {'task_id': task_id, **result}

    @app.get(prefix+'/admin/documents/{document_id}', dependencies=admin)
    def get_document(document_id: str):
        doc = store.document(document_id)
        return {**doc, 'publication': store.publication_status(document_id), 'tasks': store.document_tasks(document_id, doc['latest_revision'])}

    @app.get(prefix+'/admin/documents/{document_id}/processing', dependencies=admin)
    def processing_detail(document_id: str):
        import hashlib, json
        from .structured_reference import parser_id
        doc = store.document(document_id)
        key = hashlib.sha256((doc['file_hash']+parser_id()).encode()).hexdigest()
        path = store.root/'parsed'/key/'manifest.json'
        return json.loads(path.read_text()) if path.exists() else {'pages': [], 'failed_pages': {}, 'stage': doc['status']}

    @app.get(prefix+'/admin/documents/{document_id}/pictures/{number}', dependencies=admin)
    def original_picture(document_id: str, number: int):
        import hashlib
        from .structured_reference import parser_id
        if number < 1: raise HTTPException(400, '图像编号无效')
        doc = store.document(document_id)
        key = hashlib.sha256((doc['file_hash']+parser_id()).encode()).hexdigest()
        path = store.root/'parsed'/key/f'picture-{number}.png'
        if not path.exists(): raise HTTPException(404, '图像区域不存在，请确认对应解析配置或下载原件')
        return FileResponse(path, media_type='image/png')

    @app.get(prefix+'/admin/documents/{document_id}/pages/{page}', dependencies=admin)
    def original_page(document_id: str, page: int):
        if page < 1: raise HTTPException(400, '页码必须为正整数')
        doc = store.document(document_id)
        if Path(doc['file_name']).suffix.lower() != '.pdf':
            raise HTTPException(400, 'PPTX请下载原件核对；PDF可直接预览页面')
        import io
        import pypdfium2 as pdfium
        from fastapi.responses import Response
        path = store.root/'sources'/(doc['file_hash']+'.pdf')
        from .ocr import _PDF_LOCK
        with _PDF_LOCK:
            pdf = pdfium.PdfDocument(path)
            try:
                if page > len(pdf): raise HTTPException(404, '页码超出范围')
                rendered = pdf[page-1].render(scale=1.5).to_pil()
                output = io.BytesIO(); rendered.save(output, format='PNG')
                return Response(output.getvalue(), media_type='image/png')
            finally: pdf.close()

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
        return store.edit_document(document_id, body.expected_version, body.blocks, body.rules, body.excluded_pages)

    @app.post(prefix+'/admin/documents/{document_id}/review', dependencies=admin)
    def review(document_id: str, body: Review):
        return store.review(document_id, **body.model_dump())

    @app.post(prefix+'/admin/documents/{document_id}/disable', dependencies=admin)
    def disable(document_id: str, body: Disable):
        return store.disable_document(document_id, body.disabled)

    @app.get(prefix+'/admin/tasks/{task_id}', dependencies=admin)
    def task(task_id: str):
        return store.task(task_id)

    def publish_job(task_id, kb_id, restore_id=None, reindex=False, scope=None):
        try:
            store.set_task(task_id, 'processing', '准备索引并发布')
            result = store.publish(kb_id, embedder, restore_id, reindex, **(scope or {}))
            store.set_task(task_id, 'succeeded', '已发布', {k:v for k,v in result.items() if k!='entries'})
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else '索引构建或发布失败，原发布版本仍可使用'
            store.set_task(task_id, 'failed', '发布失败', error=message)

    def enqueue_publish(background, kb_id, restore_id=None, reindex=False, scope=None):
        doc = store.document(scope['document_id']) if scope else None
        task_id=store.new_task(document_id=doc['id'] if doc else None,
            revision=doc['latest_revision'] if doc else None, rule_id=None if isinstance((scope or {}).get('rule_id'), list) else (scope or {}).get('rule_id'),
            rule_ids=(scope or {}).get('rule_id') if isinstance((scope or {}).get('rule_id'), list) else None,
            operation=('entry' if (scope or {}).get('target') == 'rule' else 'description') if (scope or {}).get('target') else 'document',
            expected_version=(scope or {}).get('expected_version'))
        background.add_task(publish_job, task_id, kb_id, restore_id, reindex, scope)
        return {'task_id':task_id}

    @app.post(prefix+'/admin/documents/{document_id}/confirm', dependencies=admin, status_code=202)
    def confirm_document(document_id: str, body: Confirmation, background: BackgroundTasks):
        doc = store.document(document_id)
        if doc['disabled']:
            raise HTTPException(409, '请先恢复使用此文档')
        reviewed = store.review(document_id, body.expected_version, body.target, 'approved',
            body.rule_id, body.note, body.excluded_pages, reuse_approval=True, include_context=body.include_context)
        return {**enqueue_publish(background, reviewed['kb_id'], scope={
            'document_id': document_id, 'target': body.target, 'rule_id': body.rule_id,
            'expected_version': reviewed['edit_version']}), 'edit_version': reviewed['edit_version']}

    @app.post(prefix+'/admin/documents/{document_id}/confirm-batch', dependencies=admin, status_code=202)
    def confirm_batch(document_id: str, body: BatchConfirmation, background: BackgroundTasks):
        doc = store.review_batch(document_id, body.expected_version, body.rule_ids, body.note)
        queued = enqueue_publish(background, doc['kb_id'], scope={
            'document_id': document_id, 'target': 'rule', 'rule_id': body.rule_ids,
            'expected_version': doc['edit_version']})
        return {**queued, 'edit_version': doc['edit_version'], 'rule_ids': body.rule_ids}

    @app.post(prefix+'/admin/documents/{document_id}/publish', dependencies=admin, status_code=202)
    def publish_document(document_id: str, body: DocumentPublication, background: BackgroundTasks):
        doc = store.document(document_id)
        if doc['edit_version'] != body.expected_version:
            raise HTTPException(409, '文档已变化，请刷新后重试')
        return enqueue_publish(background, doc['kb_id'], scope={
            'document_id': document_id, 'expected_version': body.expected_version})

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

    @app.post(prefix+'/retrieve', dependencies=read, response_model=wire.Retrieval, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def retrieve(body: Query):
        from .retrieval import retrieve as search
        rel=active_release(body.kb_id,body.release_id)
        hits=search(body.query,rel['entries'],rel['index'],embedder,body.top_k,body.device_type)
        return {'release_id':rel['id'],'kb_id':body.kb_id,'profile':rel['profile'],'items':hits}

    @app.post(prefix+'/answers', dependencies=read, response_model=wire.Answer, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def answers(body: Query):
        from .retrieval import answer_question
        found=retrieve(body)
        result=answer_question(body.query,found['items'],None,model_factory=get_answer_model)
        return {**found,**result}

    @app.post(prefix+'/generated-answers', dependencies=read, response_model=wire.GeneratedAnswer, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def generated_answers(body: Query):
        from .generated_answers import generate_answer
        found = retrieve(body)
        result = generate_answer(body.query, found['items'], get_answer_model)
        return {**found, **result}

    @app.post(prefix+'/chat', dependencies=read, response_model=wire.ChatAnswer, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def chat(body: ChatQuery):
        from .chat import bounded_history, resolve_question
        from .generated_answers import generate_answer
        # Resolve the release before invoking a model, including clarification turns.
        rel = active_release(body.kb_id, body.release_id)
        history = bounded_history([turn.model_dump() for turn in body.history])
        question, clarification = resolve_question(body.query, history, get_answer_model)
        if clarification:
            return {'kb_id': body.kb_id, 'release_id': rel['id'], 'profile': rel['profile'], 'items': [],
                    'status': 'clarification_required', 'answer': clarification, 'citations': [],
                    'mode': 'grounded_generation', 'model_info': {}, 'retrieval_query': None}
        found = retrieve(Query(kb_id=body.kb_id, release_id=rel['id'], query=question,
                               device_type=body.device_type, top_k=body.top_k))
        result = generate_answer(body.query, found['items'], get_answer_model, history, question)
        return {**found, **result, 'retrieval_query': question}

    @app.get(prefix+'/chat/sessions', response_model=list[wire.ConversationSummary], responses=wire.ERROR_RESPONSES)
    def list_sessions(token=Depends(read_auth)):
        return chats.list(chats.owner(token))

    @app.post(prefix+'/chat/sessions', response_model=wire.Conversation, response_model_exclude_unset=True, status_code=201, responses=wire.ERROR_RESPONSES)
    def create_session(body: SessionCreate, token=Depends(read_auth)):
        rel = active_release(body.kb_id, body.release_id)
        return chats.create(chats.owner(token), body.kb_id, rel['id'], body.device_type)

    @app.get(prefix+'/chat/sessions/{session_id}', response_model=wire.Conversation, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def get_session(session_id: str, token=Depends(read_auth)):
        return chats.get(chats.owner(token), session_id)

    @app.patch(prefix+'/chat/sessions/{session_id}', response_model=wire.Conversation, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def rename_session(session_id: str, body: SessionRename, token=Depends(read_auth)):
        return chats.rename(chats.owner(token), session_id, body.title)

    @app.delete(prefix+'/chat/sessions/{session_id}', response_model=wire.ConversationDeleted, responses=wire.ERROR_RESPONSES)
    def delete_session(session_id: str, token=Depends(read_auth)):
        chats.delete(chats.owner(token), session_id)
        return {'deleted': True}

    @app.post(prefix+'/chat/sessions/{session_id}/messages', response_model=wire.Conversation, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def session_message(session_id: str, body: SessionMessage, token=Depends(read_auth)):
        owner = chats.owner(token)
        if not body.query.strip():
            raise StoreError('请输入问题')
        session, run = chats.begin(owner, session_id, body.query, body.request_id, body.retry_turn_id)
        if not run:
            return session
        history = [{'query': turn['query'], 'answer': turn['result']['answer']} for turn in session['turns'][:-1]
                   if turn['result'] and turn['result']['status'] != 'insufficient_evidence']
        try:
            result = chat(ChatQuery(kb_id=session['kb_id'], release_id=session['release_id'],
                                    device_type=session['device_type'], query=body.query,
                                    history=history[-6:], top_k=body.top_k))
            result = wire.ChatAnswer.model_validate(result).model_dump(exclude_unset=True)
        except Exception as error:
            message = str(error) if isinstance(error, (ValueError, StoreError)) else '本轮生成失败，请重试或检查模型配置。'
            return chats.finish(owner, session_id, body.request_id, error=message)
        return chats.finish(owner, session_id, body.request_id, result=result)

    @app.get(prefix+'/rules', dependencies=read, response_model=wire.Rules, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
    def rules(kb_id: str, release_id: str | None = None, device_type: str | None = None):
        if device_type is not None and device_type not in DEVICE_TYPES:
            raise StoreError('设备类型不受支持')
        rel=active_release(kb_id,release_id)
        values=[{**e['rule'],'source':e['source'],'knowledge_id':e['id']} for e in rel['entries'] if e['kind']=='rule' and (device_type is None or e.get('device_type')==device_type)]
        return {'kb_id':kb_id,'release_id':rel['id'],'schema_version':wire.RULE_SCHEMA_VERSION,'rules':values,'count':len(values)}

    @app.get(prefix+'/knowledge/{knowledge_id}', dependencies=read, response_model=wire.KnowledgeDetail, response_model_exclude_unset=True, responses=wire.ERROR_RESPONSES)
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

import hashlib
import json
import os
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response
from fastapi.responses import FileResponse
from pydantic import Field
from .schema import Strict, RelationType
from .store import FusionStore
from .sources import snapshots
from .pipeline import Pipeline, profile
from .evaluation import evaluate, annotations, validate_labels
from ..store import StoreError
from .. import api_contract as wire


class ProjectInput(Strict):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")


class Selection(Strict):
    document_id: str
    revision: int = Field(ge=1)
    edit_version: int = Field(ge=1)


class RunInput(Strict):
    documents: list[Selection] = Field(min_length=1, max_length=50)
    top_k: int = Field(default=5, ge=1, le=50)
    same_document: bool = False


class SummaryInput(Strict):
    topic_id: str


class LabelInput(Strict):
    claim_a: str
    claim_b: str
    relation: RelationType


class LabelsInput(Strict):
    labels: list[LabelInput] = Field(max_length=20000)


def install(app, source_store, embedder, model_factory, admin_auth):
    enabled = os.environ.get("RAG_FUSION_ENABLED", "false").lower() == "true"
    router = APIRouter(
        prefix="/api/v1/admin/knowledge-fusion",
        dependencies=[Depends(admin_auth)],
        tags=["Knowledge fusion"],
    )

    @router.get("/status", response_model=wire.FusionFeature)
    def feature():
        return {"enabled": enabled}

    if not enabled:
        app.include_router(router)
        return
    store = FusionStore(source_store.root)
    pipeline = Pipeline(store, embedder, model_factory)
    app.state.fusion_store = store

    @router.get("/projects", response_model=list[wire.FusionProject])
    def projects():
        return store.projects()

    @router.post("/projects", response_model=wire.FusionProject)
    def create_project(body: ProjectInput):
        return store.project(body.name.strip())

    @router.get("/projects/{project_id}/runs", response_model=list[wire.FusionRun])
    def runs(project_id: str):
        return store.runs(project_id)

    @router.post(
        "/projects/{project_id}/runs", response_model=wire.FusionRun, status_code=202
    )
    def create_run(project_id: str, body: RunInput, background: BackgroundTasks):
        documents, spans = snapshots(source_store, body.documents)
        data = dict(
            documents=documents,
            spans=spans,
            top_k=body.top_k,
            same_document=body.same_document,
            profile=profile(model_factory(), embedder),
        )
        run = store.create(project_id, data)
        background.add_task(pipeline.run, run["id"])
        return run

    @router.get("/runs/{run_id}", response_model=wire.FusionRun)
    def run(run_id: str):
        return store.get(run_id)

    @router.post("/runs/{run_id}/retry", response_model=wire.FusionRun, status_code=202)
    def retry(run_id: str, background: BackgroundTasks):
        saved = store.get(run_id)
        if saved["profile"] != profile(model_factory(), embedder):
            raise StoreError("历史模型或 Prompt 配置不可用，请恢复配置或新建运行", 409)
        result = store.reserve(run_id, ["failed", "interrupted"])
        background.add_task(pipeline.run, run_id)
        return result

    def register_collection(name, model):
        def collection(
            run_id: str,
            offset: int = Query(0, ge=0),
            limit: int = Query(100, ge=1, le=500),
        ):
            return store.get(run_id)[name][offset : offset + limit]

        router.add_api_route(
            "/runs/{run_id}/" + name,
            collection,
            methods=["GET"],
            response_model=list[model],
            name="fusion_" + name,
        )

    for name, model in [
        ("documents", wire.FusionDocument),
        ("spans", wire.FusionSpan),
        ("claims", wire.FusionClaim),
        ("candidates", wire.FusionCandidate),
        ("relations", wire.FusionRelation),
        ("knowledge", wire.FusionKnowledge),
        ("concepts", wire.FusionConcept),
        ("topics", wire.FusionTopic),
        ("summaries", wire.FusionSummary),
    ]:
        register_collection(name, model)

    @router.post(
        "/runs/{run_id}/summaries", response_model=wire.FusionRun, status_code=202
    )
    def summarize(run_id: str, body: SummaryInput, background: BackgroundTasks):
        run = store.get(run_id)
        if body.topic_id not in {t["id"] for t in run["topics"]}:
            raise StoreError("主题不存在", 404)
        result = store.reserve(
            run_id, ["succeeded"], pending_topic=body.topic_id, stage="summary"
        )
        background.add_task(pipeline.run, run_id)
        return result

    @router.get("/runs/{run_id}/evaluation", response_model=wire.FusionEvaluation)
    def evaluation(run_id: str):
        return evaluate(store.get(run_id))

    @router.get(
        "/runs/{run_id}/annotations", response_model=list[wire.FusionAnnotation]
    )
    def export(run_id: str, claim_ids: list[str] | None = Query(None)):
        return annotations(store.get(run_id), claim_ids)

    @router.get("/runs/{run_id}/annotations.jsonl")
    def export_jsonl(run_id: str, claim_ids: list[str] | None = Query(None)):
        rows = [
            wire.FusionAnnotation.model_validate(row).model_dump()
            for row in annotations(store.get(run_id), claim_ids)
        ]
        return Response(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="annotations.jsonl"'},
        )

    @router.put("/runs/{run_id}/annotations", response_model=list[wire.FusionLabel])
    def import_labels(run_id: str, body: LabelsInput):
        run = store.get(run_id)
        if run["status"] in ("queued", "processing"):
            raise StoreError("请在任务完成后导入标注", 409)
        labels = validate_labels(run, [l.model_dump() for l in body.labels])
        store.update(run_id, labels=labels)
        return labels

    def original_path(run_id, document_id):
        run = store.get(run_id)
        doc = next(
            (d for d in run["documents"] if d["document_id"] == document_id), None
        )
        if doc is None:
            raise StoreError("运行中不存在此文档", 404)
        path = (
            source_store.root
            / "sources"
            / (doc["file_hash"] + Path(doc["file_name"]).suffix.lower())
        )
        if (
            not path.exists()
            or hashlib.sha256(path.read_bytes()).hexdigest() != doc["file_hash"]
        ):
            raise StoreError("历史原件缺失或哈希不匹配", 404)
        return path, doc

    @router.get("/runs/{run_id}/documents/{document_id}/original")
    def original(run_id: str, document_id: str):
        path, doc = original_path(run_id, document_id)
        return FileResponse(path, filename=doc["file_name"])

    @router.get("/runs/{run_id}/documents/{document_id}/pages/{page}")
    def page_image(run_id: str, document_id: str, page: int):
        import io
        import pypdfium2 as pdfium
        from ..ocr import _PDF_LOCK

        path, doc = original_path(run_id, document_id)
        if path.suffix.lower() != ".pdf" or page < 1:
            raise StoreError("请选择 PDF 的有效物理页")
        with _PDF_LOCK:
            pdf = pdfium.PdfDocument(path)
            try:
                if page > len(pdf):
                    raise StoreError("页码超出范围", 404)
                image = pdf[page - 1].render(scale=1.5).to_pil()
                output = io.BytesIO()
                image.save(output, format="PNG")
                return Response(output.getvalue(), media_type="image/png")
            finally:
                pdf.close()

    app.include_router(router)

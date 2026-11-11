"""Opt-in live smoke test on five synthetic short documents, isolated in a temp DB."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.documents import parse_document
from rag_service.models import load_dotenv, ModelConfig

CORPUS = [
    "在输入相同且计算逻辑不变时，缓存可以复用先前计算结果。",
    "当输入相同、计算逻辑保持不变时，可直接复用缓存中的计算结果。",
    "在输入或计算逻辑发生变化后，应使已有缓存失效。",
    "缓存占用额外存储空间。",
    "在输入或计算逻辑发生变化后，已有缓存仍然可以直接复用。",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        required=True,
        help="Use the locally configured LLM and embedding provider; incurs provider charges",
    )
    parser.add_argument("--output", default="outputs/fusion-validation.json")
    args = parser.parse_args()
    load_dotenv()
    os.environ["RAG_FUSION_ENABLED"] = "true"
    try:
        ModelConfig.from_env()
    except ValueError as error:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        report = dict(
            status="configuration_error",
            error=str(error),
            corpus_kind="synthetic short documents; live validation not executed",
        )
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
        return 1
    prefix = "/api/v1/admin/knowledge-fusion"
    with tempfile.TemporaryDirectory() as folder:
        app = create_app(
            Settings(Path(folder), "smoke-admin-token", "smoke-read-token")
        )
        store = app.state.store
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer smoke-admin-token"
            selected = []
            for index, text in enumerate(CORPUS):
                kb = store.create_kb("合成资料来源" + str(index))["id"]
                content = text.encode()
                upload = store.reserve_upload(kb, "synthetic.md", content)
                doc = store.finish_upload(
                    upload["document_id"],
                    1,
                    parse_document(content, "synthetic.md", document_type="reference"),
                )
                selected.append(
                    dict(
                        document_id=doc["id"],
                        revision=1,
                        edit_version=doc["edit_version"],
                    )
                )
            project = client.post(
                prefix + "/projects", json={"name": "五份合成短资料的真实模型验证"}
            ).json()
            response = client.post(
                prefix + "/projects/" + project["id"] + "/runs",
                json={"documents": selected},
            )
            response.raise_for_status()
            run_id = response.json()["id"]
            run = app.state.fusion_store.get(run_id)
            if run["status"] == "succeeded" and run["topics"]:
                response = client.post(
                    prefix + "/runs/" + run_id + "/summaries",
                    json={"topic_id": run["topics"][0]["id"]},
                )
                response.raise_for_status()
                run = app.state.fusion_store.get(run_id)
            report = dict(
                corpus_kind="synthetic short documents, real configured providers",
                corpus=CORPUS,
                status=run["status"],
                error=run["error"],
                profile=run["profile"],
                evaluation=client.get(
                    prefix + "/runs/" + run_id + "/evaluation"
                ).json(),
                claims=run["claims"],
                relations=run["relations"],
                knowledge=run["knowledge"],
                concepts=run["concepts"],
                topics=run["topics"],
                summaries=run["summaries"],
            )
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                json.dumps(
                    dict(
                        status=run["status"],
                        error=run["error"],
                        evaluation=report["evaluation"],
                        output=str(output),
                    ),
                    ensure_ascii=False,
                )
            )
            return 0 if run["status"] == "succeeded" and run["summaries"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

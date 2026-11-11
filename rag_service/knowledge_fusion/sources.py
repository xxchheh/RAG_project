"""Freeze source revisions without modifying the source RAG database."""

import hashlib
import json
from ..store import StoreError


def snapshots(store, selections):
    documents, spans = [], []
    if len({s.document_id for s in selections}) != len(selections):
        raise StoreError("文档选择重复")
    with store.connection() as c:
        c.execute("BEGIN")
        for selected in selections:
            row = c.execute(
                "SELECT d.*,r.*,k.archived FROM documents d JOIN revisions r ON r.document_id=d.id AND r.revision=d.latest_revision JOIN kbs k ON k.id=d.kb_id WHERE d.id=?",
                (selected.document_id,),
            ).fetchone()
            if (
                row is None
                or row["archived"]
                or row["disabled"]
                or row["status"] != "ready"
            ):
                raise StoreError("请选择有效知识库中已解析的启用文档", 409)
            if (
                row["latest_revision"] != selected.revision
                or row["edit_version"] != selected.edit_version
            ):
                raise StoreError("所选文档版本已变化，请刷新", 409)
            payload = json.loads(row["payload"])
            documents.append(
                dict(
                    document_id=selected.document_id,
                    kb_id=row["kb_id"],
                    title=row["title"],
                    revision=selected.revision,
                    edit_version=selected.edit_version,
                    file_hash=row["file_hash"],
                    file_name=row["file_name"],
                    parser_version=payload.get("parser_version", "legacy"),
                )
            )
            for block in payload["blocks"]:
                types = block.get("content_types", [])
                excluded = bool(
                    block.get("excluded")
                    or block.get("page") in payload.get("excluded_pages", [])
                    or "unresolved_picture" in types
                    or ("formula" in types and block.get("kind") == "uncertain")
                )
                text = block.get("source_text", block["text"])
                # Long spans retain exact offsets. Tables repeat their header in window context only.
                start = 0
                while start < len(text):
                    end = min(start + 5000, len(text))
                    if end < len(text):
                        boundary = text.rfind("\n", start + 2500, end)
                        if boundary > start:
                            end = boundary + 1
                    part = text[start:end]
                    key = hashlib.sha256(
                        f'{selected.document_id}:{selected.revision}:{selected.edit_version}:{block["id"]}:{start}'.encode()
                    ).hexdigest()[:24]
                    spans.append(
                        dict(
                            id="span-" + key,
                            document_id=selected.document_id,
                            block_id=block["id"],
                            text=part,
                            corrected_text=block["text"],
                            start=start,
                            end=end,
                            section=block.get("section", ""),
                            page=block.get("page"),
                            locator=block["locator"],
                            bbox=block.get("bbox", []),
                            extraction=block.get("extraction", ""),
                            excluded=excluded,
                            table_header=(
                                "\n".join(text.splitlines()[:2])
                                if "table" in types
                                else ""
                            ),
                        )
                    )
                    start = end
    return documents, spans


def windows(spans, limit=6000):
    result, group, size = [], [], 0
    for span in spans:
        if span["excluded"] or not span["text"].strip():
            continue
        boundary = group and (
            group[-1]["document_id"] != span["document_id"]
            or group[-1]["section"] != span["section"]
        )
        if group and (boundary or size + len(span["text"]) > limit):
            result.append(group)
            group = (
                [group[-1]] if not boundary and len(group[-1]["text"]) <= 1000 else []
            )
            size = sum(len(s["text"]) for s in group)
        group.append(span)
        size += len(span["text"])
    if group:
        result.append(group)
    return result

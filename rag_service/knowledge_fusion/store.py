"""Transactional JSON snapshots in an isolated SQLite database."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from ..store import StoreError
from ..common import now


def identifier(prefix):
    return prefix + "-" + uuid4().hex


class FusionStore:
    def __init__(self, root):
        root = Path(root) / "fusion"
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "knowledge_fusion.db"
        with self.connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), status TEXT NOT NULL, body TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS active_project ON runs(project_id) WHERE status IN ('queued','processing');
            """)
            rows = c.execute(
                "SELECT id,body FROM runs WHERE status IN ('queued','processing')"
            ).fetchall()
            for row in rows:
                body = json.loads(row["body"])
                body.update(
                    status="interrupted", error="服务重启中断任务，可恢复检查点"
                )
                c.execute(
                    "UPDATE runs SET status=?,body=? WHERE id=?",
                    ("interrupted", json.dumps(body, ensure_ascii=False), row["id"]),
                )

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=20) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            yield c

    def projects(self):
        with self.connection() as c:
            return [
                dict(r) for r in c.execute("SELECT * FROM projects ORDER BY rowid DESC")
            ]

    def project(self, name):
        result = dict(id=identifier("project"), name=name, created_at=now())
        with self.connection() as c:
            c.execute("INSERT INTO projects VALUES (:id,:name,:created_at)", result)
        return result

    def get(self, run_id):
        with self.connection() as c:
            row = c.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError("融合运行不存在", 404)
        return json.loads(row["body"])

    def runs(self, project_id):
        with self.connection() as c:
            return [
                json.loads(r["body"])
                for r in c.execute(
                    "SELECT body FROM runs WHERE project_id=? ORDER BY rowid DESC",
                    (project_id,),
                )
            ]

    def create(self, project_id, data):
        data.update(
            id=identifier("run"),
            project_id=project_id,
            status="queued",
            stage="snapshot",
            created_at=now(),
            error=None,
            claims=[],
            candidates=[],
            relations=[],
            knowledge=[],
            concepts=[],
            topics=[],
            summaries=[],
            labels=[],
            checkpoints={},
            pending_topic=None,
        )
        try:
            with self.connection() as c:
                c.execute(
                    "INSERT INTO runs VALUES (?,?,?,?)",
                    (
                        data["id"],
                        project_id,
                        data["status"],
                        json.dumps(data, ensure_ascii=False, allow_nan=False),
                    ),
                )
        except sqlite3.IntegrityError:
            raise StoreError("项目不存在或已有活动任务", 409) from None
        return data

    def update(self, run_id, **fields):
        try:
            with self.connection() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    "SELECT body FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise StoreError("融合运行不存在", 404)
                body = json.loads(row["body"])
                body.update(fields)
                c.execute(
                    "UPDATE runs SET status=?,body=? WHERE id=?",
                    (
                        body["status"],
                        json.dumps(body, ensure_ascii=False, allow_nan=False),
                        run_id,
                    ),
                )
        except sqlite3.IntegrityError:
            raise StoreError("项目已有活动任务", 409) from None
        return body

    def reserve(self, run_id, allowed, **fields):
        try:
            with self.connection() as c:
                c.execute("BEGIN IMMEDIATE")
                row = c.execute(
                    "SELECT body FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise StoreError("融合运行不存在", 404)
                body = json.loads(row["body"])
                if body["status"] not in allowed:
                    raise StoreError("当前状态不能执行此操作", 409)
                body.update(fields, status="queued", error=None)
                c.execute(
                    "UPDATE runs SET status=?,body=? WHERE id=?",
                    ("queued", json.dumps(body, ensure_ascii=False), run_id),
                )
        except sqlite3.IntegrityError:
            raise StoreError("项目已有活动任务", 409) from None
        return body

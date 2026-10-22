"""Versioned, local rule knowledge store.

Rule bodies and their source documents are immutable. Publication and enablement
live separately, so an analysis can retain a complete, reproducible snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .common import DemoError, ROOT, now, read_json

_MANAGEMENT = {
    "revision", "status", "enabled", "active", "source", "created_at",
    "published_at", "published_revision", "has_draft", "review_confirmed",
}


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise DemoError("规则及来源必须是可保存的 JSON 数据，不能包含无效数值。") from exc


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class KnowledgeStore:
    def __init__(self, path=None, seed=True):
        self.path = Path(path or os.environ.get("DIAGNOSTIC_KNOWLEDGE_DB") or Path.cwd() / "knowledge" / "knowledge.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        base = read_json(ROOT / "config" / "rules.json")
        with self._connection(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rules (
                id TEXT PRIMARY KEY, device_type TEXT NOT NULL, name TEXT NOT NULL,
                published_revision INTEGER, enabled INTEGER NOT NULL DEFAULT 0,
                UNIQUE(device_type, name))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rule_revisions (
                rule_id TEXT NOT NULL REFERENCES rules(id), revision INTEGER NOT NULL,
                body TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(rule_id, revision))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS publications (
                rule_id TEXT NOT NULL, revision INTEGER NOT NULL,
                published_at TEXT NOT NULL, review_confirmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(rule_id, revision),
                FOREIGN KEY(rule_id, revision) REFERENCES rule_revisions(rule_id, revision))""")
            for key, value in (("conventions", base["conventions"]), ("original_version", base["version"])):
                conn.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES (?,?)", (key, _json(value)))
            seeded = conn.execute("SELECT 1 FROM metadata WHERE key='seed_completed'").fetchone()
            if seed and not seeded:
                # A nonempty user-created store must never be overwritten or seeded later.
                if not conn.execute("SELECT 1 FROM rules LIMIT 1").fetchone():
                    for rule in base["rules"]:
                        source = {**base["source"], "excerpt": rule["original_text"]}
                        draft = self._create(conn, rule, source)
                        self._publish(conn, draft["id"], draft["revision"], confirm_notes=True)
                conn.execute("INSERT INTO metadata(key,value) VALUES ('seed_completed','true')")

    @contextmanager
    def _connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _identity(conn, rule_id):
        row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise DemoError(f"未找到规则 {rule_id}。")
        return row

    @staticmethod
    def _matching_ids(conn, device_type, name):
        """Match the published name and latest draft, never older draft names."""
        rows = conn.execute("""SELECT r.id, published.body AS published_body,
            latest.body AS latest_body FROM rules r
            LEFT JOIN rule_revisions published ON published.rule_id=r.id
                AND published.revision=r.published_revision
            LEFT JOIN rule_revisions latest ON latest.rule_id=r.id
                AND latest.revision=(SELECT MAX(revision) FROM rule_revisions WHERE rule_id=r.id)
            ORDER BY r.id""").fetchall()
        matches = set()
        for row in rows:
            for key in ("published_body", "latest_body"):
                if row[key] is not None:
                    body = json.loads(row[key])
                    if body["device_type"] == device_type and body["name"] == name:
                        matches.add(row["id"])
        return matches

    def _record(self, conn, rule_id, revision=None):
        identity = self._identity(conn, rule_id)
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
            raise DemoError("规则版本必须是大于零的整数。")
        row = conn.execute(
            "SELECT * FROM rule_revisions WHERE rule_id=? " +
            ("ORDER BY revision DESC LIMIT 1" if revision is None else "AND revision=?"),
            (rule_id,) if revision is None else (rule_id, revision),
        ).fetchone()
        if row is None:
            raise DemoError(f"未找到规则 {rule_id} 的版本 V{revision}。")
        publication = conn.execute("SELECT * FROM publications WHERE rule_id=? AND revision=?", (rule_id, row["revision"])).fetchone()
        return {
            **json.loads(row["body"]), "revision": row["revision"],
            "source": json.loads(row["source"]), "created_at": row["created_at"],
            "status": "published" if publication else "draft",
            "enabled": bool(identity["enabled"]),
            "active": bool(identity["enabled"]) and row["revision"] == identity["published_revision"],
            "published_revision": identity["published_revision"],
            "published_at": publication["published_at"] if publication else None,
            "review_confirmed": bool(publication["review_confirmed"]) if publication else False,
        }

    def get_rule(self, rule_id, revision=None):
        with self._connection() as conn:
            return self._record(conn, rule_id, revision)

    def list_rules(self, device_type=None):
        with self._connection() as conn:
            rows = conn.execute("SELECT id FROM rules ORDER BY id").fetchall()
            result = []
            for row in rows:
                record = self._record(conn, row["id"])
                if device_type is not None and record["device_type"] != device_type:
                    continue
                result.append({key: record[key] for key in (
                    "id", "device_type", "name", "revision", "status", "enabled", "published_revision", "source",
                )} | {"has_draft": record["status"] == "draft"})
            return result

    def _create(self, conn, rule, source=None):
        from .rule_validation import normalize_rule

        if not isinstance(rule, dict):
            raise DemoError("每条规则必须是一个 JSON 对象。")
        body = {key: copy.deepcopy(value) for key, value in rule.items() if key not in _MANAGEMENT}
        rule_id = body.get("id")
        if rule_id is not None and (not isinstance(rule_id, str) or not re.fullmatch(r"R\d{3,}", rule_id) or int(rule_id[1:]) < 1):
            raise DemoError("规则编号须为 R001、R017 等格式。")
        body = normalize_rule(body)
        matching_ids = self._matching_ids(conn, body["device_type"], body["name"])
        if len(matching_ids) > 1:
            raise DemoError("规则名称同时匹配多个已发布或草稿规则，请明确规则编号并解决名称冲突。")
        matching_id = next(iter(matching_ids), None)
        if matching_id and rule_id and matching_id != rule_id:
            raise DemoError(f"相同设备类型下已存在同名规则 {matching_id}，不能重复创建。")
        if not rule_id:
            if matching_id:
                rule_id = matching_id
            else:
                all_ids = conn.execute("SELECT id FROM rules").fetchall()
                rule_id = f"R{max((int(row['id'][1:]) for row in all_ids), default=0) + 1:03d}"
        body["id"] = rule_id
        body = normalize_rule(body)
        existing = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if existing:
            previous = self._record(conn, rule_id)
            canonical = self._record(conn, rule_id, existing["published_revision"]) if existing["published_revision"] else previous
            if body["device_type"] != canonical["device_type"]:
                raise DemoError("同一规则编号不能更换设备类型，请为其他设备类型新建规则。")
            revision = previous["revision"] + 1
        else:
            previous, revision = None, 1
        if source is None:
            source = rule.get("source") or (previous["source"] if previous else None)
        if source is None:
            source = {"title": "手工维护机理规则", "date": now()[:10], "sha256": _digest(body), "excerpt": body.get("original_text", "")}
        if not isinstance(source, dict):
            raise DemoError("规则来源必须是 JSON 对象。")
        source = copy.deepcopy(source)
        source.setdefault("title", "机理规则资料")
        source.setdefault("date", now()[:10])
        source.setdefault("excerpt", body.get("original_text", ""))
        source.setdefault("sha256", _digest(source))
        if not all(isinstance(source.get(key), str) and source[key].strip() for key in ("title", "date", "sha256")):
            raise DemoError("规则来源的 title、date、sha256 必须是非空文本。")
        encoded_body, encoded_source = _json(body), _json(source)
        try:
            if existing and existing["published_revision"] is None:
                # Only unpublished rules use their latest draft as their identity.
                conn.execute("UPDATE rules SET device_type=?,name=? WHERE id=?", (body["device_type"], body["name"], rule_id))
            elif not existing:
                conn.execute("INSERT INTO rules(id,device_type,name) VALUES (?,?,?)", (rule_id, body["device_type"], body["name"]))
            conn.execute("INSERT INTO rule_revisions(rule_id,revision,body,source,created_at) VALUES (?,?,?,?,?)", (rule_id, revision, encoded_body, encoded_source, now()))
        except sqlite3.IntegrityError as exc:
            raise DemoError("规则编号或同设备类型下的名称冲突。") from exc
        return self._record(conn, rule_id, revision)

    def create_draft(self, rule, source=None):
        return self.create_drafts([rule], source)[0]

    def create_drafts(self, rules, source=None):
        if not isinstance(rules, list) or not rules:
            raise DemoError("待导入规则必须是非空列表。")
        with self._connection(write=True) as conn:
            result, ids = [], set()
            for rule in rules:
                record = self._create(conn, rule, source)
                if record["id"] in ids:
                    raise DemoError(f"同一批导入包含重复规则 {record['id']}。")
                ids.add(record["id"])
                result.append(record)
            return result

    def _publish(self, conn, rule_id, revision=None, confirm_notes=False):
        from .rule_validation import normalize_rule

        record = self._record(conn, rule_id, revision)
        normalize_rule({key: value for key, value in record.items() if key not in _MANAGEMENT})
        if record.get("review_notes") and not confirm_notes:
            raise DemoError("规则含待复核说明，请核对原文及 review_notes 后显式确认再发布。")
        active_revision = record["published_revision"]
        if active_revision is not None and record["revision"] < active_revision:
            raise DemoError("不能发布比当前已发布版本更旧的规则；请新建修订版。")
        conflicting_ids = self._matching_ids(conn, record["device_type"], record["name"]) - {rule_id}
        if conflicting_ids:
            raise DemoError("规则名称与其他已发布或草稿规则冲突，不能发布。")
        conn.execute("INSERT OR IGNORE INTO publications(rule_id,revision,published_at,review_confirmed) VALUES (?,?,?,?)", (rule_id, record["revision"], now(), int(confirm_notes)))
        conn.execute("UPDATE rules SET published_revision=?,enabled=1,device_type=?,name=? WHERE id=?", (record["revision"], record["device_type"], record["name"], rule_id))
        return self._record(conn, rule_id, record["revision"])

    def publish(self, rule_id, revision=None, confirm_notes=False):
        with self._connection(write=True) as conn:
            return self._publish(conn, rule_id, revision, confirm_notes)

    def disable(self, rule_id):
        with self._connection(write=True) as conn:
            identity = self._identity(conn, rule_id)
            conn.execute("UPDATE rules SET enabled=0 WHERE id=?", (rule_id,))
            return self._record(conn, rule_id, identity["published_revision"])

    def history(self, rule_id):
        with self._connection() as conn:
            self._identity(conn, rule_id)
            rows = conn.execute("SELECT revision FROM rule_revisions WHERE rule_id=? ORDER BY revision", (rule_id,)).fetchall()
            return [self._record(conn, rule_id, row["revision"]) for row in rows]

    def diff(self, rule_id, revision=None):
        with self._connection() as conn:
            candidate = self._record(conn, rule_id, revision)
            baseline = self._record(conn, rule_id, candidate["published_revision"]) if candidate["published_revision"] is not None else None
            before = {key: value for key, value in (baseline or {}).items() if key not in _MANAGEMENT} if baseline else {}
            after = {key: value for key, value in candidate.items() if key not in _MANAGEMENT}
            # Source changes matter even if the executable conditions are unchanged.
            if baseline:
                before["source"] = baseline["source"]
            after["source"] = candidate["source"]
            changes = []

            def walk(left, right, path=""):
                if isinstance(left, dict) and isinstance(right, dict):
                    for key in sorted(set(left) | set(right)):
                        walk(left.get(key), right.get(key), f"{path}.{key}" if path else key)
                elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
                    for index, (old, new) in enumerate(zip(left, right)):
                        walk(old, new, f"{path}[{index}]")
                elif left != right:
                    changes.append({"path": path, "before": left, "after": right})

            walk(before, after)
            return {"id": rule_id, "revision": candidate["revision"], "published_revision": candidate["published_revision"], "changes": changes}

    def snapshot(self, device_type=None):
        with self._connection() as conn:
            rows = conn.execute("SELECT id,published_revision FROM rules WHERE enabled=1 AND published_revision IS NOT NULL ORDER BY id").fetchall()
            rules = []
            for row in rows:
                record = self._record(conn, row["id"], row["published_revision"])
                if device_type is not None and record["device_type"] != device_type:
                    continue
                rules.append({key: value for key, value in record.items() if key not in _MANAGEMENT} | {"revision": record["revision"], "source": record["source"]})
            conventions = json.loads(conn.execute("SELECT value FROM metadata WHERE key='conventions'").fetchone()["value"])
            content_hash = _digest({"conventions": conventions, "rules": rules})
            sources = {_json(rule["source"]): rule["source"] for rule in rules}
            return {
                "version": f"knowledge-{content_hash[:16]}", "snapshot_id": f"kb-{content_hash}",
                "conventions": conventions, "rules": rules,
                "source": {"title": "版本化机理规则知识库", "date": now()[:10], "sha256": content_hash},
                "sources": list(sources.values()),
            }

    def search(self, query, device_type=None):
        if not isinstance(query, str):
            raise DemoError("知识查询关键词必须是文本。")
        keywords = [word.casefold() for word in query.split() if word]
        results = []
        for rule in self.snapshot(device_type)["rules"]:
            text = " ".join(str(rule.get(key, "")) for key in (
                "id", "name", "device_type", "source_section", "original_text", "phenomena", "causes", "actions", "notes",
            )).casefold()
            if all(keyword in text for keyword in keywords):
                results.append(rule)
        return results

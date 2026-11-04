"""SQLite drafts, review decisions and immutable publication manifests."""
from __future__ import annotations
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from .common import now
from .contracts import METRICS, DERIVED, normalize_rule


def uid(prefix):
    return prefix + '-' + uuid4().hex[:16]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class StoreError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, root, recover_tasks=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'knowledge.db'
        with self.connection() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS kbs(id TEXT PRIMARY KEY,name TEXT NOT NULL,archived INTEGER DEFAULT 0,current_release TEXT,state_version INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,title TEXT NOT NULL,external_id TEXT,latest_revision INTEGER DEFAULT 0,disabled INTEGER DEFAULT 0,status TEXT DEFAULT 'queued',UNIQUE(kb_id,external_id));
            CREATE TABLE IF NOT EXISTS revisions(document_id TEXT,revision INTEGER,payload TEXT NOT NULL,description_status TEXT DEFAULT 'pending',edit_version INTEGER DEFAULT 1,file_hash TEXT,file_name TEXT,created_at TEXT,PRIMARY KEY(document_id,revision));
            CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY,kb_id TEXT,number INTEGER,created_at TEXT,body TEXT NOT NULL,UNIQUE(kb_id,number));
            CREATE TABLE IF NOT EXISTS audits(id TEXT PRIMARY KEY,document_id TEXT,revision INTEGER,target TEXT,decision TEXT,content_hash TEXT,note TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,status TEXT,stage TEXT,result TEXT,error TEXT,created_at TEXT);
            ''')
            if recover_tasks:
                c.execute("UPDATE documents SET status='failed' WHERE status IN ('queued','processing')")
                c.execute("UPDATE tasks SET status='failed',error='服务重启中断任务，请重新处理',stage='interrupted' WHERE status IN ('queued','processing')")

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _kb(self, c, kb_id, writable=False):
        row = c.execute('SELECT * FROM kbs WHERE id=?', (kb_id,)).fetchone()
        if row is None:
            raise StoreError('知识库不存在', 404)
        if writable and row['archived']:
            raise StoreError('知识库已归档，先取消归档再维护', 409)
        return dict(row)

    def list_kbs(self, include_archived=False):
        with self.connection() as c:
            return [dict(r) for r in c.execute('SELECT * FROM kbs ORDER BY rowid') if include_archived or not r['archived']]

    def create_kb(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise StoreError('知识库名称为1至100个字符')
        kb_id = uid('kb')
        with self.connection() as c:
            c.execute('INSERT INTO kbs(id,name) VALUES (?,?)', (kb_id, name.strip()))
            return self._kb(c, kb_id)

    def archive_kb(self, kb_id, archived):
        with self.connection() as c:
            self._kb(c, kb_id)
            c.execute('UPDATE kbs SET archived=?,state_version=state_version+1 WHERE id=?', (int(archived), kb_id))
            return self._kb(c, kb_id)

    def task(self, task_id):
        with self.connection() as c:
            row = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row:
                raise StoreError('任务不存在', 404)
            result = dict(row)
            result['result'] = json.loads(result['result']) if result['result'] else None
            return result

    def new_task(self):
        key = uid('task')
        with self.connection() as c:
            c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)', (key, 'queued', 'queued', None, None, now()))
        return key

    def set_task(self, key, status, stage, result=None, error=None):
        with self.connection() as c:
            c.execute('UPDATE tasks SET status=?,stage=?,result=?,error=? WHERE id=?', (status, stage, encoded(result) if result is not None else None, error, key))

    def reserve_upload(self, kb_id, title, content, external_id=None, document_id=None, document_type="auto", force=False):
        if document_type not in ('auto', 'rules', 'reference'):
            raise StoreError('不支持的文档类型')
        if not content or len(content) > 100*1024*1024:
            raise StoreError('文件为空或超过100 MiB')
        if not isinstance(title, str) or len(title) > 240:
            raise StoreError('文件名过长')
        suffix = Path(title).suffix.lower()
        if suffix not in ('.md', '.docx', '.pdf', '.pptx'):
            raise StoreError('仅支持.md、.docx、文字型.pdf、.pptx；.ppt请先另存为.pptx')
        sha = hashlib.sha256(content).hexdigest()
        folder = self.root / 'sources'
        folder.mkdir(exist_ok=True)
        original = folder / (sha + suffix)
        if original.exists() and hashlib.sha256(original.read_bytes()).hexdigest() != sha:
            raise StoreError('归档文件哈希异常')
        if not original.exists():
            original.write_bytes(content)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self._kb(c, kb_id, True)
            if external_id and not document_id:
                found = c.execute('SELECT id FROM documents WHERE kb_id=? AND external_id=?', (kb_id, external_id)).fetchone()
                document_id = found['id'] if found else None
            if document_id:
                doc = self._doc(c, document_id)
                if doc['kb_id'] != kb_id:
                    raise StoreError('文档不属于指定知识库')
                if doc['status'] in ('queued', 'processing'):
                    raise StoreError('该文档仍有处理任务，请等待完成', 409)
                previous = c.execute('SELECT file_hash FROM revisions WHERE document_id=? AND revision=?', (document_id, doc['latest_revision'])).fetchone()
                if not force and previous and previous['file_hash'] == sha and doc['status'] == 'ready' and document_type in ('auto', doc['payload'].get('document_type', 'auto')):
                    return {'document_id': document_id, 'revision': doc['latest_revision'], 'unchanged': True}
                revision = doc['latest_revision'] + 1
                c.execute("UPDATE documents SET latest_revision=?,title=?,status='queued',disabled=0 WHERE id=?", (revision, title, document_id))
            else:
                document_id, revision = uid('doc'), 1
                c.execute('INSERT INTO documents(id,kb_id,title,external_id,latest_revision) VALUES (?,?,?,?,?)', (document_id, kb_id, title, external_id or None, revision))
            c.execute('INSERT INTO revisions(document_id,revision,payload,file_hash,file_name,created_at) VALUES (?,?,?,?,?,?)', (document_id, revision, encoded({'blocks': [], 'rules': [], 'warnings': [], 'document_type': document_type}), sha, title, now()))
            c.execute('UPDATE kbs SET state_version=state_version+1 WHERE id=?', (kb_id,))
        return {'document_id': document_id, 'revision': revision, 'unchanged': False}

    def _doc(self, c, document_id):
        row = c.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
        if not row:
            raise StoreError('文档不存在', 404)
        rev = c.execute('SELECT * FROM revisions WHERE document_id=? AND revision=?', (document_id, row['latest_revision'])).fetchone()
        return {**dict(row), **{k: rev[k] for k in ('description_status', 'edit_version', 'file_hash', 'file_name', 'created_at')}, 'payload': json.loads(rev['payload'])}

    def document(self, document_id):
        with self.connection() as c:
            return self._doc(c, document_id)

    def documents(self, kb_id):
        with self.connection() as c:
            self._kb(c, kb_id)
            return [{**{k: v for k, v in doc.items() if k != 'payload'}, 'document_type': doc['payload'].get('document_type', 'auto')} for row in c.execute('SELECT id FROM documents WHERE kb_id=? ORDER BY rowid', (kb_id,)).fetchall() for doc in [self._doc(c, row['id'])]]

    @staticmethod
    def _rule(rule, blocks):
        rule = copy.deepcopy(rule)
        if not rule.get('retired'):
            from .documents import validate_candidate
            rule = validate_candidate(rule, blocks)
        errors = list(rule.get('validation_errors', [])) if not rule.get('retired') else []
        try:
            canonical = normalize_rule(rule)
            metrics = []
            def walk(node):
                if 'metric' in node:
                    key = node['metric']
                    unit = METRICS[key][1] if key in METRICS else '1'
                    if node.get('unit', unit) != unit:
                        raise ValueError('指标单位不符：' + key)
                    metrics.append((key, unit))
                else:
                    for child in node.get('all', node.get('any', [])):
                        walk(child)
            walk(canonical['condition'])
            expected = dict(metrics)
            supplied = rule.get('units', expected)
            if not isinstance(supplied, dict) or any(supplied.get(k) != v for k, v in expected.items()):
                raise ValueError('规则指标单位与能力契约不一致')
            rule.update(canonical)
            rule['units'] = expected
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
        ids = rule.get('block_ids', [])
        if not rule.get('retired') and (not ids or not set(ids) <= {b['id'] for b in blocks}):
            errors.append('规则必须关联有效的原文块')
        rule['valid'], rule['validation_errors'] = not errors, errors
        return rule

    def finish_upload(self, document_id, revision, payload):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = self._doc(c, document_id)
            if doc['latest_revision'] != revision:
                raise StoreError('文档修订版已变化', 409)
            payload = copy.deepcopy(payload)
            payload.setdefault('document_type', doc['payload'].get('document_type', 'auto'))
            previous = c.execute('SELECT payload FROM revisions WHERE document_id=? AND revision=?', (document_id, revision-1)).fetchone()
            old_rules = json.loads(previous['payload'])['rules'] if previous else []
            names = {r['name']: r for r in old_rules}
            assigned = set()
            for block in payload['blocks']:
                block['source_text'] = block['text']
            for rule in payload['rules']:
                old = names.get(rule['name'])
                rule['id'] = old['id'] if old else rule.get('id') or uid('rule')
                if rule['id'] in assigned:
                    raise StoreError('候选规则编号重复')
                assigned.add(rule['id'])
                rule['knowledge_revision'] = old.get('knowledge_revision', 1)+1 if old else 1
                rule['review_status'] = 'pending'
            # Removed rules require an explicit individual retirement review.
            for old in old_rules:
                if old['id'] not in assigned:
                    payload['rules'].append({**old, 'retired': True, 'block_ids': [], 'review_status': 'pending', 'knowledge_revision': old.get('knowledge_revision', 1)+1})
            payload['rules'] = [self._rule(r, payload['blocks']) for r in payload['rules']]
            c.execute("UPDATE revisions SET payload=?,edit_version=edit_version+1 WHERE document_id=? AND revision=?", (encoded(payload), document_id, revision))
            c.execute("UPDATE documents SET status='ready' WHERE id=?", (document_id,))
            c.execute('UPDATE kbs SET state_version=state_version+1 WHERE id=?', (doc['kb_id'],))
        return self.document(document_id)

    def fail_upload(self, document_id):
        with self.connection() as c:
            c.execute("UPDATE documents SET status='failed' WHERE id=?", (document_id,))

    def publication_status(self, document_id):
        doc = self.document(document_id)
        with self.connection() as c:
            kb = self._kb(c, doc['kb_id'])
        release = self.release(doc['kb_id']) if kb['current_release'] else {'entries': []}
        entries = [e for e in release['entries'] if e['source']['document_id'] == document_id]
        pending = doc['status'] != 'ready'
        if not pending and not kb['archived']:
            try:
                _, candidate = self._candidate(doc['kb_id'], document_id)
            except StoreError:
                # A staged rule-ID conflict must not prevent opening the editor.
                pending = True
            else:
                proposed = [e for e in candidate if e['source']['document_id'] == document_id]
                def comparable(values):
                    return {e['id']: {k:v for k,v in e.items() if k != 'knowledge_revision'} for e in values}
                pending = comparable(entries) != comparable(proposed)
        pending = pending or doc['description_status'] != 'approved' or any(rule['review_status'] != 'approved' for rule in doc['payload']['rules'])
        return {'release_id': kb['current_release'], 'entry_count': len(entries),
                'current_revision_count': sum(e['source']['document_revision'] == doc['latest_revision'] for e in entries),
                'pending_changes': pending}

    def edit_document(self, document_id, expected_version, blocks=None, rules=None):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = self._doc(c, document_id)
            self._kb(c, doc['kb_id'], True)
            if doc['status'] != 'ready' or expected_version != doc['edit_version']:
                raise StoreError('文档已变化或尚未解析完成，请刷新', 409)
            payload = doc['payload']
            old_blocks = {b['id']: b for b in payload['blocks']}
            if blocks is not None:
                if not isinstance(blocks, list) or len(blocks) != len(old_blocks) or {b.get('id') for b in blocks} != set(old_blocks):
                    raise StoreError('保留原文块编号，不能增删原文定位')
                for b in blocks:
                    if b.get('kind') not in ('description', 'rule', 'uncertain') or not isinstance(b.get('text'), str) or len(b['text']) > 50000:
                        raise StoreError('无效的原文块')
                    b['source_text'], b['locator'] = old_blocks[b['id']]['source_text'], old_blocks[b['id']]['locator']
                    for key in ('excluded', 'heading', 'section', 'page', 'page_title', 'extraction', 'ocr_confidence'):
                        if key in old_blocks[b['id']]:
                            b[key] = old_blocks[b['id']][key]
                        else:
                            b.pop(key, None)
                payload['blocks'] = blocks
            if rules is not None:
                if not isinstance(rules, list) or len(rules) > 100:
                    raise StoreError('规则列表格式错误或超过100条')
                old = {r['id']: r for r in payload['rules']}
                present = set()
                prepared = []
                for r in rules:
                    r = copy.deepcopy(r)
                    r['id'] = r.get('id') or uid('rule')
                    if r['id'] in present:
                        raise StoreError('规则编号重复')
                    present.add(r['id'])
                    previous = old.get(r['id'])
                    ignored = ('review_status', 'knowledge_revision', 'valid', 'validation_errors')
                    changed = not previous or {k:v for k,v in r.items() if k not in ignored} != {k:v for k,v in previous.items() if k not in ignored}
                    r['review_status'] = 'pending' if changed else previous['review_status']
                    r['knowledge_revision'] = previous.get('knowledge_revision', 1)+int(changed) if previous else 1
                    prepared.append(self._rule(r, payload['blocks']))
                if set(old)-present:
                    raise StoreError('停用规则请勾选retired并逐条审核，不可直接删除')
                payload['rules'] = prepared
            changed_block_ids = {b['id'] for b in payload['blocks'] if b != old_blocks[b['id']]}
            if changed_block_ids:
                for r in payload['rules']:
                    if changed_block_ids & set(r.get('block_ids', [])):
                        r['review_status'] = 'pending'
                        r['knowledge_revision'] = r.get('knowledge_revision', 1)+1
            description_status = 'pending' if changed_block_ids else doc['description_status']
            c.execute('UPDATE revisions SET payload=?,description_status=?,edit_version=edit_version+1 WHERE document_id=? AND revision=?', (encoded(payload), description_status, document_id, doc['latest_revision']))
            c.execute('UPDATE kbs SET state_version=state_version+1 WHERE id=?', (doc['kb_id'],))
        return self.document(document_id)

    def review(self, document_id, expected_version, target, decision, rule_id=None, note='', excluded_pages=None, reuse_approval=False):
        if decision not in ('approved', 'rejected') or target not in ('description', 'rule'):
            raise StoreError('无效审核决定')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            doc = self._doc(c, document_id)
            self._kb(c, doc['kb_id'], True)
            if doc['status'] != 'ready' or doc['edit_version'] != expected_version:
                raise StoreError('审核对象已变化，请刷新', 409)
            payload = doc['payload']
            previous_pages = payload.get('excluded_pages', [])
            if excluded_pages is not None:
                if target != 'description' or payload.get('document_type') != 'reference':
                    raise StoreError('仅普通参考文档支持按页排除')
                available = {b.get('page') for b in payload['blocks'] if b.get('page')}
                if any(type(page) is not int or page not in available for page in excluded_pages):
                    raise StoreError('排除页须为文档中存在的物理页码或幻灯片编号')
                payload['excluded_pages'] = sorted(set(excluded_pages))
            if target == 'rule':
                rule = next((r for r in payload['rules'] if r['id'] == rule_id), None)
                if rule is None:
                    raise StoreError('规则不存在', 404)
                if decision == 'approved' and not rule['valid']:
                    raise StoreError('先修正指标、单位、条件及来源错误，再审核通过')
                if reuse_approval and rule['review_status'] == decision:
                    return doc
                rule['review_status'] = decision
                body = rule
            else:
                if reuse_approval and doc['description_status'] == decision and previous_pages == payload.get('excluded_pages', []):
                    return doc
                body = {'blocks': payload['blocks'], 'excluded_pages': payload.get('excluded_pages', [])}
                c.execute('UPDATE revisions SET description_status=? WHERE document_id=? AND revision=?', (decision, document_id, doc['latest_revision']))
            c.execute('INSERT INTO audits VALUES (?,?,?,?,?,?,?,?)', (uid('audit'), document_id, doc['latest_revision'], rule_id or target, decision, digest(body), note, now()))
            c.execute('UPDATE revisions SET payload=?,edit_version=edit_version+1 WHERE document_id=? AND revision=?', (encoded(payload), document_id, doc['latest_revision']))
            c.execute('UPDATE kbs SET state_version=state_version+1 WHERE id=?', (doc['kb_id'],))
        return self.document(document_id)

    def disable_document(self, document_id, disabled):
        with self.connection() as c:
            doc = self._doc(c, document_id)
            self._kb(c, doc['kb_id'], True)
            c.execute('UPDATE documents SET disabled=? WHERE id=?', (int(disabled), document_id))
            c.execute('UPDATE kbs SET state_version=state_version+1 WHERE id=?', (doc['kb_id'],))
        return self.document(document_id)

    def release(self, kb_id, release_id=None, include_index=False):
        with self.connection() as c:
            kb = self._kb(c, kb_id)
            key = release_id or kb['current_release']
            row = c.execute('SELECT * FROM releases WHERE id=? AND kb_id=?', (key, kb_id)).fetchone()
            if not row:
                raise StoreError('该知识库尚无发布版本或指定版本不存在', 404)
            result = json.loads(row['body'])
            if not include_index:
                result.pop('index', None)
            return result

    def releases(self, kb_id):
        with self.connection() as c:
            self._kb(c, kb_id)
            return [{k:v for k,v in json.loads(r['body']).items() if k not in ('index', 'entries')} for r in c.execute('SELECT body FROM releases WHERE kb_id=? ORDER BY number DESC', (kb_id,))]

    def _candidate(self, kb_id, document_id=None, target=None, rule_id=None, expected_version=None):
        from .embeddings import split_text
        with self.connection() as c:
            c.execute('BEGIN')
            kb = self._kb(c, kb_id, True)
            previous = c.execute('SELECT body FROM releases WHERE id=?', (kb['current_release'],)).fetchone()
            entries = {e['id']: e for e in json.loads(previous['body'])['entries']} if previous else {}
            for row in c.execute('SELECT id FROM documents WHERE kb_id=?', (kb_id,)).fetchall():
                if document_id is not None and row['id'] != document_id:
                    continue
                doc = self._doc(c, row['id'])
                if expected_version is not None and doc['edit_version'] != expected_version:
                    raise StoreError('文档在确认后已变化，请刷新核对后重试', 409)
                did = doc['id']
                published_rules = {k:v for k,v in entries.items() if v['source']['document_id'] == did and v['kind'] == 'rule'}
                if doc['disabled']:
                    entries = {k:v for k,v in entries.items() if v['source']['document_id'] != did}
                    continue
                if doc['status'] != 'ready':
                    continue
                payload = doc['payload']
                source = {'document_id': did, 'document_revision': doc['latest_revision'], 'title': doc['title'], 'sha256': doc['file_hash']}
                blocked = {b for r in payload['rules'] if not r.get('retired') for b in r.get('block_ids', [])}
                unsafe = blocked | {b['id'] for b in payload['blocks'] if b['kind'] != 'description' or b.get('page') in payload.get('excluded_pages', [])}
                if target != 'rule' and (doc['description_status']=='approved' or any(r['review_status']=='approved' for r in payload['rules'])):
                    entries = {k:v for k,v in entries.items() if not (v['kind']=='description' and v['source']['document_id']==did and v['source']['document_revision']==doc['latest_revision'] and any(k.startswith(f'{did}-{bid}-') for bid in unsafe))}
                if doc['description_status'] == 'approved' and target != 'rule':
                    entries = {k:v for k,v in entries.items() if not (v['source']['document_id'] == did and v['kind'] == 'description')}
                    from .knowledge_units import prose_units
                    # For a structured rule document, all section prose is represented
                    # once by the fault unit; approved rules replace the prose-only unit.
                    prose_blocks = payload['blocks']
                    if payload.get('document_type') == 'rules':
                        from .documents import _context
                        complete_ids = {b['id'] for r in payload['rules'] if r['review_status'] == 'approved' and not r.get('retired') and (target != 'description' or (r['id'] in published_rules and published_rules[r['id']]['source']['document_revision'] == doc['latest_revision'] and published_rules[r['id']]['knowledge_revision'] == r.get('knowledge_revision', 1))) for b in _context(prose_blocks, r.get('block_ids', []))}
                        unsafe |= complete_ids
                    from .documents import _context
                    section_devices = {b['id']: r['device_type'] for r in payload['rules'] for b in _context(prose_blocks, r.get('block_ids', []))}
                    units = prose_units(prose_blocks, unsafe)
                    counters = {}
                    for block_id, text, locator, raw in units:
                        i = counters.get(block_id, 0); counters[block_id] = i+1
                        key = f"{did}-{block_id}-{i}"
                        entries[key] = {'id': key, 'kind': 'description', 'text': text,
                            'device_type': section_devices.get(block_id) or next((b.get('device_type') for b in prose_blocks if b['id'] == block_id), None) or (payload['rules'][0].get('device_type') if payload['rules'] and len({r.get('device_type') for r in payload['rules']}) == 1 else None),
                            'knowledge_revision': doc['edit_version'], 'source': {**source, 'locator': locator, 'original_quote': raw}}
                for rule in payload['rules']:
                    if target == 'rule' and rule['id'] != rule_id:
                        continue
                    # Confirming prose may enrich an already published exact rule,
                    # but must not publish separately staged or edited rules.
                    published = published_rules.get(rule['id'])
                    if target == 'description' and (not published or published['source']['document_revision'] != doc['latest_revision'] or published['knowledge_revision'] != rule.get('knowledge_revision', 1)):
                        continue
                    if rule['review_status'] != 'approved':
                        continue
                    key = rule['id']
                    if key in entries and entries[key]['source']['document_id'] != did:
                        raise StoreError('知识编号与其他文档冲突，请修改草稿规则编号')
                    if rule.get('retired'):
                        entries.pop(key, None)
                        continue
                    from .knowledge_units import rule_text, source_location
                    public_rule = copy.deepcopy(rule)
                    descriptions = doc['description_status'] == 'approved'
                    if not descriptions:
                        for field in ('phenomena', 'causes', 'actions', 'notes'):
                            public_rule[field] = []
                    raw = rule['original_text']
                    locator = '；'.join(b['locator'] for b in payload['blocks'] if b['id'] in rule.get('block_ids', []))
                    if descriptions and payload.get('document_type') == 'rules':
                        from .documents import _context
                        context = _context(payload['blocks'], rule.get('block_ids', []))
                        locator = source_location(context)
                        raw = '\n'.join([*(b.get('source_text', b['text']) for b in context if b['kind'] == 'description' and b['id'] not in blocked and not b.get('excluded')), rule['original_text']])
                    if target == 'rule' and descriptions and payload.get('document_type') == 'rules':
                        ids = {b['id'] for b in context}
                        entries = {k:v for k,v in entries.items() if not (v['kind']=='description' and v['source']['document_id']==did and v['source']['document_revision']==doc['latest_revision'] and any(k.startswith(f'{did}-{bid}-') for bid in ids))}
                    entries[key] = {'id': key, 'kind': 'rule', 'text': rule_text(public_rule, descriptions), 'device_type': rule['device_type'], 'rule': public_rule, 'knowledge_revision': rule.get('knowledge_revision', 1), 'source': {**source, 'locator': locator, 'original_quote': raw}}
            return kb, list(entries.values())

    def publish(self, kb_id, embedder, restore_id=None, reindex=False, document_id=None, target=None, rule_id=None, expected_version=None):
        from .retrieval import build_index
        if restore_id or reindex:
            with self.connection() as c:
                kb=self._kb(c,kb_id,True)
            entries=[]
        else:
            kb, entries = self._candidate(kb_id, document_id, target, rule_id, expected_version)
        previous = self.release(kb_id) if kb['current_release'] else {'entries': []}
        if restore_id:
            entries = self.release(kb_id, restore_id)['entries']
        elif reindex:
            entries = previous['entries']
        index = build_index(entries, embedder)
        old, new = {e['id']:e for e in previous['entries']}, {e['id']:e for e in entries}
        changes = {'added': sorted(set(new)-set(old)), 'removed': sorted(set(old)-set(new)), 'changed': sorted(k for k in set(old)&set(new) if digest(old[k]) != digest(new[k]))}
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            current = self._kb(c, kb_id, True)
            if current['state_version'] != kb['state_version'] or current['current_release'] != kb['current_release']:
                raise StoreError('索引准备期间草稿或发布版本变化，请重试发布', 409)
            number = c.execute('SELECT COALESCE(MAX(number),0)+1 FROM releases WHERE kb_id=?', (kb_id,)).fetchone()[0]
            key = uid('release')
            result = {'id': key, 'release_id': key, 'kb_id': kb_id, 'number': number, 'created_at': now(), 'entries': entries, 'entry_count': len(entries), 'profile': index['profile'], 'dimension': index['dimension'], 'index': index, 'changes': changes, 'restored_from': restore_id}
            c.execute('INSERT INTO releases VALUES (?,?,?,?,?)', (key, kb_id, number, now(), encoded(result)))
            if restore_id:
                restored_docs={e['source']['document_id'] for e in entries}
                for row in c.execute('SELECT id FROM documents WHERE kb_id=?',(kb_id,)).fetchall():
                    draft=self._doc(c,row['id'])
                    for r in draft['payload']['rules']:
                        r['review_status']='pending'
                    c.execute("UPDATE revisions SET description_status='pending',payload=?,edit_version=edit_version+1 WHERE document_id=? AND revision=?",(encoded(draft['payload']),draft['id'],draft['latest_revision']))
                    c.execute('UPDATE documents SET disabled=? WHERE id=?',(int(draft['id'] not in restored_docs),draft['id']))
                    c.execute('INSERT INTO audits VALUES (?,?,?,?,?,?,?,?)',(uid('audit'),draft['id'],draft['latest_revision'],'restore','pending',digest(draft['payload']),'恢复'+restore_id+'；现有草稿需重新审核',now()))
            c.execute('UPDATE kbs SET current_release=?,state_version=state_version+1 WHERE id=?', (key, kb_id))
        result.pop('index')
        return result

    def audits(self, document_id):
        with self.connection() as c:
            self._doc(c, document_id)
            return [dict(r) for r in c.execute('SELECT * FROM audits WHERE document_id=? ORDER BY rowid', (document_id,))]

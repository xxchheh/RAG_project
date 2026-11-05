"""Small token-scoped SQLite conversation store, separate from knowledge releases."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4
from .common import now
from .store import StoreError


class ChatStore:
    def __init__(self, root):
        self.path = root / 'conversations.db'
        with self.connection() as c:
            c.execute('CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated TEXT NOT NULL, body TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS conversation_owner ON conversations(owner, updated)')
            for row in c.execute('SELECT * FROM conversations').fetchall():
                session = json.loads(row['body'])
                changed = False
                for turn in session['turns']:
                    if turn['status'] == 'pending':
                        turn.update(status='failed', error='服务重启中断了本轮请求，请重试。')
                        changed = True
                if changed:
                    self.write(c, row['owner'], session)

    @contextmanager
    def connection(self):
        with sqlite3.connect(self.path, timeout=30) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN IMMEDIATE')
            yield c

    @staticmethod
    def owner(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def read(self, c, owner, identifier):
        row = c.execute('SELECT body FROM conversations WHERE id=? AND owner=?', (identifier, owner)).fetchone()
        if row is None:
            raise StoreError('会话不存在', 404)
        return json.loads(row['body'])

    def write(self, c, owner, session):
        session['updated_at'] = now()
        c.execute('UPDATE conversations SET updated=?,body=? WHERE id=? AND owner=?',
                  (session['updated_at'], json.dumps(session, ensure_ascii=False), session['id'], owner))

    def create(self, owner, kb_id, release_id, device_type):
        session = {'id': uuid4().hex, 'title': '新对话', 'automatic_title': True, 'kb_id': kb_id,
                   'release_id': release_id, 'device_type': device_type, 'created_at': now(), 'updated_at': now(), 'turns': []}
        with self.connection() as c:
            c.execute('INSERT INTO conversations VALUES(?,?,?,?)',
                      (session['id'], owner, session['updated_at'], json.dumps(session, ensure_ascii=False)))
        return session

    def list(self, owner):
        with self.connection() as c:
            return [json.loads(row['body']) for row in c.execute('SELECT body FROM conversations WHERE owner=? ORDER BY updated DESC, rowid DESC', (owner,))]

    def get(self, owner, identifier):
        with self.connection() as c:
            return self.read(c, owner, identifier)

    def rename(self, owner, identifier, title):
        title = title.strip()
        if not title:
            raise StoreError('会话标题不能为空')
        with self.connection() as c:
            session = self.read(c, owner, identifier)
            session.update(title=title, automatic_title=False)
            self.write(c, owner, session)
        return session

    def delete(self, owner, identifier):
        with self.connection() as c:
            self.read(c, owner, identifier)
            c.execute('DELETE FROM conversations WHERE id=? AND owner=?', (identifier, owner))

    def begin(self, owner, identifier, query, request_id, retry_turn_id=None):
        with self.connection() as c:
            session = self.read(c, owner, identifier)
            for turn in session['turns']:
                if turn['request_id'] == request_id:
                    if turn['query'] != query:
                        raise StoreError('请求编号已用于其他问题', 409)
                    if turn['status'] == 'pending':
                        raise StoreError('本轮正在处理，请稍后刷新会话', 409)
                    return session, False
            if any(turn['status'] == 'pending' for turn in session['turns']):
                raise StoreError('当前会话已有正在处理的问题', 409)
            if retry_turn_id:
                if not session['turns'] or session['turns'][-1]['id'] != retry_turn_id or session['turns'][-1]['status'] != 'failed':
                    raise StoreError('只能重试最后一轮失败的问题', 409)
                turn = session['turns'][-1]
                if turn['query'] != query:
                    raise StoreError('重试问题与原问题不一致', 409)
                turn.update(request_id=request_id, status='pending', result=None, error=None)
            else:
                turn = {'id': uuid4().hex, 'request_id': request_id, 'query': query, 'status': 'pending', 'result': None, 'error': None}
                if not session['turns'] and session['automatic_title']:
                    session['title'] = ' '.join(query.split())[:48]
                session['turns'].append(turn)
            self.write(c, owner, session)
        return session, True

    def finish(self, owner, identifier, request_id, result=None, error=None):
        with self.connection() as c:
            session = self.read(c, owner, identifier)
            turn = session['turns'][-1]
            if turn['request_id'] != request_id or turn['status'] != 'pending':
                raise StoreError('会话状态已变化，请重新读取', 409)
            turn.update(status='failed' if error else 'succeeded', result=result, error=error)
            self.write(c, owner, session)
        return session

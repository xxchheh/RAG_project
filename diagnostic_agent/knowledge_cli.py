"""Explicit local knowledge maintenance; the conversational agent has no write tool."""
from __future__ import annotations

import json
from pathlib import Path

from .common import DemoError


def add_knowledge_parser(subparsers):
    parser = subparsers.add_parser('knowledge', help='维护本地SQLite规则知识库')
    parser.add_argument('--db', help='SQLite文件路径；默认knowledge/knowledge.db')
    commands = parser.add_subparsers(dest='knowledge_command', required=True)
    commands.add_parser('init', help='创建知识库并迁移内置16条规则（重复运行不覆盖）')
    listing = commands.add_parser('list', help='查看规则及草稿/发布/启用状态')
    listing.add_argument('--type', dest='device_type', choices=['motor', 'pump', 'fan', 'coupling', 'gearbox'])
    for name, help_text in [('show', '查看规则内容'), ('diff', '比较草稿与当前发布版'), ('publish', '发布草稿并启用规则')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('rule_id')
        command.add_argument('--revision', type=int)
        if name == 'publish':
            command.add_argument('--confirm-notes', action='store_true', help='已核对导入歧义说明，确认采用该解释')
    disable = commands.add_parser('disable', help='停用规则，保留历史版本')
    disable.add_argument('rule_id')
    history = commands.add_parser('history', help='查看规则全部历史版本')
    history.add_argument('rule_id')
    draft = commands.add_parser('draft', help='从JSON新增/更新草稿，不立即生效')
    draft.add_argument('path')
    draft.add_argument('--rule-id', help='明确更新的现有规则ID；限单条')
    importing = commands.add_parser('import', help='调用模型提取Word规则为草稿，核对后另行发布')
    importing.add_argument('path')
    importing.add_argument('--rule-id', help='明确更新的现有规则ID；限单条文档')
    exporting = commands.add_parser('export', help='导出知识库内容（含历史版本），用于迁移备份')
    exporting.add_argument('path')
    return parser


def execute_knowledge(args):
    from .knowledge import KnowledgeStore
    store = KnowledgeStore(args.db)
    command = args.knowledge_command
    if command == 'init':
        result = {'database': str(store.path), 'rules': len(store.list_rules()), 'message': '知识库就绪；已有修改不会被种子规则覆盖。'}
    elif command == 'list':
        keys = ('id', 'name', 'device_type', 'revision', 'status', 'enabled', 'published_revision', 'has_draft')
        result = [{key: rule[key] for key in keys} for rule in store.list_rules(device_type=args.device_type)]
    elif command == 'show':
        result = store.get_rule(args.rule_id, revision=args.revision)
    elif command == 'diff':
        result = store.diff(args.rule_id, revision=args.revision)
    elif command == 'history':
        result = store.history(args.rule_id)
    elif command == 'publish':
        result = store.publish(args.rule_id, revision=args.revision, confirm_notes=args.confirm_notes)
    elif command == 'disable':
        result = store.disable(args.rule_id)
    elif command == 'draft':
        try:
            value = json.loads(Path(args.path).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise DemoError('无法读取草稿JSON文件') from None
        rules = value.get('rules', [value]) if isinstance(value, dict) else value
        if not isinstance(rules, list) or not rules:
            raise DemoError('草稿JSON应为一条规则、规则列表或含rules的对象')
        if args.rule_id:
            if len(rules) != 1 or not isinstance(rules[0], dict):
                raise DemoError('--rule-id仅支持单条规则')
            store.get_rule(args.rule_id)
            rules[0]['id'] = args.rule_id
        result = store.create_drafts(rules)
    elif command == 'import':
        from .knowledge_import import import_document
        from .model import ChatModel, ModelConfig
        result = import_document(args.path, store, ChatModel(ModelConfig.from_env()), rule_id=args.rule_id)
    elif command == 'export':
        # Preserve the complete SQLite database, including source text and old revisions.
        import sqlite3
        output = Path(args.path).resolve()
        if output == store.path.resolve():
            raise DemoError('备份目标不能与当前数据库相同')
        if output.exists():
            raise DemoError('备份目标已存在，请选择新路径')
        output.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(store.path) as source, sqlite3.connect(output) as target:
            source.backup(target)
        result = {'backup': str(output), 'message': '已备份规则及所有版本；原始文档附件可另备份数据库旁的sources目录。'}
    else:
        raise DemoError('未知知识库操作')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if command in ('draft', 'import'):
        print('\n已保存为草稿，尚未生效。请用 knowledge diff <规则编号> 核对，然后 knowledge publish <规则编号> 发布。')
    return 0

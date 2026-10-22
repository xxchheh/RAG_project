"""Compatibility entry point: Word import creates model-extracted knowledge drafts.

This command never rewrites the packaged bootstrap rules.json.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostic_agent.cli import main

if __name__ == '__main__':
    print('规则导入已迁移到知识库：提取为草稿，核对后另行发布。')
    raise SystemExit(main(['knowledge', 'import', *sys.argv[1:]]))

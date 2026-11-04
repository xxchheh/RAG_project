# 专业知识 RAG 服务

独立的文档知识服务，提供中文管理页面和 HTTP 接口。支持 Markdown、Word、PDF（含扫描件）及 PPTX 的解析、来源核对、分级审核、版本发布、混合检索和带引用问答。

本服务向外部 Agent 提供知识和完整的已发布机理规则。设备取数、数值诊断与报告生成由调用方负责。

## 安装与启动

建议 Python 3.12（项目要求 Python ≥3.11）。克隆并创建独立环境：

```bash
git clone git@github.com:xxchheh/RAG_project.git
cd RAG_project
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

在 `.env` 配置对话模型的 `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。向量模型配置独立，默认本地 FastEmbed。密钥仅保存在本地，已有环境变量优先于 `.env`。

扫描教材和 PPTX 推荐使用隔离的 Docling 解析环境：

```bash
python3.12 -m venv .venv-docling
.venv-docling/bin/python -m pip install -r requirements-parser.lock
```

解析依赖锁来自 Mac 实测，Linux CPU 安装及模型版本说明见 [结构化资料解析](docs/structured_reference.md)。首次解析和索引需要下载模型。部署时保留完整源码目录，包含 `scripts/docling_worker.py`。

```bash
.venv/bin/python -m rag_service serve --host 0.0.0.0 --port 8000
# 另开终端，查看管理令牌与只读令牌
.venv/bin/python -m rag_service tokens
```

浏览器访问 `http://服务器IP:8000/`，用管理令牌连接。交互式接口文档为 `/docs`。默认数据目录为 `data/rag_work/`；原文件、数据库、解析缓存及访问令牌均不提交 Git。

## 使用流程

1. 创建知识库，上传文件，选择“机理规则”或“普通参考文档”。
2. 普通资料先看整份摘要，可按需抽查原页、查看需关注内容或排除指定页面，不要求逐页审核。
3. 点击“确认入库”，系统自动准备索引并发布；专业规则仍逐条核对，点击“审核通过并生效”。待核对内容不会自动进入检索。
4. 在“检索与问答”查询知识，或由外部 Agent 使用只读令牌调用接口。
5. 更新或重新解析产生新修订版，确认后自动生效；“历史记录”提供版本查看、恢复和高级维护。

规则接口返回全部适用的已审核规则。教材中的公式、例题和经验数值不会自动成为正式诊断规则。复杂公式、模糊数字及上下标仍需人工核对。

## 对接与维护

- [Agent 接口简版](docs/agent_api.md)、[请求响应示例](docs/agent_api_examples.md)
- [简化确认流程](docs/quick_confirmation.md)
- [完整使用说明](docs/rag_work.md)
- [Docling 解析配置、检查点与重新处理](docs/structured_reference.md)
- [接口兼容约定](docs/api_compatibility.md)、[验证记录](docs/validation.md)
- [外部 HTTP 调用示例](examples/rag_agent_client.py)、[样本文档](examples/rag_documents)

内置 16 条规则种子及参考 Word 保存在 `rag_service/assets/`，仅用于显式导入和解析验证。导入仍为待审核草稿，不自动发布：

```bash
.venv/bin/python -m rag_service import-legacy --name '设备机理知识库'
# 也支持 --database /path/to/old/knowledge.db，只读复制旧规则
```

迁移已有服务时停止写入，备份并复制整个 `data/rag_work/` 及本地配置，保留原访问令牌和历史模型配置。此次代码清理不需要迁移或重建现有业务数据库。

## 检查

```bash
.venv/bin/python -m unittest discover -s tests -v
# 使用真实本地向量模型验证内置规则文档（临时知识库）
.venv/bin/python scripts/validate_rule_manual.py
```

公开 Agent 接口及规则能力由 `tests/test_agent_api_contract.py` 和固定契约基线约束。维护时遵守 `AGENTS.md`，不得用覆盖基线的方式掩盖不兼容变更。

# 多文档知识抽取与融合 Demo

## 独立开发环境

开发目录为 `/Users/xxc/Developer/work/RAG_project-knowledge`，分支为 `knowledge`。原演示目录使用 `main`。

本机已从现有环境建立独立的 `.venv` 和 `.venv-docling` 副本，启动脚本路径已重定位。本地模型缓存也建立了独立副本。其他机器使用现有 `requirements.txt` 和 `requirements-parser.lock` 安装依赖。

在本目录 `.env` 设置以下内容，已有模型配置继续使用 `LLM_*` 和 `EMBEDDING_*`。

```dotenv
RAG_FUSION_ENABLED=true
RAG_DATA_DIR=/Users/xxc/Developer/work/RAG_project-knowledge/data/rag_work
EMBEDDING_CACHE_DIR=/Users/xxc/Developer/work/RAG_project-knowledge/data/rag_work/models
LLM_PROVIDER=deepseek
LLM_MODEL=替换为账户可用的模型
LLM_API_KEY=本机配置的密钥
```

```bash
cd /Users/xxc/Developer/work/RAG_project-knowledge
.venv/bin/python -m rag_service serve --port 8001
# 另开终端，在本机查看管理令牌
.venv/bin/python -m rag_service tokens
```

管理页为 `http://127.0.0.1:8001/`，融合页为 `http://127.0.0.1:8001/fusion`。环境变量优先于 `.env`，启动前需确认终端中的数据路径属于新目录。使用单进程启动，任务状态恢复按单服务实例设计。

## 使用流程

1. 在原有工作台创建若干知识库，重新上传 5–10 份有知识交叉的短资料，选择参考资料并完成解析。
2. 用管理令牌连接融合页，创建融合项目。从多个知识库勾选文档。
3. 设置跨文档 Top-K，默认5。可选同时检查同一文档内的重复知识。
4. 点击“抽取、融合并构建主题”，查看阶段状态。关闭页面后任务继续执行，重新进入可查看历史运行。
5. 在“原始知识与关系”查看 Claim 的条件、限定词、原文引文、候选对和判断理由。
6. 在“统一知识与主题”查看规范化知识、全部归并成员、概念和主题目录。原件下载及 PDF 原页核对绑定本次运行的文件哈希。
7. 选择主题生成专题。点击引用跳转到规范化知识，再展开查看 Claim 和原文。
8. 失败或中断后点击“恢复失败任务”。输入、模型和 Prompt 变更通过新运行记录。

融合结果属于管理员实验数据，原有 RAG 的审核和发布仍通过现有入口进行。解析完成的草稿可参与实验，实验结果可在页面核对来源。

## 模块与数据

复用现有文档归档、解析块、Docling/OCR、LLM 供应商适配、Embedding、鉴权、FastAPI 和页面样式。新模块 `rag_service.knowledge_fusion` 负责来源快照、结构化抽取、候选匹配、关系判断、融合、目录和专题。管理员输出通过 `rag_service.api_contract` 的 `Fusion*` 模型校验。

独立 SQLite 文件为 `$RAG_DATA_DIR/fusion/knowledge_fusion.db`。`projects` 保存融合项目，`runs` 保存完整版本化运行快照及检查点。逻辑对象包括以下内容。

- Document 快照保存知识库、修订号、编辑版本、原件哈希和解析版本。
- SourceSpan 保存原始 `source_text`、校对文本、章节、页码、坐标、块编号及字符偏移。抽取引用以原始解析文本为准。
- RawClaim 保存概念、通用知识类型、陈述、条件、限定词和证据。
- Candidate、ClaimRelation 保存召回分数、语义关系、条件比较、方向和合并建议。
- CanonicalKnowledge 保存代表陈述及全部成员和证据。
- Concept、Topic、GeneratedContent 保存目录、知识关联及带引用专题。
- Profile 保存模型名称、服务地址、Embedding 指纹和四个 Prompt 指纹，密钥由本机环境提供。
- Labels 保存本次运行的人工关系标注。

运行按窗口、配对和目录批次保存检查点，重试跳过已完成部分。项目通过数据库唯一索引限制活动主任务。新服务启动后，中断任务显式标记为 interrupted。模型或 Prompt 指纹变化时，恢复返回错误。

抽取窗口约6000字，相邻小块重叠。超长原文块按段落或表格行拆分，表格头作为上下文保留。互补、概括/细化和冲突保持独立陈述。归并组要求所有成员两两明确等价、条件一致并建议合并。代表陈述取组内完整原始陈述，原始成员全部保留。

## 管理员 API

所有接口使用管理员 Bearer 令牌，前缀为 `/api/v1/admin/knowledge-fusion`。交互说明及完整类型见本机 `/docs`。

| 路径 | 方法及用途 |
|---|---|
| `/status` | GET，返回功能开关 |
| `/projects` | GET / POST，查看及创建项目 |
| `/projects/{id}/runs` | GET / POST，查看及创建运行 |
| `/runs/{id}` | GET，查看状态、阶段及版本配置 |
| `/runs/{id}/retry` | POST，恢复失败或中断任务 |
| `/runs/{id}/{collection}` | GET，分页查询阶段结果 |
| `/runs/{id}/summaries` | POST，按 topic_id 生成专题 |
| `/runs/{id}/evaluation` | GET，统计及人工标注评价 |
| `/runs/{id}/annotations` | GET / PUT，导出及替换人工标注 |
| `/runs/{id}/annotations.jsonl` | GET，下载 JSONL |
| `/runs/{id}/documents/{document_id}/original` | GET，核对快照原件 |
| `/runs/{id}/documents/{document_id}/pages/{page}` | GET，核对快照 PDF 原页 |

创建运行的 documents 数组包含 document_id、revision 和 edit_version，另可设置 top_k 与 same_document。collection 支持 documents、spans、claims、candidates、relations、knowledge、concepts、topics、summaries。分页参数 offset 默认0，limit 默认100、最大500。

标注导出可重复传入 claim_ids，最多选择30条，导出它们之间全部跨文档配对。填写 relation，支持 equivalent、complementary、broader_narrower、conflict、unrelated、uncertain。导入采用 labels 数组，每项仅包含 claim_a、claim_b、relation。页面支持直接导入填写后的 JSONL。导入替换本次运行的标注集，请保留原标注文件。

## 验证和已知局限

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_agent_api_contract.py' -v
.venv/bin/python -m unittest discover -s tests -v
# 显式使用已配置模型，可能产生模型 API 费用
.venv/bin/python scripts/validate_knowledge_fusion.py --live
```

本次完整回归118项通过，包含原有99项及新增19项。固定模型端到端测试覆盖跨库、等价归并、冲突、主题、专题、引用与人工评价。五份合成短资料已使用固定测试模型完成页面加载、统计和专题引用检查。

真实模型验证检查发现本地缺少有效 LLM_MODEL 或 LLM_API_KEY，当前未取得真实模型融合效果结果。验证脚本将失败原因记录在 `outputs/fusion-validation.json`。用户上传的真实资料及人工标注尚待提供。

统计与效果评价分开显示。Precision、Recall、F1 只对已标注配对计算，候选未召回的人工等价配对计为漏检。分母为零时显示空值。来源链完整率衡量引用是否可定位，语义是否得到充分支持仍需人工检查。

本轮面向短资料。目录组织按批次处理，再合并目录。全局目录与专题输入设有80000字保护，超限显示失败并要求缩小任务或主题。完整长教材的上下文覆盖、成本与效果仍需专项验证。来源引文与结构校验无法证明模型的语义判断完全正确，OCR 和公式质量仍依赖原件核对。


## 代码变更位置

新增 `rag_service/knowledge_fusion/`，包括 schema、sources、store、pipeline、evaluation、routes 及四个独立 Prompt。新增融合页 fusion.html、fusion.js、fusion.css 和入口脚本 fusion-entry.js。新增 `tests/test_knowledge_fusion.py` 与显式真实模型验证脚本 `scripts/validate_knowledge_fusion.py`。

适配 app.py 注册路由和页面，api_contract.py 增加管理员响应类型，index.html 增加入口，pyproject.toml 打包 Prompt，.env.example 增加开关。原 Agent 契约基线继续使用现有版本。

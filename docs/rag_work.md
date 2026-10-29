# 独立 RAG 服务使用与对接

`RAG_work` 提供可独立运行的 HTTP 服务和中文管理页面。说明内容按文档审核，专业规则逐条审核；外部 Agent 通过接口读取已发布知识，并在自己的诊断工具中执行数值判断。

## 启动

在项目根目录运行，使用 Python 3.11 或更新版本：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
.venv/bin/python -m rag_service serve
```

已有虚拟环境和 `.env` 可以继续使用，不要覆盖本地密钥。默认打开 `http://127.0.0.1:8000/`；接口文档位于 `/docs`，OpenAPI 文件位于 `/openapi.json`。

在另一个终端读取本地生成的管理令牌和只读令牌：

```bash
.venv/bin/python -m rag_service tokens
```

在页面填入管理令牌。外部 Agent 使用只读令牌。两类令牌应分别保管，不要提交到 Git 或把管理令牌交给只需读取的调用方。也可用 `RAG_ADMIN_TOKEN`、`RAG_READ_TOKEN` 指定不同且分别至少12位的值；修改后需重启服务，并使调用方使用同一配置。

默认数据目录为 `data/rag_work/`，通过 `RAG_DATA_DIR` 可修改。目录中包含知识库、原件归档及首次生成的访问令牌。备份时保存整个目录；运行目录与模型缓存不提交仓库。

## 模型配置

提取专业规则和生成问答使用已有的 `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。未配置对话模型时，普通文档仍可解析，疑似规则块会隔离等待人工处理。模型超时或提取失败会显示任务失败，不会自动发布内容。

向量化单独配置。本地模式为默认值，首次使用需要下载中文模型：

```dotenv
EMBEDDING_MODE=local
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
# 可选，指定模型缓存位置
EMBEDDING_CACHE_DIR=data/rag_work/models
```

在线模式要求服务提供兼容的 `/embeddings` 接口。`EMBEDDING_BASE_URL` 是该接口前缀，不含末尾 `/embeddings`：

```dotenv
EMBEDDING_MODE=api
EMBEDDING_BASE_URL=https://your-provider.example/v1
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_API_KEY=your-local-key
EMBEDDING_TIMEOUT=90
```

在线模型密钥与对话模型密钥分别配置。切换模式或模型后重启服务，再在页面执行索引重建；重建产生新发布版本，失败保留原版本。系统校验查询模型与索引的配置标识，不混用不同模型或维度。历史文本、规则和引用仍可查看；使用历史版本进行语义检索时，需要匹配的模型配置。

## 文档与分级审核

首版支持 UTF-8 Markdown、`.docx` 和文字型 `.pdf`，单文件不超过10 MiB、提取文字不超过200000字，PDF不超过100页。PDF中有无法提取文字的页面时会提示补充完整文字版，不自动执行OCR。Word支持段落与表格，旧版 `.doc` 需要先另存为 `.docx`。

样本目录 `examples/rag_documents/` 提供三份可上传文件：

| 文件 | 用途 |
|---|---|
| `standard.md` | 电机轴承标准规则，适合查看行号、阈值及条件分组 |
| `standard.docx` | 同一标准规则的 Word 版本，适合查看段落来源 |
| `maintenance.pdf` | 非标准说明型文字PDF，适合整体审核和润滑维护问答 |

Markdown与Word是同一规则的两种格式。可分别创建演示文档；若要演示格式替换，使用同一文档的“更新”操作，避免把它们当成两条业务规则。

标准文档建议使用以下字段，并将每个字段单独写成段落：

```text
设备类型：motor（电机）
故障名称：电机滚动轴承故障
机理现象：……
机理原因：……
解决措施：……
机理规则：加速度非超额峭度大于等于3.5且小于4.5，并且……
```

页面操作顺序：

1. 创建知识库，上传文档，等待处理任务进入“待审核”。
2. 在审核工作台对照原文、来源位置和解析结果。普通说明整体通过或退回。
3. 逐条检查专业规则的设备类型、指标、单位、阈值、边界及且／或关系；修正后保存，再单独通过或退回。
4. 执行发布。索引准备成功后，新版本对外生效。

说明可以先发布，规则随后逐条发布。未审核的规则块、无法确定的块和与规则关联的原文不会通过说明检索被返回。模型给出的规则只是候选；无效指标、阈值或来源错误必须先修正。

目前专业规则覆盖 `motor`、`pump`、`fan`、`coupling`、`gearbox`，支持的指标和单位可查询 `/api/v1/capabilities`。条件使用 `all`、`any` 和 `ge`、`gt`、`le`、`lt`，不执行文档或条件中的代码。

## 更新 同步与版本

更新文档会产生新修订版；审核发布前，继续使用原发布内容。修改已审核的说明或规则后，相关审核失效，需要再次确认。

发布允许说明和规则独立替换，尚未审核的新规则不会替换旧规则。更新后消失的规则作为“待停用”候选保留，需要逐条审核停用。停用整个文档后，也需要发布才能从当前检索和规则接口移除。

首版同步采用外部系统主动推送：上传接口可携带 `external_id`，同一知识库中相同编号代表同一文档，内容变化时创建新修订版；重复推送未变化内容不会重复创建草稿。此功能不主动抓取第三方文档系统。

每次发布记录条目增删改、模型配置和完整知识快照。恢复历史版本会创建一个新发布版本，不覆盖原历史记录。建议 Agent 在任务开始时记录 `release_id`，此后读取规则、检索和问答都传同一个编号。

## Agent 读取接口

面向外部 Agent 开发者的独立交付文档见 [Agent 侧接口文档](agent_api.md)，包含常用功能、接口、主要参数和简单调用流程。

接口前缀为 `/api/v1`，请求头使用 `Authorization: Bearer <只读令牌>`。接口返回的来源包括文档编号、文档修订版、原文位置、原文摘录和哈希，不包含服务器文件路径。

| 方法与路径 | 主要参数 | 返回内容 |
|---|---|---|
| `GET /knowledge-bases` | 无 | 可用知识库及当前发布版本 |
| `GET /capabilities` | 无 | 设备、指标、单位、派生约定与条件格式 |
| `POST /retrieve` | `kb_id`、`query`、可选 `release_id`、`device_type`、`top_k` | 相关片段、来源及模型配置 |
| `GET /rules` | `kb_id`、可选 `release_id`、`device_type` | 全部适用已发布规则，不按top-k截断 |
| `GET /knowledge/{id}` | `kb_id`、可选 `release_id` | 指定发布版本中的知识详情 |
| `POST /answers` | 与retrieve相同 | 基于检索证据的回答及引用 |

下面假设令牌和知识库编号已经在本地终端赋给变量，勿将真实值写入脚本提交：

```bash
export RAG_BASE_URL=http://127.0.0.1:8000
export RAG_READ_TOKEN='填写本地只读令牌'
export KB_ID='填写知识库编号'

curl "$RAG_BASE_URL/api/v1/knowledge-bases" \
  -H "Authorization: Bearer $RAG_READ_TOKEN"

curl "$RAG_BASE_URL/api/v1/rules?kb_id=$KB_ID&device_type=motor" \
  -H "Authorization: Bearer $RAG_READ_TOKEN"

curl "$RAG_BASE_URL/api/v1/retrieve" \
  -H "Authorization: Bearer $RAG_READ_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"kb_id\":\"$KB_ID\",\"query\":\"润滑不足的原因与维护措施\",\"top_k\":5}"
```

`release_id` 省略时使用当前发布版，响应会给出实际版本。规则接口返回结构化条件与单位；调用方负责准备对应指标并执行诊断。检索结果和问答不能代替设备测量证据。

## 管理接口

管理接口使用管理令牌，普通只读令牌调用写操作会得到403。具体请求结构可直接查看 `/docs`：

| 方法与路径 | 功能 |
|---|---|
| `POST /admin/knowledge-bases` | 创建知识库，JSON字段`name` |
| `POST /admin/knowledge-bases/{id}/archive` | 归档或取消归档，JSON字段`archived` |
| `POST /admin/documents` | multipart上传，字段`kb_id`、`file`、可选`external_id` |
| `POST /admin/documents/{id}/revisions` | multipart更新，字段`file` |
| `GET /admin/documents?kb_id=…` | 文档列表 |
| `GET /admin/documents/{id}` | 原文块、规则候选、审核状态和`edit_version` |
| `PATCH /admin/documents/{id}` | 修改块和规则，携带`expected_version` |
| `POST /admin/documents/{id}/review` | 整体说明或单条规则审核，携带`expected_version` |
| `POST /admin/documents/{id}/disable` | 停用或恢复文档，JSON字段`disabled` |
| `GET /admin/tasks/{task_id}` | 查询处理或发布任务 |
| `POST /admin/publish` | 发布已审核内容，JSON字段`kb_id` |
| `POST /admin/reindex` | 用当前向量配置重建索引，JSON字段`kb_id` |
| `POST /admin/restore` | 恢复历史内容为新版本，字段`kb_id`、`release_id` |
| `GET /admin/releases?kb_id=…` | 发布记录及差异 |
| `GET /admin/releases/{release_id}` | 发布内容快照 |
| `GET /admin/documents/{id}/audits` | 文档审核记录 |

审核请求的 `target` 为 `description` 或 `rule`，`decision` 为 `approved` 或 `rejected`；规则审核还需传 `rule_id`，可用 `note` 保存复核说明。`expected_version` 必须使用最新文档详情中的 `edit_version`；若返回409，应刷新内容再审核，避免批准已经变化的草稿。

上传和发布接口返回202及 `task_id`，不表示内容已经发布。调用方需查询任务，只有 `status=succeeded` 才算完成；发布任务结果含实际 `release_id`。错误任务显示失败原因，原发布版继续有效。

## 迁移与演示

迁移默认使用仓库内置16条规则，保存为待审核草稿，不自动发布：

```bash
.venv/bin/python -m rag_service import-legacy --name '原规则演示库'
# 向指定知识库导入一份旧数据库中的当前规则
.venv/bin/python -m rag_service import-legacy --kb-id '填写知识库编号' --database /path/to/old/knowledge.db
```

迁移仅复制规则及来源，旧库不会被修改。迁移后的每条规则仍需在页面审核。原有终端 Agent 可继续通过 `.venv/bin/python -m diagnostic_agent chat` 启动，其知识库与本服务默认数据目录独立。

建议演示顺序：上传三种格式；查看普通说明与规则的不同审核状态；先发布说明并检索；修正、逐条审核规则后再发布；用只读接口读取完整规则；更新文档；停用并发布；指定旧版本查看历史内容。

自动测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

真实向量模型与对话模型验收需分别记录。没有配置在线Embedding密钥时，只能完成该接口的协议模拟测试，不能宣称已完成在线模型真实验收。


## 历史恢复与调用示例

恢复历史版本会创建新的发布记录，并同步文档启用状态；当前草稿内容保留，审核状态重置为待审核。这样再次点击发布不会把恢复内容自动覆盖。需要发布这些草稿时，重新核对并审核。

外部 Agent 示例使用标准库 HTTP 固定一次发布版本，再获取相关说明和完整适用规则：

```bash
.venv/bin/python examples/rag_agent_client.py --kb-id "$KB_ID" --device-type motor
# 可选：调用方读取特征JSON并在本地诊断工具中计算
.venv/bin/python examples/rag_agent_client.py --kb-id "$KB_ID" --features /path/to/features.json
```

问答采用摘录式生成：模型从命中内容中选择与问题有关的原文，程序验证知识编号和连续引用后返回。它提供有出处的专业依据，不生成设备诊断结论。

真实服务验收脚本只使用仓库模拟样本，会自动审核这些测试草稿；请在隔离数据目录的测试服务上执行：

```bash
# 先通过环境变量设置该测试服务的管理令牌和只读令牌
.venv/bin/python scripts/validate_rag_service.py --base-url http://127.0.0.1:8000
```

发布前应查看任务是否成功。当前为单进程演示服务；中断任务在重启后标记失败，可重新上传处理。运行时依赖可用 `pip install '.[rag,local]'` 安装，在线向量模式可用 `pip install '.[rag]'`，不安装本地模型运行依赖。

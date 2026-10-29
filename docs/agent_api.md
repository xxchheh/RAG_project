# RAG 服务接口说明（Agent 对接简版）

适用分支：`RAG_work`。接口版本：`/api/v1`。规则格式：`equipment-rules-v1`。

本文是 Agent 侧的 v1 对接约定。内部解析、切分、检索和存储可以调整，以下核心字段和含义保持兼容。

## 1. 这个服务提供什么

本服务为 Agent 提供经过审核、发布的专业知识，包括设备原理、故障原因、处理措施和结构化诊断规则。

Agent 可以查询知识辅助回答，也可以获取规则交给自己的诊断工具执行。**设备数据获取、数值判断及报告生成由 Agent 侧负责。**

## 2. 接入方式

由 RAG 服务提供方交付三项信息：

- 服务地址，例如 `http://127.0.0.1:8000`，远程调用时替换成实际可达地址。
- 只读访问令牌。
- 要使用的知识库编号 `kb_id`。

请求地址统一为：

```text
{服务地址}/api/v1/{接口路径}
```

除健康检查外，请求都需要携带：

```http
Authorization: Bearer <只读令牌>
```

POST 请求使用 `Content-Type: application/json`。令牌由程序配置保存，不放入模型提示词。

## 3. 常用功能与接口

下表路径均相对于 `/api/v1`。

| 功能 | 接口 | 主要参数 | 返回内容 |
|---|---|---|---|
| 查看知识库 | `GET /knowledge-bases` | 无 | 知识库编号、名称和当前发布版本 `current_release` |
| 检索相关知识 | `POST /retrieve` | `kb_id`、`query`；可选版本、设备类型、条数 | 相关片段、知识编号、来源和实际使用版本 |
| 获取诊断规则 | `GET /rules` | `kb_id`；可选 `device_type`、`release_id` | 指定范围内全部已发布规则，包括指标、单位、阈值和且／或条件 |
| 查看知识详情 | `GET /knowledge/{knowledge_id}` | 路径中的知识编号；Query中传 `kb_id`，可选 `release_id` | 完整知识内容、来源及规则详情 |
| 获取知识回答 | `POST /answers` | 与检索接口相同 | 带原文引用的摘录式回答；依据不足时明确提示 |
| 查看支持的规则格式 | `GET /capabilities` | 无 | 支持的设备类型、指标、单位和派生计算约定 |
| 检查服务状态 | `GET /health` | 无，不需要令牌 | 服务状态和向量模型配置信息，不代表已验证模型可用性 |

上传文档、审核和发布由知识库管理端完成，Agent 日常调用以上读取接口即可。

### 检索和问答的请求参数

`POST /retrieve` 与 `POST /answers` 使用相同的 JSON 请求格式：

| 参数 | 必填 | 说明 |
|---|---|---|
| `kb_id` | 是 | 知识库编号 |
| `query` | 是 | 问题或关键词，1–4000字符，不能全为空白 |
| `release_id` | 否 | 发布版本；省略时使用当前版本 |
| `device_type` | 否 | 设备类型；省略时不按设备筛选 |
| `top_k` | 否 | 最多返回条数，默认5，范围1–50 |

设备类型：`motor` 电机、`pump` 水泵、`fan` 风机、`coupling` 联轴器、`gearbox` 齿轮箱。

设备筛选采用严格匹配，可能排除未标设备类型的通用说明。一般原理问答可以先不传 `device_type`。

## 4. 简单调用示例

以下编号均为占位示例，需要替换成实际值。

**查询规则：**

```http
GET /api/v1/rules?kb_id=kb-demo&device_type=motor&release_id=release-demo
Authorization: Bearer <只读令牌>
```

响应主要字段为 `release_id`、`count` 和 `rules`。从 `rules[i].condition` 读取判断条件，从 `rules[i].units` 读取单位，从 `rules[i].source` 获取来源。

**检索相关知识：**

```http
POST /api/v1/retrieve
Authorization: Bearer <只读令牌>
Content-Type: application/json
```

```json
{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "query": "电机轴承润滑不足有哪些原因和处理措施？",
  "top_k": 5
}
```

响应主要字段为：

- `release_id`：实际使用的发布版本。
- `items`：命中条目，每项包含 `id`、`kind`、`text`、`source` 及检索分数。
- `source` 中包含文档编号、修订版、原文位置和摘录，供回答或报告引用。

希望直接获得带引用的知识回答时，将请求路径换成 `/api/v1/answers`。响应额外包含 `status`、`answer` 和 `citations`。`status=insufficient_evidence` 表示知识不足，属于正常响应。

## 5. 推荐的 Agent 调用流程

1. 获取目标知识库的 `current_release`，保存为本次任务的 `release_id`。
2. 需要设备诊断时，调用 `/rules` 获取全部适用规则，由 Agent 侧工具结合测量数据计算。
3. 需要原因、措施或解释时，调用 `/retrieve` 获取依据，或调用 `/answers` 获取摘录式回答。
4. 后续详情查询继续使用同一 `release_id`，在任务结果中保留版本和来源。

调用时注意：

- 诊断规则通过 `/rules` 完整获取，不能只执行检索排名靠前的几条。
- 条件树中的 `all` 表示且，`any` 表示或；比较符 `ge/gt/le/lt` 分别表示≥、>、≤、<。指标及计算约定可查 `/capabilities`。
- 检索分数用于排序，不是故障概率或诊断置信度。
- 无规则、未命中或知识不足，都不能直接解释为设备正常。
- `/knowledge-bases` 直接返回数组；`/rules` 返回 `rules` 数组；知识详情则放在 `item` 中，规则详情位于 `item.rule`。
- 常见错误：401为令牌无效，404为知识库、版本或条目不存在，400/422通常需要检查请求或模型配置。不要在报错时自动更换知识库或发布版本。

## 6. 返回字段与规则约定

所有成功请求返回 HTTP 200。以下为保证可用的核心字段，其他附加字段可忽略；不要自行包装或假定存在 `data`。

| 接口 | 响应结构 |
|---|---|
| `/knowledge-bases` | 根数组；每项有 `id`、`name`、`current_release`（未发布为 null）、`archived`、`state_version`；最后一项是内部编辑计数，不是发布版本 |
| `/retrieve` | `kb_id`、`release_id`、`profile`、`items` |
| `/answers` | 检索响应加 `status`、`answer`、`citations`；状态为 `answered` 或 `insufficient_evidence` |
| `/rules` | `kb_id`、`release_id`、`schema_version`、`count`、`rules`；不受检索 `top_k` 限制 |
| `/knowledge/{id}` | `release_id`、`item`；不存在返回 404 |
| `/capabilities` | `schema_version`、`device_types`、`metrics`、`derived`、`operators`、`groups`、`formats`、`executes_diagnosis=false` |
| `/health` | `status=ok`、`embedding`、`rule_schema_version` |

**知识条目**：`id`（字符串）、`kind`（`description` 或 `rule`）、`text`（字符串）、`device_type`（上述设备类型或 null）、`knowledge_revision`（整数）、`source`。规则条目还包含 `rule` 对象。

检索条目额外包含数值型 `score`、`semantic_score`、`keyword_score`；仅用于本次排序，算法与分值范围可能调整，不能作为固定业务阈值。`profile` 和健康检查的 `embedding` 用于显示模型配置，不参与诊断。

**规则对象**：`id`、`knowledge_revision`、`name`、`device_type`、`original_text`、`condition`、`units`；`phenomena`、`causes`、`actions`、`notes`、`review_notes` 均为字符串数组，可以为空。`/rules` 每项另外包含 `knowledge_id`、`source`。可选审核元数据不需要参与执行。

**来源 `source`**：`document_id`、`title`、`sha256`、`locator`、`original_quote` 为字符串，`document_revision` 为整数。`sha256` 是原始文件哈希；人工修订后的知识文本可能与原文摘录不同。引用以 `kb_id + release_id + knowledge_id` 定位，编号按不透明字符串保存，不从编号猜含义。

**回答引用 `citations`**：每项有 `knowledge_id`、`quote`、`source`。`quote` 是对应检索条目 `text` 中的连续原文；知识不足时 `citations=[]`。回答文字可优化，程序应读取 `status` 判断结果。

### 条件树的含义

例如下面的条件表示：峭度在 `[3.5, 4.5)`，并且峰值因子大于等于 4：

```json
{
  "all": [
    {"metric": "kurtosis", "ge": 3.5, "lt": 4.5},
    {"metric": "crest_factor", "ge": 4}
  ]
}
```

- `all`：全部子条件满足；`any`：至少一个子条件满足；可以嵌套，条件组不能为空。
- 叶子用 `metric` 指定指标；`ge/gt/le/lt` 分别表示 ≥、>、≤、<。同一叶子的上下限同时生效。
- 阈值为有限数值；每个叶子至少一个边界，同侧不能同时填写 `ge` 和 `gt` 或 `le` 和 `lt`。可选 `label` 仅供显示，不用于执行。
- `units` 按指标编号记录单位，例如 `{"kurtosis":"1","crest_factor":"1"}`。不依靠自然语言或 `label` 猜单位和公式。
- 条件树最多 200 个节点，根深度为 0、最大深度为 12。v1 不接受其他运算符或自定义代码。

### v1 指标及派生计算

| 指标编号 | 含义及单位 |
|---|---|
| `velocity_rms` | 速度有效值，`mm/s` |
| `acceleration_rms`、`acceleration_peak` | 加速度有效值、加速度绝对峰值，`m/s²` |
| `kurtosis` | 非超额峭度，`1`（无量纲） |
| `low_frequency_impact` | 低频冲击演示指标，`demo_index` |
| `h1`～`h9` | 1～9 倍转频速度 RMS 幅值，`mm/s` |
| `bpf1`、`bpf2` | 一倍、二倍叶片通过频率速度 RMS 幅值，`mm/s` |

派生指标单位均为 `1`：

- `crest_factor = acceleration_peak / acceleration_rms`。
- `h1_ratio = h1 / velocity_rms`；`h2_ratio = h2 / velocity_rms`。
- `even_ratio = sqrt(h2²+h4²+h6²+h8²) / velocity_rms`。
- `third_ratio = sqrt(h3²+h6²+h9²) / velocity_rms`。
- `bpf_ratio = sqrt(bpf1²+bpf2²) / velocity_rms`。

Agent 工具负责计算；必要数据缺失、单位不符、分母非正时应报告无法判断，不补零。不要执行自己不支持的规则格式或指标。

## 7. 错误与兼容要求

错误保留 `detail` 并提供稳定的 `code`，例如：

```json
{"code":"NOT_FOUND","detail":"该发布版本中不存在此知识条目"}
```

| HTTP 状态 | `code` | 调用方处理 |
|---|---|---|
| 400 | `BAD_REQUEST` | 检查参数、模型配置或数据，不能当作没有知识 |
| 401 / 403 | `UNAUTHORIZED` / `FORBIDDEN` | 检查令牌或权限 |
| 404 | `NOT_FOUND` | 知识库、版本、条目不可用；不要自动改用其他版本 |
| 409 | `CONFLICT` | 管理操作对象已变化，刷新后核对 |
| 422 | `VALIDATION_ERROR` | 参数格式错误；`detail` 是校验错误数组 |
| 500 | `INTERNAL_ERROR` | 服务失败，保留错误并联系服务提供方 |

其他状态或网关、网络错误统一视为调用失败。`detail` 的措辞不固定，不通过它识别错误。

- 省略或传空 `release_id` 使用当前发布版；同一任务应显式固定版本。返回的版本必须与所选版本一致。
- 新增知识、修改阈值通过新发布版本生效。旧发布内容保留，但归档知识库后不可读；历史语义检索需要对应向量模型配置可用。
- 客户端忽略新增响应字段；请求只发送文档定义的字段，多余 JSON 参数会被拒绝。
- v1 不删除、改名或改变核心字段类型、层级、默认行为及规则含义。不兼容修改另开接口或规则格式版本。

## 8. 进一步对接

- 在线接口说明：`{服务地址}/docs`。
- 机器可读定义：`{服务地址}/openapi.json`。
- [可运行的外部 Agent 调用示例](../examples/rag_agent_client.py)。
- [服务部署、文档维护与审核说明](rag_work.md)。

# Wiki 分支：知识问答与专业规则

本分支把原“检索与问答”拆成两个日常入口。“知识管理”的上传、整份确认、规则逐条审核、自动入库和历史记录仍按原流程使用。

## 知识问答

选择知识库后输入问题，可查看检索依据或获取带引用的回答。版本、设备类型、返回条数用于缩小查询范围；一般原理问题可不选设备类型，避免排除未标设备的参考资料。

当前页面采用**大模型综合回答与原文引用**：检索后将问题和证据交给回答模型，由模型组织成清楚的说明、原因或步骤。每段回答附连续原文引用，后端校验知识编号与摘录。仅有引用并不能自动证明生成文字的每句话都正确，专业内容仍需核对。引用可以查看文档名称、修订版及页码或原文位置；依据不足时明确提示，不会自动给出设备正常或故障结论。

- 查看依据使用既有 `POST /api/v1/retrieve`。
- 页面生成回答使用新增 `POST /api/v1/generated-answers`；旧 `POST /api/v1/answers` 保持摘录式行为。
- 同一次查询的结果与详情保留实际 `release_id`，不把历史查询偷偷切换到当前版本。

### 回答模型配置

服务端通过兼容 Chat Completions 的适配器接入 DeepSeek 或 Qwen。密钥只配置在本地 `.env` 或服务器环境变量中，不填写到浏览器，也不提交 Git。修改配置后重启服务。

**未设置任何非空 `ANSWER_LLM_*` 项时，问答沿用原 `LLM_*` 配置，现有部署不需要修改。** 一旦启用独立回答配置，就必须同时提供 `ANSWER_LLM_MODEL` 和 `ANSWER_LLM_API_KEY`，不会从原配置继承其他厂商的模型或密钥。`ANSWER_LLM_PROVIDER` 默认 `deepseek`，服务地址按厂商选择，超时默认90秒。

DeepSeek 示例（替换为账号实际可用模型）：

```dotenv
ANSWER_LLM_PROVIDER=deepseek
ANSWER_LLM_BASE_URL=https://api.deepseek.com/v1
ANSWER_LLM_MODEL=你的DeepSeek模型名称
ANSWER_LLM_API_KEY=你的本地密钥
ANSWER_LLM_TIMEOUT=90
```

Qwen 示例（地址按账号地域调整）：

```dotenv
ANSWER_LLM_PROVIDER=qwen
ANSWER_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ANSWER_LLM_MODEL=你的Qwen模型名称
ANSWER_LLM_API_KEY=你的本地密钥
ANSWER_LLM_TIMEOUT=90
```

两组示例选择一组配置即可。原 `LLM_*` 继续用于非标准文档说明整理、候选规则提取；`EMBEDDING_*` 继续独立用于向量索引与检索。页面生成式问答有检索证据时始终调用回答模型，明确故障问题也不再绕过模型；空检索结果直接提示知识不足，不浪费模型请求。

新增生成式接口使用相同查询参数、权限和版本约定；返回 answer、citations、items 等，并增加 mode=grounded_generation 和 model_info（仅厂商和模型名）。生成文字不要求逐字照抄证据，citations.quote 必须是本次给模型的连续原文。引用编号由程序按 citations 顺序添加。模型失败或引用无效时报错，不退回摘录并假装生成成功；旧 /answers 及其客户端均保持不变。

## 专业规则

规则页以“目录＋详情”的方式阅读，无需先想好自然语言问题：

1. 选择设备类型及发布版本，获取该范围内完整的已发布规则。
2. 在目录中按故障名称或关键词查找，点击规则查看详情。
3. 详情集中展示故障名称、机理现象、原因、处理措施、原文判据及判断条件。
4. 条件以可读的“且／或”表达式和指标表呈现，保留指标、单位、上下限和比较符；来源与版本便于核对。

目录是只读浏览。修改或新增规则仍从“知识管理”进入对应文档，保存并逐条审核生效。未审核、未发布规则不出现在公共规则目录。

规则目录使用 `GET /api/v1/rules`，按设备类型精确读取**全部适用规则**，再在页面中搜索筛选；不会用 `/retrieve` 的 `top_k` 结果替代完整规则集。规则详情来源使用同一发布版本。没有规则时明确显示空状态，不补造条目。

规则页不执行数值判断，不提供健康评分或停机等级。设备数据、派生指标计算、规则执行与报告仍由外部 Agent 侧诊断工具负责。

## 接口与数据

七个 Agent 读取接口及 `equipment-rules-v1` 的指标、单位、阈值、比较符、`all/any`、派生公式含义不变。规则和引用经过既有公开响应模型；契约基线不因页面拆分而更新。

本分支无需迁移数据库或重新解析原文件。更新代码后重启服务并刷新浏览器即可；旧发布记录和来源保留。未配置回答模型时仍可查看检索依据及规则目录，生成式问答会明确返回配置错误，不伪装成成功回答。

更完整的对接约定见 [Agent 接口简版](agent_api.md)；审核与入库操作见 [简化确认流程](quick_confirmation.md)。

## 模型接口参考

- [DeepSeek Chat Completions 官方接口](https://api-docs.deepseek.com/api/create-chat-completion/)
- [Qwen 思考模式与兼容调用官方说明](https://www.alibabacloud.com/help/en/model-studio/deep-thinking)

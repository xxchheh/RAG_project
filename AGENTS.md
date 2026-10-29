# 本仓库开发约定

## Agent 对接接口兼容性（必须遵守）

- Agent 公开读取接口 `/api/v1` 的约定见 `docs/agent_api.md`；维护规则见 `docs/api_compatibility.md`。
- 对外输出必须经过 `rag_service/api_contract.py` 的响应模型；不得把内部数据结构直接当作新增公开约定。
- 不得删除或改名现有字段、改变字段类型/层级、增加必填请求参数，或改变默认行为、规则及版本含义。
- `all/any`、比较符、指标编号、单位、派生公式和 `equipment-rules-v1` 含义必须保持兼容。知识阈值修改通过审核、发布新 `release_id` 完成。
- 新增可选能力需同步更新说明及兼容测试；不兼容修改应新增 `/api/v2` 或新的规则格式版本，并保留旧版迁移路径，先与用户确认影响。
- 变更解析、切分、索引、模型或存储后仍须保留旧发布内容及来源；历史模型配置不可用时明确报错，不偷偷改用当前版本。
- API 修改后运行 `tests/test_agent_api_contract.py` 及相关回归测试。禁止为让测试通过直接覆盖契约基线；基线更新必须说明兼容影响。
- `tests/fixtures/agent_v1_contract.json` 是公开接口 OpenAPI 与规则能力基线。

- `.github/workflows/agent-api-contract.yml` 会在 push / PR 检查契约；不能通过跳过此测试来交付接口变更。

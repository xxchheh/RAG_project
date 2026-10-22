# 本地规则知识库维护

本分支用 SQLite 管理结构化判断条件、说明文字和来源。设备诊断按类型执行全部有效规则；关键词检索用于原理及措施问答，不使用向量 TopK 筛选诊断规则。当前设备与采集数据仍是模拟样本，没有接入 Java 系统。

## 初始化与查看

在项目根目录执行以下命令。默认数据库为当前目录下的 `knowledge/knowledge.db`；运行 Agent 也会自动初始化。首次迁移内置16条规则一次，后续启动或再次 `init` 不覆盖修改，也不会重新启用已停用的规则。`knowledge/` 和数据库文件已被 Git 忽略。

```bash
.venv/bin/python -m diagnostic_agent knowledge init
.venv/bin/python -m diagnostic_agent knowledge list
.venv/bin/python -m diagnostic_agent knowledge list --type motor
.venv/bin/python -m diagnostic_agent knowledge show R001
.venv/bin/python -m diagnostic_agent knowledge show R001 --revision 1
.venv/bin/python -m diagnostic_agent knowledge history R001
```

每条规则有固定编号、适用类型、名称、判断树、现象、原因、措施、原文和来源。修改产生新版本；草稿尚未生效，发布后才进入新分析。列表同时显示最新版本、草稿状态和当前发布版本。`show` 默认显示最新版本，可能是草稿。

自定义数据库时，`--db` 放在 `knowledge` 后、子命令前：

```bash
.venv/bin/python -m diagnostic_agent knowledge --db /path/to/knowledge.db list
```

也可在本地环境或 `.env` 设置 `DIAGNOSTIC_KNOWLEDGE_DB=/path/to/knowledge.db`，使 Agent 和维护命令使用同一数据库；命令行 `--db` 优先。未指定路径时，务必从同一个项目根目录启动。

## 从 Word 更新规则

先配置现有模型 API。导入会把 Word 中的规则段发送给该模型，生成结构化候选；本地校验后保存为草稿，并将原件归档到数据库旁的 `sources/` 目录。原 Word 不被修改。

```bash
.venv/bin/python -m diagnostic_agent knowledge import /path/to/规则修订.docx
.venv/bin/python -m diagnostic_agent knowledge diff R001
.venv/bin/python -m diagnostic_agent knowledge show R001
.venv/bin/python -m diagnostic_agent knowledge publish R001
```

文档每条规则需要可识别的标题和“机理规则”标签，可附“机理现象”“机理原因”“解决措施”等段落。支持 `.docx` 段落和表格文字，不能从截图或扫描图片识别文字。单次最多100条规则、10 MiB 文件。

同设备类型、同名规则自动创建原编号的新版本；新名称会创建新编号。规则更名时，用仅含一条规则的文档明确指定现有编号，避免意外新增：

```bash
.venv/bin/python -m diagnostic_agent knowledge import /path/to/单条规则.docx --rule-id R001
```

对照 `diff`、`show` 中的原文、条件树、来源和 `review_notes` 检查。分组、单位或算法等解释尚需复核时，普通发布会拒绝；确认本次解释后显式发布：

```bash
.venv/bin/python -m diagnostic_agent knowledge publish R001 --confirm-notes
# 也可以明确选择要发布的修订版
.venv/bin/python -m diagnostic_agent knowledge publish R001 --revision 2 --confirm-notes
```

模型提取不能代替业务核对。未知指标、虚构阈值或无效条件会拒绝导入；一个文档的候选全部通过后才一起写入草稿。对话 Agent 只有只读知识检索工具，不能通过聊天自动发布规则。

## 人工修改 JSON、停用与备份

`show` 对应程序的 `get_rule()`，输出可直接保存为 JSON 编辑，再创建新草稿：

```bash
.venv/bin/python -m diagnostic_agent knowledge show R001 > rule-edit.json
# 编辑 rule-edit.json 中的条件、说明及来源，保留原编号和设备类型
.venv/bin/python -m diagnostic_agent knowledge draft rule-edit.json
.venv/bin/python -m diagnostic_agent knowledge diff R001
.venv/bin/python -m diagnostic_agent knowledge publish R001
```

JSON 也接受规则列表或 `{"rules": [...]}`。新增规则省略 `id`，程序分配新编号；明确更新单条规则可加 `draft rule-edit.json --rule-id R001`。条件使用 `all`、`any` 组合，叶子使用 `metric` 及 `ge`、`gt`、`le`、`lt`；标签由程序生成，不执行 Python 或文档中的代码。维护阈值时同步核对原文及来源，避免文字依据与执行条件不一致。

```bash
.venv/bin/python -m diagnostic_agent knowledge disable R001
.venv/bin/python -m diagnostic_agent knowledge history R001
.venv/bin/python -m diagnostic_agent knowledge export /path/to/backup/knowledge.db
```

停用立即影响后续分析，历史版本保留。重新发布需要的版本可再次启用；不能发布比当前发布版本更旧的版本，如需恢复旧内容，应创建一个新修订版。

`export` 使用 SQLite 备份保存完整规则及版本，目标必须尚不存在。**原始 Word 附件不在数据库中：请把数据库旁的整个 `sources/` 目录一起备份。** 迁移后保留数据库和附件，并将 `DIAGNOSTIC_KNOWLEDGE_DB` 指向迁移后的数据库；规则原文摘录、版本和摘要保存在数据库中，旧分析也保留这些内容。

## 设备目录、分析快照与限制

设备清单独立保存在 `diagnostic_agent/config/devices.json`，或通过 `DIAGNOSTIC_DEVICE_CATALOG` 指向另一份同格式 JSON。每次查询重新读取，新增同类型设备、删除设备无需修改规则。设备 ID 必须唯一，测点需有唯一 ID 和受支持的模拟 `scenario`。规则中的测试标签不决定适用范围，设备的 `device_type` 才决定使用哪类规则。

类型固定为 `motor`、`coupling`、`pump`、`fan`、`gearbox`。当前支持速度有效值、加速度有效值和绝对峰值、非超额峭度、低频冲击值、1至9倍转频速度 RMS 幅值、1至2倍叶片通过频率幅值，以及现有比值和能量和派生计算。新增这些指标的规则可维护配置；新指标或新算法需要先增加程序支持。

每次 `analyze_device` 固定一份已发布且启用的规则快照，末次判断、历史扫描、解释和报告使用同一版本。报告、`analysis.json` 和清单保存快照编号及各规则版本；后续发布、改名或停用不改变已生成报告的依据。

Agent 共五个工具：查设备、取数据、分析、生成报告，以及只读 `search_knowledge(query, device_type?)`。后者只检索已发布且启用的知识，不能把检索到的故障说明当成该设备已发生故障的证据。

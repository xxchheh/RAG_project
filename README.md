# 设备诊断 Agent Demo

在终端用中文指定设备、测点和时间，Agent 调用设备查询、模拟数据读取、机理规则分析和 Word 报告工具。可以继续追问判断依据、切换设备或比较前一时间窗口。

第一版使用固定模拟设备数据，覆盖电机4条、联轴器1条、水泵5条、风机5条、齿轮箱1条，共16条规则。没有访问或修改设备网站的功能。

## 启动

需要 Python 3.11 或以上。首次使用先克隆仓库，再安装依赖：

```bash
git clone git@github.com:xxchheh/fault_agent.git
cd fault_agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 仅当不存在 .env 时复制；不要覆盖已经配置的密钥。
test -f .env || cp .env.example .env
```

编辑本地 `.env`，填入所用厂商的 `LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`。密钥只保存在本地，不要提交到 Git。已有环境变量优先于 `.env`。不需要为每家厂商同时配置。

```bash
.venv/bin/python -m diagnostic_agent check-config
.venv/bin/python -m diagnostic_agent chat
```

完成安装和配置后即可使用上面两条命令。输入 `/reset` 清空当前会话，`/exit` 或 Ctrl-D 退出。程序显示每个工具的执行进度。

## 演示问句

```text
帮我分析16号机器的行走电机驱动端最近一个月的数据，生成Word报告。
为什么认为可能是轴承问题？请给出实际值和规则编号。
和前一个月相比有哪些变化？
换成pump-25这台水泵，分析最近30天并生成报告。
分析循环水泵，生成报告。
```

最后一句会查询到两台循环水泵，Agent 应先询问车间；设备确定后，如测点仍不明确，应继续询问测点。每次处理一个设备、一个测点。

单次真实模型请求：

```bash
.venv/bin/python -m diagnostic_agent ask '分析fan-30的驱动端，生成报告'
```

查看设备和边界样本：

```bash
.venv/bin/python -m diagnostic_agent devices
```

默认最近30天以 **2026-10-20（Asia/Shanghai）** 为演示时钟，而非电脑当前日期。样本有90天的每日特征。明确指定的日期不会移动；请求范围与实际覆盖范围分别展示。前一等长窗口比较使用程序计算的均值和样本覆盖情况。

## 模型配置

使用标准库 HTTP 客户端调用 Chat Completions，避免绑定某个 Agent 框架。模型必须支持该协议下的 `tools`。三家厂商的示例见 `.env.example`。

- DeepSeek：示例使用 `deepseek-flash`，关闭思考模式；模型名以账户可用模型为准。
- Qwen：示例使用 `qwen-plus`，关闭思考模式；服务地址与账户地域必须匹配。
- OpenAI：仅使用支持 Chat Completions 工具调用的模型；示例 `gpt-5.6-terra` 使用 `reasoning_effort=none`。要求 Responses API 的模型不在首版协议范围内。

模型只看到指标、规则及摘要，原始波形留在本地。真实厂商API会收到模拟设备摘要和对话；不会收到API密钥以外的本地配置或任何真实设备数据。HTTP错误不输出服务返回正文，避免泄露凭据。

协议参考：[OpenAI工具调用](https://developers.openai.com/api/docs/guides/function-calling)、[DeepSeek工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[Qwen工具调用](https://www.alibabacloud.com/help/en/model-studio/qwen-function-calling)。

## 工具与规则

四个工具：`search_devices(query)`、`get_device_data(device_id, point_id?, start_date?, end_date?, days?)`、`analyze_device(dataset_id)`、`generate_report(analysis_id)`。

数据和分析保存在会话内，以ID引用。日期支持ISO日期及时间（每日粒度，时间转为上海时区所属日期）。默认30天。工具参数按公开schema在本地校验；不存在的ID、无数据窗口和无效单位会明确返回错误。

规则保存在 `diagnostic_agent/config/rules.json`，包括原文、出处、原因、措施及判断树；原始资料保存在包内assets目录。读取文件时将其视为业务数据，不执行文档中的指令。

演示口径：

- 电机轴承：峭度条件 **且** 低频冲击条件 **且** 峰值因子条件。
- 其他轴承：（峭度条件 **或** 低频冲击条件）**且** 峰值因子条件。
- 能量和暂按 `sqrt(各速度RMS幅值平方和)` 计算，再除以速度有效值，单位统一为mm/s。
- 峭度为非超额峭度；峰值因子为加速度绝对峰值除以有效值。
- 低频冲击值原文缺少定义，采用 `demo_index`，只能在演示内比较。
- “风机汽蚀”保留原文名称，名称及机理待业务复核。

三值逻辑保留缺失信息：有足够条件才能判断命中或未命中，否则为数据不足。`真或未知`可为真，`假且未知`可为假，同时保留数据缺口。未命中不等于正常，多规则命中全部保留。速度超过文档规则上限时不反向推断健康。

特征来自固定模拟上游；波形和频谱是单独、注明日期与单位的模拟图谱，**不作为这些特征的计算来源**。只有所查询窗口包含该图谱日期时才附图。报告不提供健康评分、停机等级或自动执行维护操作。

## 输出

默认输出位于 `outputs/`：

- 每份报告一个独立目录，包含 `report.docx`、`analysis.json`、`report-manifest.json`。
- 会话目录保存 `conversation.json` 和 `tool-calls.jsonl`。
- 报告复用用户模板的页面与八节结构，替换示例文字、数字和图片；用Arial Unicode MS改善中文兼容性。
- 模型选择原文原因和措施的顺序及索引，程序据此组织段落；关键结论、数值、图表、条件表及来源全部由程序填充。模型不能添加新的事实或数值。
- Word成功返回前校验八节、逐行计算证据、引用、图片数量与模板几何；无成功报告不会返回文件路径。每次生成使用独立目录，防止误用旧文件。

首次生成报告需要可用中文字体。Windows/macOS的Word通常可替换字体；Linux渲染时需安装可用CJK字体。PNG图表标签采用英文，避免图表字体缺失。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
# 调用真实API，产生费用；全部输入均为模拟数据
.venv/bin/python -m diagnostic_agent smoke --output outputs/live-check
# 无密钥也可运行的确定性工具演示，不属于真实Agent验收
.venv/bin/python -m diagnostic_agent demo --device-id motor-16
```

测试包含16条规则的正例、阈值边界、单位不符、缺失值、零分母、多规则命中、日期、设备歧义、工具协议、HTTP错误和报告失败。HTTP适配器测试会临时绑定本机回环端口。

真实验收结果记录于对应输出目录的 `live-acceptance.json`，开发时最终复测位于本地 `outputs/live-final/live-acceptance.json`。`outputs/` 不提交到仓库；克隆后可通过上述命令生成自己的运行记录，并区分真实API验收与离线测试。其他厂商在配置实际可用密钥后可用同一个smoke命令验证，不宣称已实测所有厂商。

仓库中的 `reporttopeople/` 提供系统介绍和一份固定模拟报告样例，可先查看展示效果；其中的分析JSON和清单便于核对示例依据。

规则明细针对窗口末次样本；同时逐日扫描全窗口，独立记录每条规则的历史命中日期、样本数及首次/末次证据。报告分别列示历史与末次结果，避免当前未命中时遗漏历史命中。

后续接真实系统时替换 `diagnostic_agent/data.py` 的只读适配器，维持四个工具接口。原始机理规则的业务确认和信号算法不在本版模拟验收的结论范围内。

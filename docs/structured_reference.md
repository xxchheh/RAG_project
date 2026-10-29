# 真实参考资料解析与检索

## 本轮选择

核对日期：2026-10-29。PyPI 正式版本：Docling 2.145.0、RAG-Anything 1.4.4、LightRAG-HKU 1.5.7、MinerU 4.1.0。

- [LightRAG 主线](https://github.com/HKUDS/LightRAG)已说明合并多模态解析能力；主线状态不等同于所有 PyPI 发行版。它不是 OCR 引擎。
- [RAG-Anything](https://github.com/HKUDS/RAG-Anything)当前说明其 MinerU CLI 适配要求 3.x、不兼容 4.x。不能机械安装最新 MinerU 再假定两者兼容。
- 本机为 Apple Silicon Mac。第一轮直接复用 [Docling](https://github.com/docling-project/docling) 的版面、表格、OCR 和 HierarchicalChunker，不引入重复的图谱数据库及 LLM 抽取流水线。图谱尚无真实收益证据。
- 中文 OCR 使用 RapidOCR 3.10.0；普通检索使用 jieba 0.42.1、rank-bm25 0.2.2 与现有真实向量。诊断规则执行、规则含义及 v1 适配边界不变。

## 安装与启动

```bash
# 项目根目录；Python 3.12，以下完整锁定环境在 macOS arm64 验证
python3 -m venv .venv-docling
.venv-docling/bin/python -m pip install -r requirements-parser.lock
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m rag_service serve
```

配置见 `.env.example`。`auto` 在存在隔离 Python 且经服务上传时采用 Docling；`docling` 强制采用它；`legacy` 显式使用旧解析。不会在 Docling 出错时偷偷退回旧 OCR 并报告成功。首次官方模型下载约 GB 级，保存在 `data/rag_work/parsed/models`，不上传原文件给云解析服务。

机理规则 Word 保留现有确定性流程。PDF/PPTX **参考资料不运行诊断规则提取模型**，例题、公式和经验数值不会被提升成正式规则。

## 处理与核对

1. 上传时选择“普通参考文档”。服务上传上限改为 100 MiB，Docling 路径不再受旧 100 页限制。
2. PDF 逐物理页解析，PPTX 原生解析并对嵌入图片追加 Docling OCR；进度显示当前页。
3. 结构化原始 JSON、Markdown、原页/图片、成功检查点和失败 manifest 保存在文件哈希及解析版本隔离目录。失败不会发布部分内容；重试复用成功页，不会删掉难页。
4. 审核页“核对本页／核对PDF原页”按原 PDF 物理页取图；“处理及失败页”显示检查点及失败原因。原始 PPTX 仍可下载，图片区域有“核对图像区域”入口，并保存在解析目录；点击预览图可放大核对。
5. 公式默认不做慢速的生成式补全，公式区域标为待修正并从公共检索隔离。核对原页后，人工修正内容、改为说明，再重新审核发布。`DOCLING_FORMULA=true` 可试生成 LaTeX，但仍须人工核对，不能凭模型输出认定正确。
6. 已审核的表格保留表头与完整结构化文本，不再固定 350 字截断；章节、图注与来源页保留。完整 Docling JSON 提供更细的表格单元格、图像和坐标记录。
7. 发布仍由现有事务在索引准备完成后切换。历史 release 不重写；旧索引仍能查询，新索引增加中文 BM25 与去重，不改变 Agent 字段含义。

## 旧数据迁移

备份整个 `data/rag_work/`。文档列表点击“重新解析”，选择参考资料，重新核对审核后发布。新修订版处理期间，旧发布内容保持可用。无需迁移原文件、来源或审核历史表；不要覆盖旧数据库或删除旧发布快照。

## 真实资料验收与限制

原书为 169 页《机械振动基础》，另使用真实《示例.pptx》。原页、目录、正文、公式及表格应对照 `outputs/` 实际记录核对，不能将脚本成功当作识别准确率。

初次代表页已发现：第 25 物理页的章节、列表、正文得到保留，但“由于”存在误识别；第 12 页目录被版面模型当成表格，部分页码识别错误；第 60 页生成公式出现 `er²` 被读作 `e^r`、导数阶数错误和一个未解码公式。因此目录保留供审核但不作为检索证据，公式必须隔离，不声称该方案已准确识别数学。

单独运行解析与隔离验收：

```bash
HF_HOME="$PWD/data/rag_work/parsed/models" .venv-docling/bin/python scripts/docling_worker.py '/path/book.pdf' outputs/book
# 代表页试解析：末尾可传 12,25,60,152；不能用代表页结果冒充整本验收
.venv/bin/python scripts/accept_structured_reference.py --source '/path/book.pdf' --parsed outputs/book
```

验收脚本只在临时库验证审核、发布、索引、Agent 查询与来源映射，不代表全书已由业务审核。不要将测试库中的自动审核搬到日常业务流程。

模型快照在首次验证时为：
- CodeFormulaV2：`ecedbe111d15c2dc60bfd4a823cbe80127b58af4`（可选公式生成）。
- docling-models：`fc0f2d45e2218ea24bce5045f58a389aed16dc23`。
- docling-layout-heron：`8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`。

锁文件锁定 Python 包；模型缓存及解析产物也需一起备份。更换模型缓存或配置后需新建解析版本再审核，不能用旧检查点冒充新模型结果。跨页连续章节的标题恢复、复杂合并表格、小字号/模糊扫描、数学符号和插图语义仍需人工核对。未配置视觉模型，不生成图片解释。


## 本轮实际结果

- 169 个 PDF 物理页均保留处理记录。第 143 页为未形成文本的工程插图，保留图像待复核，不伪造正文；其余失败重试复用了已成功的 168 页。
- 487 个公式候选隔离待修正；另 1 个图像条目待复核。临时验收库发布了 1226 个已选说明条目，不代表这些 OCR 文字已获得业务认可。
- 真实本地中文向量加 BM25 查询：“机械振动研究哪些问题”命中物理页25；响应分析问题命中页25；临界转速与共振区别命中页60。“晚饭吃什么”“如何办理护照”返回空结果。
- 对已验证的 `BAAI/bge-small-zh-v1.5`，新索引在没有名称或 BM25 证据且余弦相似度低于0.45时排除候选。该门槛是本轮少量正负例校准，不是置信度，也不保证所有问法；其他向量模型不套用这个数值。
- 真实7页《示例.pptx》包含14个嵌入图片OCR区域，形成28个说明候选。报告生成、机器编号不存在和知识库调用问题分别命中相关幻灯片；两项无关问题均为空。
- 记录：`outputs/structured-acceptance.json`、`outputs/pptx-acceptance.json`、`outputs/docling-probe/`（含开启生成式公式识别的失败质量样例）、`outputs/structured-preview.png`。这些本地数据及原资料不提交Git。
- 已将相同文件与解析版本的检查点准备在 `data/rag_work/parsed/`；上传上述两个原文件可复用。现有业务库、规则及原发布版本没有自动改写。

以上只验证了上述真实文件及少量问题。没有比较所有OCR方案或证明字符准确率；MinerU尚未在本机完成实测，不能声称它劣于或优于Docling。数学公式与无文字插图语义仍是明确的未完成能力，须逐项校准或后续更换经过实测的专用模型。

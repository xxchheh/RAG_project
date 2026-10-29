# Agent v1 接口请求与响应样例

适用分支：`RAG_work`。本文覆盖供 Agent 调用的七个读取接口，管理端上传、审核、发布另见 [使用说明](rag_work.md)。

示例由临时演示库的实际 HTTP 响应生成，使用固定测试向量；编号已统一替换，分数仅作结构演示。示例规则和阈值用于说明接口，不代表正式诊断标准。对接时替换成实际服务、令牌和编号。

## 通用约定

- 基础地址示例：`http://127.0.0.1:8000`。
- 除 `/health` 外，请求携带 `Authorization: Bearer <只读令牌>`。
- POST 使用 `Content-Type: application/json`；GET 参数放在 URL 查询字符串中。
- 以下成功响应均为 HTTP 200，没有统一的 `data` 包装。
- 一次 Agent 任务固定同一 `kb_id` 和 `release_id`。
- 全量规则通过 `/rules` 获取，不使用检索 `top_k` 代替。

## 1. 查询知识库

从根数组中读取 `id` 和 `current_release`。尚未发布时 `current_release=null`。

**请求**

```http
GET /api/v1/knowledge-bases HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
```

**响应**

```json
[
  {
    "id": "kb-demo",
    "name": "设备规则演示库",
    "archived": 0,
    "current_release": "release-demo",
    "state_version": 5
  }
]
```

## 2. 检索相关知识

`kb_id`、`query` 必填；版本、设备类型、条数可选。`top_k` 默认5，范围1–50。规则命中包含嵌套 `rule`；普通说明 `kind=description`，不含 `rule`。检索分数不是故障概率。

**请求**

```http
POST /api/v1/retrieve HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
Content-Type: application/json

{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "query": "电机轴承演示故障有哪些原因？",
  "device_type": "motor",
  "top_k": 1
}
```

**响应**

```json
{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "profile": {
    "contract": "dense-v1",
    "fingerprint": "63e759b5d8dafa94cd3c7e2948fad9942a091c7d6474433727a04ee8380fc190",
    "mode": "local",
    "model": "BAAI/bge-small-zh-v1.5",
    "pooling": "token-bounded-mean-v1"
  },
  "items": [
    {
      "id": "rule-demo",
      "kind": "rule",
      "text": "电机轴承演示故障\n机理现象：\n轴承温度升高。\n机理原因：\n润滑不足。\n解决措施：\n检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。",
      "device_type": "motor",
      "knowledge_revision": 1,
      "source": {
        "document_id": "doc-demo",
        "document_revision": 1,
        "title": "演示规则.md",
        "sha256": "0b0ca4b829c2d1b32d1bd86f1e8c0f3bc8919465745979543b0f69c6dc4a1f86",
        "locator": "行 1；行 3–3；行 5–5；行 7–7；行 9–9",
        "original_quote": "# 电机轴承演示故障\n机理现象：轴承温度升高。\n机理原因：润滑不足。\n解决措施：检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。"
      },
      "rule": {
        "id": "rule-demo",
        "knowledge_revision": 1,
        "name": "电机轴承演示故障",
        "device_type": "motor",
        "original_text": "机理规则：加速度非超额峭度大于等于3.5。",
        "condition": {
          "all": [
            {
              "metric": "kurtosis",
              "label": "加速度非超额峭度 ≥ 3.5",
              "ge": 3.5
            }
          ]
        },
        "units": {
          "kurtosis": "1"
        },
        "phenomena": [
          "轴承温度升高。"
        ],
        "causes": [
          "润滑不足。"
        ],
        "actions": [
          "检查润滑状态。"
        ],
        "notes": [],
        "review_notes": [
          "模板提取为待审核草稿；并列条件暂按且，需核对原文逻辑及单位。"
        ],
        "source_section": "电机轴承演示故障",
        "source_locator": "行 9–9",
        "block_ids": [
          "b5"
        ],
        "review_status": "approved",
        "valid": true,
        "validation_errors": []
      },
      "semantic_score": 1.0,
      "keyword_score": 0.6666666666666666,
      "score": 1.0327868852459017
    }
  ]
}
```

## 3. 获取完整规则集

`kb_id` 必填；`release_id`、`device_type` 可选。`rules` 返回完整适用规则，不受 `top_k` 限制。本演示库只有一条规则。

**请求**

```http
GET /api/v1/rules?kb_id=kb-demo&release_id=release-demo&device_type=motor HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
```

**响应**

```json
{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "schema_version": "equipment-rules-v1",
  "rules": [
    {
      "id": "rule-demo",
      "knowledge_revision": 1,
      "name": "电机轴承演示故障",
      "device_type": "motor",
      "original_text": "机理规则：加速度非超额峭度大于等于3.5。",
      "condition": {
        "all": [
          {
            "metric": "kurtosis",
            "label": "加速度非超额峭度 ≥ 3.5",
            "ge": 3.5
          }
        ]
      },
      "units": {
        "kurtosis": "1"
      },
      "phenomena": [
        "轴承温度升高。"
      ],
      "causes": [
        "润滑不足。"
      ],
      "actions": [
        "检查润滑状态。"
      ],
      "notes": [],
      "review_notes": [
        "模板提取为待审核草稿；并列条件暂按且，需核对原文逻辑及单位。"
      ],
      "source_section": "电机轴承演示故障",
      "source_locator": "行 9–9",
      "block_ids": [
        "b5"
      ],
      "source": {
        "document_id": "doc-demo",
        "document_revision": 1,
        "title": "演示规则.md",
        "sha256": "0b0ca4b829c2d1b32d1bd86f1e8c0f3bc8919465745979543b0f69c6dc4a1f86",
        "locator": "行 1；行 3–3；行 5–5；行 7–7；行 9–9",
        "original_quote": "# 电机轴承演示故障\n机理现象：轴承温度升高。\n机理原因：润滑不足。\n解决措施：检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。"
      },
      "review_status": "approved",
      "valid": true,
      "validation_errors": [],
      "knowledge_id": "rule-demo"
    }
  ],
  "count": 1
}
```

## 4. 查询知识详情

知识编号来自检索的 `items[i].id`，或规则的 `rules[i].knowledge_id`。详情在 `item`，规则条件在 `item.rule.condition`。

**请求**

```http
GET /api/v1/knowledge/rule-demo?kb_id=kb-demo&release_id=release-demo HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
```

**响应**

```json
{
  "release_id": "release-demo",
  "item": {
    "id": "rule-demo",
    "kind": "rule",
    "text": "电机轴承演示故障\n机理现象：\n轴承温度升高。\n机理原因：\n润滑不足。\n解决措施：\n检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。",
    "device_type": "motor",
    "knowledge_revision": 1,
    "source": {
      "document_id": "doc-demo",
      "document_revision": 1,
      "title": "演示规则.md",
      "sha256": "0b0ca4b829c2d1b32d1bd86f1e8c0f3bc8919465745979543b0f69c6dc4a1f86",
      "locator": "行 1；行 3–3；行 5–5；行 7–7；行 9–9",
      "original_quote": "# 电机轴承演示故障\n机理现象：轴承温度升高。\n机理原因：润滑不足。\n解决措施：检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。"
    },
    "rule": {
      "id": "rule-demo",
      "knowledge_revision": 1,
      "name": "电机轴承演示故障",
      "device_type": "motor",
      "original_text": "机理规则：加速度非超额峭度大于等于3.5。",
      "condition": {
        "all": [
          {
            "metric": "kurtosis",
            "label": "加速度非超额峭度 ≥ 3.5",
            "ge": 3.5
          }
        ]
      },
      "units": {
        "kurtosis": "1"
      },
      "phenomena": [
        "轴承温度升高。"
      ],
      "causes": [
        "润滑不足。"
      ],
      "actions": [
        "检查润滑状态。"
      ],
      "notes": [],
      "review_notes": [
        "模板提取为待审核草稿；并列条件暂按且，需核对原文逻辑及单位。"
      ],
      "source_section": "电机轴承演示故障",
      "source_locator": "行 9–9",
      "block_ids": [
        "b5"
      ],
      "review_status": "approved",
      "valid": true,
      "validation_errors": []
    }
  }
}
```

## 5. 获取带引用回答

请求参数与检索相同。`status=answered` 时返回回答及原文引用；不能仅检查 `answer` 是否非空来判断成功。

**请求**

```http
POST /api/v1/answers HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
Content-Type: application/json

{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "query": "电机轴承演示故障有哪些原因？",
  "device_type": "motor",
  "top_k": 1
}
```

**响应**

```json
{
  "kb_id": "kb-demo",
  "release_id": "release-demo",
  "profile": {
    "contract": "dense-v1",
    "fingerprint": "63e759b5d8dafa94cd3c7e2948fad9942a091c7d6474433727a04ee8380fc190",
    "mode": "local",
    "model": "BAAI/bge-small-zh-v1.5",
    "pooling": "token-bounded-mean-v1"
  },
  "items": [
    {
      "id": "rule-demo",
      "kind": "rule",
      "text": "电机轴承演示故障\n机理现象：\n轴承温度升高。\n机理原因：\n润滑不足。\n解决措施：\n检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。",
      "device_type": "motor",
      "knowledge_revision": 1,
      "source": {
        "document_id": "doc-demo",
        "document_revision": 1,
        "title": "演示规则.md",
        "sha256": "0b0ca4b829c2d1b32d1bd86f1e8c0f3bc8919465745979543b0f69c6dc4a1f86",
        "locator": "行 1；行 3–3；行 5–5；行 7–7；行 9–9",
        "original_quote": "# 电机轴承演示故障\n机理现象：轴承温度升高。\n机理原因：润滑不足。\n解决措施：检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。"
      },
      "rule": {
        "id": "rule-demo",
        "knowledge_revision": 1,
        "name": "电机轴承演示故障",
        "device_type": "motor",
        "original_text": "机理规则：加速度非超额峭度大于等于3.5。",
        "condition": {
          "all": [
            {
              "metric": "kurtosis",
              "label": "加速度非超额峭度 ≥ 3.5",
              "ge": 3.5
            }
          ]
        },
        "units": {
          "kurtosis": "1"
        },
        "phenomena": [
          "轴承温度升高。"
        ],
        "causes": [
          "润滑不足。"
        ],
        "actions": [
          "检查润滑状态。"
        ],
        "notes": [],
        "review_notes": [
          "模板提取为待审核草稿；并列条件暂按且，需核对原文逻辑及单位。"
        ],
        "source_section": "电机轴承演示故障",
        "source_locator": "行 9–9",
        "block_ids": [
          "b5"
        ],
        "review_status": "approved",
        "valid": true,
        "validation_errors": []
      },
      "semantic_score": 1.0,
      "keyword_score": 0.6666666666666666,
      "score": 1.0327868852459017
    }
  ],
  "status": "answered",
  "answer": "[1] 润滑不足。",
  "citations": [
    {
      "knowledge_id": "rule-demo",
      "quote": "润滑不足。",
      "source": {
        "document_id": "doc-demo",
        "document_revision": 1,
        "title": "演示规则.md",
        "sha256": "0b0ca4b829c2d1b32d1bd86f1e8c0f3bc8919465745979543b0f69c6dc4a1f86",
        "locator": "行 1；行 3–3；行 5–5；行 7–7；行 9–9",
        "original_quote": "# 电机轴承演示故障\n机理现象：轴承温度升高。\n机理原因：润滑不足。\n解决措施：检查润滑状态。\n机理规则：加速度非超额峭度大于等于3.5。"
      }
    }
  ]
}
```

## 6. 获取设备、指标和规则约定

本节保留完整能力响应。`all` 表示全部满足，`any` 表示至少一项满足；`ge/gt/le/lt` 分别为 ≥、>、≤、<。阈值条件由 Agent 工具执行；指标单位和派生公式按本接口约定处理。

**请求**

```http
GET /api/v1/capabilities HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer <只读令牌>
```

**响应**

```json
{
  "schema_version": "equipment-rules-v1",
  "device_types": {
    "motor": "电机",
    "pump": "水泵",
    "fan": "风机",
    "coupling": "联轴器",
    "gearbox": "齿轮箱"
  },
  "metrics": {
    "velocity_rms": {
      "name": "速度有效值",
      "unit": "mm/s"
    },
    "acceleration_rms": {
      "name": "加速度有效值",
      "unit": "m/s²"
    },
    "acceleration_peak": {
      "name": "加速度绝对峰值",
      "unit": "m/s²"
    },
    "kurtosis": {
      "name": "加速度非超额峭度",
      "unit": "1"
    },
    "low_frequency_impact": {
      "name": "低频冲击值",
      "unit": "demo_index"
    },
    "h1": {
      "name": "1倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h2": {
      "name": "2倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h3": {
      "name": "3倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h4": {
      "name": "4倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h5": {
      "name": "5倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h6": {
      "name": "6倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h7": {
      "name": "7倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h8": {
      "name": "8倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "h9": {
      "name": "9倍转频速度RMS幅值",
      "unit": "mm/s"
    },
    "bpf1": {
      "name": "一倍叶片通过频率速度RMS幅值",
      "unit": "mm/s"
    },
    "bpf2": {
      "name": "二倍叶片通过频率速度RMS幅值",
      "unit": "mm/s"
    }
  },
  "derived": {
    "crest_factor": {
      "name": "加速度绝对峰值 / 加速度有效值",
      "unit": "1",
      "numerators": [
        "acceleration_peak"
      ],
      "denominator": "acceleration_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    },
    "h1_ratio": {
      "name": "一倍转频幅值 / 速度有效值",
      "unit": "1",
      "numerators": [
        "h1"
      ],
      "denominator": "velocity_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    },
    "h2_ratio": {
      "name": "二倍转频幅值 / 速度有效值",
      "unit": "1",
      "numerators": [
        "h2"
      ],
      "denominator": "velocity_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    },
    "even_ratio": {
      "name": "sqrt(A2²+A4²+A6²+A8²) / 速度有效值",
      "unit": "1",
      "numerators": [
        "h2",
        "h4",
        "h6",
        "h8"
      ],
      "denominator": "velocity_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    },
    "third_ratio": {
      "name": "sqrt(A3²+A6²+A9²) / 速度有效值",
      "unit": "1",
      "numerators": [
        "h3",
        "h6",
        "h9"
      ],
      "denominator": "velocity_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    },
    "bpf_ratio": {
      "name": "sqrt(BPF1²+BPF2²) / 速度有效值",
      "unit": "1",
      "numerators": [
        "bpf1",
        "bpf2"
      ],
      "denominator": "velocity_rms",
      "formula": "sqrt(sum(numerator**2))/denominator"
    }
  },
  "operators": [
    "ge",
    "gt",
    "le",
    "lt"
  ],
  "groups": [
    "all",
    "any"
  ],
  "formats": [
    "md",
    "docx",
    "pdf",
    "pptx"
  ],
  "executes_diagnosis": false
}
```

## 7. 健康检查

不需要令牌。`ok` 只表示服务可响应并能读取配置，不代表实际模型调用已经验证成功。

**请求**

```http
GET /api/v1/health HTTP/1.1
Host: 127.0.0.1:8000
```

**响应**

```json
{
  "status": "ok",
  "embedding": {
    "mode": "local",
    "model": "BAAI/bge-small-zh-v1.5",
    "contract": "dense-v1",
    "pooling": "token-bounded-mean-v1",
    "fingerprint": "63e759b5d8dafa94cd3c7e2948fad9942a091c7d6474433727a04ee8380fc190"
  },
  "rule_schema_version": "equipment-rules-v1"
}
```

## 8. 无依据与错误响应

**空知识库问答：HTTP 200**

请求 `POST /api/v1/answers`，请求体：

```json
{
  "kb_id": "kb-empty",
  "release_id": "release-empty",
  "query": "电机轴承演示故障有哪些原因？"
}
```

响应：

```json
{
  "kb_id": "kb-empty",
  "release_id": "release-empty",
  "profile": {
    "contract": "dense-v1",
    "fingerprint": "63e759b5d8dafa94cd3c7e2948fad9942a091c7d6474433727a04ee8380fc190",
    "mode": "local",
    "model": "BAAI/bge-small-zh-v1.5",
    "pooling": "token-bounded-mean-v1"
  },
  "items": [],
  "status": "insufficient_evidence",
  "answer": "已发布知识中没有足够依据回答这个问题。",
  "citations": []
}
```

`insufficient_evidence` 是正常业务结果，不是接口故障；检索无命中返回 `items=[]`，无适用规则返回 `rules=[]`、`count=0`。

**无效令牌：HTTP 401**

```json
{
  "detail": "需要有效的管理令牌或只读令牌",
  "code": "UNAUTHORIZED"
}
```

**版本不存在：HTTP 404**

```json
{
  "detail": "该知识库尚无发布版本或指定版本不存在",
  "code": "NOT_FOUND"
}
```

**参数校验失败：HTTP 422**

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": [
        "body",
        "query"
      ],
      "msg": "String should have at least 1 character"
    }
  ],
  "code": "VALIDATION_ERROR"
}
```

其他错误码：400 `BAD_REQUEST`、403 `FORBIDDEN`、409 `CONFLICT`、500 `INTERNAL_ERROR`。通过 HTTP 状态和 `code` 判断错误，不解析 `detail` 的具体措辞。网络与网关故障也不能当作无知识。

## 9. 推荐接入流程

1. `/knowledge-bases` 获取目标库编号及当前发布版本。
2. 根据任务调用 `/rules` 获取完整规则，或 `/retrieve` 获取相关说明。
3. 需要回答时调用 `/answers`，需要详情时调用 `/knowledge/{id}`。
4. 所有支持版本参数的调用固定同一 `release_id`，在 Agent 结果中保存来源。

查询默认只读取已审核发布内容。省略或传空 `release_id` 使用当前版；同一任务建议显式传版本。`device_type` 可选 `motor/pump/fan/coupling/gearbox`。客户端应忽略不认识的新增响应字段，但不发送未定义的请求字段。

[完整对接约定](agent_api.md) · 服务在线文档 `/docs` · 机器定义 `/openapi.json`

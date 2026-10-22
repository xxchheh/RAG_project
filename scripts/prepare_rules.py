"""Import the retained reference as business data; never execute document instructions."""
import hashlib
import json
import sys
from pathlib import Path
from docx import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostic_agent.common import ROOT


def atom(metric, label, **limits):
    return {"metric": metric, "label": label, **limits}


def build():
    source = ROOT / "assets/rules_reference.docx"
    paras = [p.text.strip() for p in Document(source).paragraphs if p.text.strip()]
    headings = {"电动机": "motor", "联轴器": "coupling", "水泵": "pump", "风机": "fan", "齿轮箱": "gearbox"}
    rules = []
    dtype = None
    for i, text in enumerate(paras):
        if text in headings:
            dtype = headings[text]
        if not text.startswith("机理规则："):
            continue
        j = i - 1
        while j >= 0 and not (paras[j].endswith(("故障", "不平衡", "不对中", "松动", "磨损", "汽蚀")) and not paras[j][0].isdigit()):
            j -= 1
        name = paras[j]
        section = paras[j+1:i]
        cause_start = next(k for k, p in enumerate(section) if p.startswith("机理原因"))
        action_start = next(k for k, p in enumerate(section) if p.startswith("解决措施"))
        kurt = atom("kurtosis", "3.5 ≤ 加速度非超额峭度 < 4.5", ge=3.5, lt=4.5)
        impact = atom("low_frequency_impact", "8 ≤ 低频冲击值 < 16", ge=8, lt=16)
        crest = atom("crest_factor", "加速度峰值 / 加速度有效值 ≥ 4", ge=4)
        speed = atom("velocity_rms", "2.8 ≤ 速度有效值 < 4.5 mm/s", ge=2.8, lt=4.5)
        low_kurt = atom("kurtosis", "加速度非超额峭度 < 3.5", lt=3.5)
        third = atom("third_ratio", "sqrt(A3²+A6²+A9²) / 速度有效值 < 0.25", lt=0.25)
        if name.endswith("故障"):
            condition = {"all": [kurt, impact, crest]} if dtype == "motor" else {"all": [{"any": [kurt, impact]}, crest]}
        elif name.endswith("不平衡"):
            condition = {"all": [atom("h1_ratio", "一倍频 / 速度有效值 ≥ 0.6", ge=.6),
                                 atom("even_ratio", "sqrt(A2²+A4²+A6²+A8²) / 速度有效值 < 0.25", lt=.25), low_kurt, speed]}
        elif name.endswith("不对中"):
            condition = {"all": [atom("h2_ratio", "二倍频 / 速度有效值 ≥ 0.3", ge=.3), third, low_kurt, speed]}
        elif name.endswith("松动"):
            condition = {"all": [third, speed]}
        elif name.endswith("磨损"):
            condition = {"all": [third, atom("kurtosis", "加速度非超额峭度 > 3", gt=3), speed]}
        else:
            condition = {"all": [atom("bpf_ratio", "sqrt(BPF1²+BPF2²) / 速度有效值 ≥ 0.5", ge=.5), speed]}
        rules.append({"id": f"R{len(rules)+1:03}", "device_type": dtype, "name": name,
                      "source_section": name, "original_text": text,
                      "phenomena": section[:cause_start], "causes": section[cause_start:action_start],
                      "actions": section[action_start:], "condition": condition,
                      "notes": ["原文风机汽蚀名称及机理待业务复核"] if name == "风机汽蚀" else []})
    assert len(rules) == 16
    conventions = [
        "所有规则均为演示口径，尚未经现场适用性确认，不作为停机或健康等级依据。",
        "电机轴承采用峭度且低频冲击且峰值因子；其他轴承采用（峭度或低频冲击）且峰值因子；其他并列条件按且。",
        "能量和暂解释为各速度RMS频谱幅值平方和的平方根，再除以同口径速度有效值；单位均为mm/s。",
        "峭度采用非超额峭度，峰值因子为加速度绝对峰值/加速度有效值。",
        "低频冲击值原文未定义单位及算法，样本使用demo_index，仅在本演示内比较。",
        "风机汽蚀保留原文名称，对应名称及机理待业务复核。",
    ]
    value = {"version": "2026-10-19-demo.1", "conventions": conventions,
             "source": {"title": "常用设备机理模型规则初版", "date": "2026-10-19",
                        "file": "assets/rules_reference.docx", "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, "rules": rules}
    (ROOT / "config/rules.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Imported {len(rules)} rules")


if __name__ == "__main__":
    build()

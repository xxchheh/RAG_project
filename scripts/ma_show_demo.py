"""Launch reproducible presentation cases with labelled fixed model outputs."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_service.app import create_app
from rag_service.config import Settings


NORMAL_QUESTION = '电机轴承润滑不足应该先检查什么？'
CONFLICT_QUESTION = '电机一倍频升高，就可以直接判定转子不平衡吗？'
DEMO_PASSAGES = [
    '电机一倍频升高\n一倍频突出是转子不平衡常见特征之一。',
    '电机一倍频升高\n解释该现象时应核对其他频率成分和适用条件。',
    '电机一倍频升高\n振动特征可为排查提供线索。',
    '电机一倍频升高\n相似的振动现象可能涉及多种故障，需要综合排查。',
    '电机一倍频升高\n作出判断前需要核对完整机理判据。',
    '电机转子不平衡的完整判据\n本演示采用以下联合条件：一倍频速度RMS幅值与速度有效值之比≥0.6；'
    '偶次频幅值平方和开根与速度有效值之比<0.25；非超额峭度<3.5；'
    '速度有效值≥2.8且<4.5 mm/s。上述条件全部满足，才命中该演示规则。',
    '电机轴承润滑不足\n先检查润滑油脂的状态、油量和污染情况，必要时更换并检查轴承。',
]


class DemoEmbedding:
    def __init__(self, delay=0):
        self.delay = delay

    def profile(self):
        return {'mode': 'demo', 'model': 'fixed-presentation-vectors', 'fingerprint': 'ma-show-demo-v1'}

    def matches(self, profile):
        return profile == self.profile()

    def embed(self, texts):
        time.sleep(self.delay)
        return [[1., .5] for _ in texts]


class DemoModel:
    def __init__(self, delay=1.2):
        self.delay = delay
        self.config = SimpleNamespace(provider='demo', model='固定候选演示模型')

    def complete(self, messages):
        time.sleep(self.delay)
        payload = json.loads(messages[-1]['content'])
        question = payload.get('question', '')
        if 'history' in payload:
            result = {'query': question, 'clarification': None}
        elif 'candidates' in payload:
            conflict = '一倍频' in question
            result = {
                'ranking': [2, 3, 1] if conflict else [1, 2, 3],
                'rationale': ('候选1将常见特征扩大成充分条件。候选2保留联合判据约束，候选3保留排查范围。'
                              if conflict else '三个候选均引用润滑检查资料，结论一致。'),
                'conflicts': ['候选1认为一倍频升高足以判定不平衡，候选2和3认为仍需核对其他条件。'] if conflict else [],
                'gaps': ['需要完整联合判据来核对“是否足够”。'] if conflict else [],
                'queries': ['电机转子不平衡的完整判据', '电机转子不平衡的完整判据是否需要全部满足'] if conflict else [],
            }
        else:
            result = self.generate(payload, question)
        return {'content': json.dumps(result, ensure_ascii=False)}

    def generate(self, payload, question):
        evidence = payload['evidence']
        context = payload.get('working_context', {})
        if '一倍频' in question:
            if context.get('stage') == 'final':
                match = next((item for item in evidence if '上述条件全部满足' in item['text']), None)
                text = ('一倍频升高可作为排查线索。补查的演示资料要求同时核对一倍频比例、偶次频能量比例、峭度和速度有效值，'
                        '全部满足才命中该演示规则。具体条件以所引演示资料为准。')
            elif context.get('focus', '').startswith('完整回答'):
                match = next((item for item in evidence if '常见特征之一' in item['text']), None)
                text = '一倍频升高就足以直接判断为转子不平衡。'
            elif context.get('focus', '').startswith('核对'):
                match = next((item for item in evidence if '应核对其他频率' in item['text']), None)
                text = '一倍频升高还需要核对其他频率成分和适用条件，单项现象尚不足以完成判断。'
            else:
                match = next((item for item in evidence if '多种故障' in item['text']), None)
                text = '相似振动现象可能涉及多种故障，需要综合排查。'
        elif '润滑' in question:
            match = next((item for item in evidence if '先检查润滑油脂' in item['text']), None)
            text = '先检查润滑油脂的状态、油量和污染情况，必要时更换，并检查轴承。'
        else:
            match = None
            text = ''
        if match is None:
            return {'paragraphs': []}
        return {'paragraphs': [{'text': text, 'citations': [{
            'knowledge_id': match['knowledge_id'], 'quote': match['text'].split('\n', 1)[-1],
        }]}]}


def create_demo_app(root, delay=1.2):
    os.environ['MA_RAG_ENABLED'] = 'true'
    embedding = DemoEmbedding(delay=min(delay / 2, .6))
    model = DemoModel(delay)
    app = create_app(Settings(Path(root), 'demo-admin-token-123', 'demo-read-token-123'),
                     embedder=embedding, answer_model_factory=lambda: model, presentation_demo=True)
    store = app.state.store
    kb = store.create_kb('MA_show 两条展示路径（演示数据）')
    content = '\n\n'.join(DEMO_PASSAGES).encode('utf-8')
    upload = store.reserve_upload(kb['id'], 'MA_show演示参考资料.md', content, document_type='reference')
    payload = {'document_type': 'reference', 'warnings': ['固定候选展示样本'], 'rules': [],
               'blocks': [{'id': 'b'+str(index), 'kind': 'description', 'chunk_ready': True,
                           'text': text, 'locator': '演示资料片段 '+str(index)}
                          for index, text in enumerate(DEMO_PASSAGES, 1)]}
    doc = store.finish_upload(upload['document_id'], 1, payload)
    store.review(doc['id'], doc['edit_version'], 'description', 'approved', note='仅用于受控演示')
    store.publish(kb['id'], embedding)
    return app


def main():
    parser = argparse.ArgumentParser(description='启动MA_show固定候选演示服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8004)
    parser.add_argument('--delay', type=float, default=1.2, help='演示阶段等待秒数')
    args = parser.parse_args()
    if not 0 <= args.delay <= 10:
        parser.error('delay应在0至10秒之间')
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='ma-show-demo-') as root:
        app = create_demo_app(root, args.delay)
        print('固定候选演示，数据位于临时目录。停止服务后清理。', flush=True)
        print('管理令牌 demo-admin-token-123，只读令牌 demo-read-token-123', flush=True)
        print('普通案例 '+NORMAL_QUESTION, flush=True)
        print('冲突案例 '+CONFLICT_QUESTION, flush=True)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()

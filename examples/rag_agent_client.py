"""External Agent integration example; HTTP retrieval and caller-side diagnosis."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    p=argparse.ArgumentParser(description='外部Agent调用示例：固定发布版本，取知识和全部适用规则')
    p.add_argument('--base-url',default=os.environ.get('RAG_BASE_URL','http://127.0.0.1:8000'))
    p.add_argument('--kb-id',required=True)
    p.add_argument('--device-type',default='motor')
    p.add_argument('--query',default='轴承润滑不足的原因和措施')
    p.add_argument('--features',help='可选特征JSON；数值判断只在本调用方执行')
    args=p.parse_args()
    token=os.environ.get('RAG_READ_TOKEN','')
    if not token:
        raise SystemExit('请通过环境变量配置RAG_READ_TOKEN，不要把令牌写入代码')
    def request(path,body=None):
        req=Request(args.base_url.rstrip('/')+'/api/v1'+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with urlopen(req,timeout=120) as response:return json.load(response)
    kb=next((v for v in request('/knowledge-bases') if v['id']==args.kb_id),None)
    if not kb or not kb['current_release']:raise SystemExit('知识库不存在或尚未发布')
    release=kb['current_release']
    hits=request('/retrieve',{'kb_id':args.kb_id,'release_id':release,'query':args.query})
    rules=request('/rules?'+urlencode({'kb_id':args.kb_id,'release_id':release,'device_type':args.device_type}))
    result={'release_id':release,'knowledge':hits['items'],'applicable_rules':rules['rules']}
    if args.features:
        # This is the Agent side. The RAG service never imports this evaluator.
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from diagnostic_agent.rules import analyze
        features=json.loads(Path(args.features).read_text())
        snapshot={'version':release,'snapshot_id':release,'rules':rules['rules'],'conventions':['由调用方按已确认规则计算'],'source':{'title':'独立RAG已发布规则'}}
        result['agent_side_diagnosis']=analyze(features,args.device_type,rule_snapshot=snapshot)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

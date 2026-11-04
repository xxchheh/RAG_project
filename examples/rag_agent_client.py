"""External Agent integration example; HTTP retrieval and complete published rule retrieval."""
import argparse
import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    p=argparse.ArgumentParser(description='外部Agent调用示例：固定发布版本，取知识和全部适用规则')
    p.add_argument('--base-url',default=os.environ.get('RAG_BASE_URL','http://127.0.0.1:8000'))
    p.add_argument('--kb-id',required=True)
    p.add_argument('--device-type',default='motor')
    p.add_argument('--query',default='轴承润滑不足的原因和措施')
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
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

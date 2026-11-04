"""Real-service acceptance using only bundled simulated documents.

This test auto-approves its own demo drafts. Never point it at business uploads.
Both a real local embedder and the configured chat API must be enabled by server.
"""
import argparse
import json
import os
import time
from pathlib import Path
import httpx


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--base-url',default='http://127.0.0.1:8000')
    p.add_argument('--output',default='outputs/rag-service-acceptance.json')
    args=p.parse_args()
    admin=os.environ.get('RAG_ADMIN_TOKEN')
    read=os.environ.get('RAG_READ_TOKEN')
    if not admin or not read:raise SystemExit('配置RAG_ADMIN_TOKEN和RAG_READ_TOKEN以运行验收')
    c=httpx.Client(base_url=args.base_url+'/api/v1',headers={'Authorization':'Bearer '+admin},timeout=120)
    def get(path,**kwargs):
        r=c.get(path,**kwargs);r.raise_for_status();return r.json()
    def post(path,**kwargs):
        r=c.post(path,**kwargs);r.raise_for_status();return r.json()
    def wait(task):
        for _ in range(300):
            value=get('/admin/tasks/'+task)
            if value['status']=='succeeded':return value['result']
            if value['status']=='failed':raise RuntimeError(value['error'])
            time.sleep(1)
        raise TimeoutError('任务超时')
    checks={}
    record={'real_chat_api':True,'real_embeddings':True,'checks':checks}
    try:
        kb=post('/admin/knowledge-bases',json={'name':'真实API模拟验收-'+str(int(time.time()))})['id']
        record['kb_id']=kb
        docs=[]
        for name in ['standard.md','standard.docx','maintenance.pdf']:
            content=(Path(__file__).resolve().parents[1]/'examples/rag_documents'/name).read_bytes()
            result=post('/admin/documents',data={'kb_id':kb,'external_id':'acceptance-'+name},files={'file':(name,content)})
            wait(result['task_id'])
            doc=get('/admin/documents/'+result['document_id']);docs.append(doc)
            print('已解析',name,'规则',len(doc['payload']['rules']),flush=True)
        checks['three_document_formats']=all(d['status']=='ready' for d in docs)
        for i,doc in enumerate(docs):
            docs[i]=post('/admin/documents/'+doc['id']+'/review',json={'expected_version':doc['edit_version'],'target':'description','decision':'approved','note':'仅模拟样本文档自动验收'})
        first=wait(post('/admin/publish',json={'kb_id':kb})['task_id'])
        rule_before=get('/rules',params={'kb_id':kb})
        checks['prose_can_publish_before_rules']=rule_before['count']==0
        primary=docs[0]
        checks['real_model_extracted_rule']=bool(primary['payload']['rules']) and all(r['valid'] for r in primary['payload']['rules'])
        if not checks['real_model_extracted_rule']:
            record['rule_errors']=[r.get('validation_errors') for r in primary['payload']['rules']]
            raise RuntimeError('真实提取未得到有效规则')
        # Save an editable reviewed note, then explicitly review the exact draft.
        r=primary['payload']['rules'][0]
        r.setdefault('review_notes',[]).append('模拟验收已核对三个条件全部满足的解释。')
        response=c.patch('/admin/documents/'+primary['id'],json={'expected_version':primary['edit_version'],'rules':primary['payload']['rules']});response.raise_for_status();primary=response.json()
        for rule in primary['payload']['rules']:
            primary=post('/admin/documents/'+primary['id']+'/review',json={'expected_version':primary['edit_version'],'target':'rule','rule_id':rule['id'],'decision':'approved','note':'模拟样本逐条核对'})
        release=wait(post('/admin/publish',json={'kb_id':kb})['task_id'])
        found=get('/rules',params={'kb_id':kb,'release_id':release['id'],'device_type':'motor'})
        checks['complete_reviewed_rule_set']=found['count']==len(primary['payload']['rules'])
        c.headers['Authorization']='Bearer '+read
        hits=post('/retrieve',json={'kb_id':kb,'release_id':release['id'],'query':'轴承润滑不足有哪些原因和措施'})
        checks['real_local_semantic_retrieval']=bool(hits['items']) and hits['profile']['mode']=='local'
        answer=post('/answers',json={'kb_id':kb,'release_id':release['id'],'query':'润滑不足时应如何维护轴承？'})
        checks['real_chat_answer_with_citations']=answer['status']=='answered' and bool(answer['citations'])
        checks['readonly_cannot_publish']=c.post('/admin/publish',json={'kb_id':kb}).status_code==403
        c.headers['Authorization']='Bearer '+admin
        primary=get('/admin/documents/'+primary['id'])
        content=(Path(__file__).resolve().parents[1]/'examples/rag_documents/standard.md').read_text().replace('大于等于3.5','大于等于4.2').encode()
        updated=post('/admin/documents/'+primary['id']+'/revisions',files={'file':('standard.md',content)})
        wait(updated['task_id']);new=get('/admin/documents/'+primary['id'])
        checks['update_does_not_change_active_rules']=get('/rules',params={'kb_id':kb})['release_id']==release['id']
        new=post('/admin/documents/'+new['id']+'/review',json={'expected_version':new['edit_version'],'target':'description','decision':'approved'})
        for rule in new['payload']['rules']:
            new=post('/admin/documents/'+new['id']+'/review',json={'expected_version':new['edit_version'],'target':'rule','rule_id':rule['id'],'decision':'approved'})
        newer=wait(post('/admin/publish',json={'kb_id':kb})['task_id'])
        rules_new=get('/rules',params={'kb_id':kb,'release_id':newer['id'],'device_type':'motor'})
        checks['published_update_changes_rule_threshold']=rules_new['count']>0 and rules_new['rules'][0]['condition'] != found['rules'][0]['condition']
        post('/admin/documents/'+new['id']+'/disable',json={'disabled':True})
        deleted=wait(post('/admin/publish',json={'kb_id':kb})['task_id'])
        checks['disable_removes_rules']=get('/rules',params={'kb_id':kb})['count']==0
        checks['history_preserves_rules']=get('/rules',params={'kb_id':kb,'release_id':release['id']})['count']==found['count']
        restored=wait(post('/admin/restore',json={'kb_id':kb,'release_id':release['id']})['task_id'])
        republished=wait(post('/admin/publish',json={'kb_id':kb})['task_id'])
        checks['restore_survives_next_publish']=get('/admin/releases/'+restored['id'])['entries']==get('/admin/releases/'+republished['id'])['entries']
        record.update(releases=[first['id'],release['id'],newer['id'],deleted['id'],restored['id']],embedding_profile=hits['profile'],answer=answer['answer'],citations=answer['citations'])
    finally:
        record['all_passed']=bool(checks) and all(checks.values()) and len(checks)>=12
        out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(record,ensure_ascii=False,indent=2))
        print(json.dumps({'all_passed':record['all_passed'],'checks':checks,'record':str(out)},ensure_ascii=False,indent=2))
    if not record['all_passed']:raise SystemExit(1)


if __name__=='__main__':main()

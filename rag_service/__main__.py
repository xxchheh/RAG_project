"""Standalone service launcher and explicit read-only legacy import."""
import argparse
import json
import sqlite3
from pathlib import Path
from .config import Settings
from .store import Store


def import_legacy(store, kb_id, database=None):
    if database:
        path=Path(database).resolve()
        if path == store.path:
            raise ValueError('旧数据库不能是新服务正在使用的数据库')
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
            rows=c.execute('SELECT v.body,v.source FROM rules r JOIN rule_revisions v ON v.rule_id=r.id AND v.revision=r.published_revision WHERE r.enabled=1').fetchall()
        rules=[{**json.loads(body),'legacy_source':{k:v for k,v in json.loads(source).items() if k!='file'}} for body,source in rows]
    else:
        source=Path(__file__).resolve().parent.parent/'diagnostic_agent/config/rules.json'
        value=json.loads(source.read_text())
        rules=[{**r,'legacy_source':{k:v for k,v in value['source'].items() if k!='file'}} for r in value['rules']]
    blocks=[]
    for i,r in enumerate(rules,1):
        key=f'b{i}'
        blocks.append({'id':key,'text':'\n'.join([r['name'], *r.get('phenomena',[]), *r.get('causes',[]), *r.get('actions',[]), r['original_text']]),'locator':f'旧知识规则 {r["id"]}','kind':'rule','heading':True})
        r['block_ids']=[key]
    content='\n\n'.join(b['text'] for b in blocks).encode()
    reserved=store.reserve_upload(kb_id,'legacy-rules.md',content,external_id='legacy-rule-import')
    if not reserved['unchanged']:
        store.finish_upload(reserved['document_id'],reserved['revision'],{'blocks':blocks,'rules':rules,'warnings':['从旧知识复制为草稿，需逐条审核，旧库未修改。']})
    return reserved


def main(argv=None):
    parser=argparse.ArgumentParser(description='独立专业知识RAG服务')
    commands=parser.add_subparsers(dest='command',required=True)
    serve=commands.add_parser('serve',help='启动HTTP服务及中文管理页面')
    serve.add_argument('--host',default='127.0.0.1')
    serve.add_argument('--port',type=int,default=8000)
    commands.add_parser('tokens',help='在本机查看管理令牌和Agent只读令牌')
    migration=commands.add_parser('import-legacy',help='将内置或旧SQLite规则复制成待审核草稿')
    migration.add_argument('--kb-id')
    migration.add_argument('--name',default='设备机理知识库')
    migration.add_argument('--database')
    args=parser.parse_args(argv)
    try:
        settings=Settings.from_env()
        if args.command=='tokens':
            print(json.dumps({'admin':settings.admin_token,'read':settings.read_token},ensure_ascii=False,indent=2))
        elif args.command=='import-legacy':
            store=Store(settings.data_dir)
            kb_id=args.kb_id or store.create_kb(args.name)['id']
            print(json.dumps({'kb_id':kb_id,**import_legacy(store,kb_id,args.database)},ensure_ascii=False,indent=2))
        else:
            import uvicorn
            print(f'管理页面 http://{args.host}:{args.port}/；接口说明 /docs；本机查看令牌：python -m rag_service tokens',flush=True)
            uvicorn.run('rag_service.app:create_app',factory=True,host=args.host,port=args.port)
    except (ValueError,OSError,sqlite3.Error) as exc:
        print('未完成：'+str(exc))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())

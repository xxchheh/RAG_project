import io,tempfile,sys,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PIL import Image,ImageDraw,ImageFont
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
parser=argparse.ArgumentParser(description='真实本地OCR验收：生成两页中文扫描PDF，上传临时库审核发布并检索')
parser.add_argument('--font',type=Path,help='支持中文的字体文件路径')
args=parser.parse_args()
candidates=[args.font,Path('/System/Library/Fonts/STHeiti Medium.ttc'),Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
font_path=next((p for p in candidates if p and p.exists()),None)
if font_path is None:parser.error('请使用 --font 指定中文字体文件')
font=ImageFont.truetype(str(font_path),40)
pages=[]
for lines in [['电机维护培训','定期检查轴承润滑状态。','速度有效值 2.8 mm/s'],['水泵维护培训','检查水泵密封状态。','定期检查润滑油。']]:
 im=Image.new('RGB',(1200,650),'white');draw=ImageDraw.Draw(im)
 for i,line in enumerate(lines):draw.text((45,45+i*120),line,font=font,fill='black')
 pages.append(im)
content=io.BytesIO();pages[0].save(content,format='PDF',save_all=True,append_images=pages[1:],resolution=150)
class Embedding:
 def profile(self):return {'mode':'test','model':'fixture','fingerprint':'ocr'}
 def matches(self,p):return p==self.profile()
 def embed(self,texts):return [[1.,1.] for _ in texts]
with tempfile.TemporaryDirectory() as root:
 app=create_app(Settings(Path(root),'scan-admin-token-123','scan-read-token-123'),embedder=Embedding(),model_factory=lambda:None)
 with TestClient(app) as c:
  c.headers['Authorization']='Bearer scan-admin-token-123'
  kb=c.post('/api/v1/admin/knowledge-bases',json={'name':'扫描验收'}).json()['id']
  upload=c.post('/api/v1/admin/documents',data={'kb_id':kb,'document_type':'reference'},files={'file':('scan.pdf',content.getvalue())})
  assert upload.status_code==202,upload.text
  task=c.get('/api/v1/admin/tasks/'+upload.json()['task_id']).json();assert task['status']=='succeeded',task
  doc=c.get('/api/v1/admin/documents/'+upload.json()['document_id']).json()
  text='\n'.join(b['text'] for b in doc['payload']['blocks']);print(text)
  assert '润滑' in text and '2.8' in text and 'mm/s' in text
  assert {b['page'] for b in doc['payload']['blocks']}=={1,2}
  assert all(b.get('extraction')=='ocr' for b in doc['payload']['blocks'])
  r=c.post('/api/v1/admin/documents/'+doc['id']+'/review',json={'expected_version':doc['edit_version'],'target':'description','decision':'approved'});assert r.status_code==200,r.text
  app.state.store.publish(kb,app.state.embedder)
  c.headers['Authorization']='Bearer scan-read-token-123'
  result=c.post('/api/v1/retrieve',json={'kb_id':kb,'query':'润滑'});assert result.status_code==200,result.text
  assert result.json()['items']
  assert all('OCR' in item['source']['locator'] for item in result.json()['items'])
  print('PASS: real 2-page Chinese scanned PDF -> local OCR -> review -> publish -> HTTP retrieval; correct page/OCR references. Embeddings fixed for pipeline validation.')

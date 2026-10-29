import io
import zipfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from fastapi.testclient import TestClient
from rag_service.app import create_app
from rag_service.config import Settings
from rag_service.documents import parse_document
from rag_service.knowledge_units import prose_units
from rag_service.reference_readers import paragraphs


def pdf_pages(texts, landscape=False):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=800 if landscape else 600, height=450 if landscape else 800)
        font = DictionaryObject({NameObject('/Type'):NameObject('/Font'), NameObject('/Subtype'):NameObject('/Type1'), NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        lines = ['BT /F1 12 Tf 40 400 Td']
        for i, line in enumerate(text.split('\n')):
            if i: lines.append('0 -20 Td')
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            lines.append('('+escaped+') Tj')
        lines.append('ET')
        stream = DecodedStreamObject(); stream.set_data('\n'.join(lines).encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    buf=io.BytesIO();writer.write(buf);return buf.getvalue()


def pptx_bytes(image_only=False):
    # Filenames intentionally differ from presentation order.
    presentation='<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="a"/><p:sldId id="257" r:id="b"/></p:sldIdLst></p:presentation>'
    relationships='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="a" Target="slides/slide9.xml"/><Relationship Id="b" Target="slides/slide2.xml"/></Relationships>'
    def shape(text, title=False):
        return '<p:sp><p:nvSpPr><p:nvPr>'+('<p:ph type="title"/>' if title else '')+'</p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>'+text+'</a:t></a:r></a:p></p:txBody></p:sp>'
    def slide(text):
        return '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'+text+'</p:spTree></p:cSld></p:sld>'
    table='<p:graphicFrame><a:graphic><a:graphicData><a:tbl>'
    for values in [('项目','说明'),('轴承','定期检查润滑')]:
        table+='<a:tr>'+''.join('<a:tc><a:txBody><a:p><a:r><a:t>'+value+'</a:t></a:r></a:p></a:txBody></a:tc>' for value in values)+'</a:tr>'
    table+='</a:tbl></a:graphicData></a:graphic></p:graphicFrame>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('ppt/presentation.xml',presentation)
        z.writestr('ppt/_rels/presentation.xml.rels',relationships)
        z.writestr('ppt/slides/slide9.xml',slide(shape('电机维护',True)+shape('检查轴承温度。')+table))
        z.writestr('ppt/slides/slide2.xml',slide('<p:pic/>' if image_only else shape('水泵维护',True)+shape('检查密封与润滑。')))
    return out.getvalue()


class ReaderTests(unittest.TestCase):
    def test_soft_wraps_lists_and_field_boundaries(self):
        self.assertEqual(paragraphs('检查润滑\n状态。\n机理原因：润滑不足。\n1、检查轴承\n温度。\n2、检查密封。'), ['检查润滑状态。','机理原因：润滑不足。','1、检查轴承温度。','2、检查密封。'])
        self.assertEqual(paragraphs('Check bearing\nlubrication.'), ['Check bearing lubrication.'])

    def test_pdf_pages_are_separate_and_bookmark_context_survives(self):
        content=pdf_pages(['Book header\nCheck bearing\nlubrication.','Book header\nInspect pump seals.','Book header\nInspect fan blades.'])
        result=parse_document(content,'book.pdf',document_type='reference')
        self.assertEqual({b['page'] for b in result['blocks']},{1,2,3})
        self.assertNotIn('Book header','\n'.join(b['text'] for b in result['blocks']))
        units=prose_units(result['blocks'],set())
        self.assertEqual(len(units),3)
        self.assertIn('Check bearing lubrication.',units[0][1])
        self.assertNotIn('Inspect pump',units[0][1])
        self.assertIn('第 2 页',units[1][2])
        self.assertTrue(result['warnings'])

    def test_landscape_pdf_keeps_slide_title(self):
        result=parse_document(pdf_pages(['Bearing care\nCheck lubrication.','Pump care\nInspect seals.'],True),'slides.pdf',document_type='reference')
        units=prose_units(result['blocks'],set())
        self.assertEqual(len(units),2)
        self.assertTrue(units[0][1].startswith('Bearing care\n'))
        self.assertTrue(units[1][1].startswith('Pump care\n'))
        self.assertIn('第 1 页',units[0][2])

    def test_single_line_landscape_is_content_not_empty_title(self):
        result=parse_document(pdf_pages(['Inspect bearing lubrication.'],True),'one.pdf',document_type='reference')
        self.assertEqual(len(prose_units(result['blocks'],set())),1)

    def test_pdf_bookmark_sets_chapter(self):
        writer=PdfWriter();writer.append(io.BytesIO(pdf_pages(['Inspect bearing.','Inspect oil.'])))
        writer.add_outline_item('Lubrication',0)
        out=io.BytesIO();writer.write(out)
        result=parse_document(out.getvalue(),'book.pdf',document_type='reference')
        self.assertTrue(all(b['section']=='Lubrication' for b in result['blocks']))

    def test_pptx_slide_order_titles_and_table_headers(self):
        result=parse_document(pptx_bytes(),'training.pptx',document_type='reference')
        self.assertEqual(result['blocks'][0]['text'],'电机维护')
        self.assertEqual(result['blocks'][-1]['page'],2)
        self.assertIn('项目：轴承；说明：定期检查润滑','\n'.join(b['text'] for b in result['blocks']))
        units=prose_units(result['blocks'],set())
        self.assertEqual(len(units),2)
        self.assertIn('电机维护',units[0][1]);self.assertIn('幻灯片 1',units[0][2])
        self.assertNotIn('水泵',units[0][1])

    def test_missing_text_or_invalid_ppt_is_explicit(self):
        for content,name in [(pptx_bytes(True),'image.pptx'),(pdf_pages(['Text.','']),'scan.pdf'),(b'invalid','old.ppt'),(b'invalid','broken.pptx')]:
            with self.subTest(name=name),self.assertRaises(ValueError):parse_document(content,name)

    def test_scanned_pdf_ocr_mixed_pages_and_progress(self):
        progress=[]
        with patch('rag_service.ocr.recognize_pdf_page', return_value={'text':'电机维护。\n检查轴承润滑。','confidence':.75}) as ocr:
            result=parse_document(pdf_pages(['A text page with enough readable words for maintenance.','']), 'scan.pdf', document_type='reference', progress=progress.append)
        self.assertEqual(ocr.call_count,1)
        found=[b for b in result['blocks'] if b.get('extraction')=='ocr']
        self.assertTrue(found)
        self.assertTrue(all(b['page']==2 and '/OCR' in b['locator'] for b in found))
        self.assertTrue(any('偏低' in warning for warning in result['warnings']))
        self.assertTrue(any('第 2/2 页' in line for line in progress))

    def test_pptx_embedded_image_ocr(self):
        content=io.BytesIO(pptx_bytes())
        with zipfile.ZipFile(content) as source:
            entries={name:source.read(name) for name in source.namelist()}
        pic='<p:pic><p:blipFill><a:blip xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:embed="img"/></p:blipFill></p:pic>'
        entries['ppt/slides/slide9.xml']=entries['ppt/slides/slide9.xml'].replace(b'</p:spTree>',pic.encode()+b'</p:spTree>')
        entries['ppt/slides/_rels/slide9.xml.rels']=b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="img" Target="../media/image1.png"/></Relationships>'
        entries['ppt/media/image1.png']=b'test fixture only; OCR mocked'
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as target:
            for name,data in entries.items():target.writestr(name,data)
        with patch('rag_service.ocr.recognize_image',return_value={'text':'图片上的润滑说明','confidence':.95}):
            result=parse_document(output.getvalue(),'images.pptx',document_type='reference')
        blocks=[b for b in result['blocks'] if b.get('extraction')=='ocr']
        self.assertEqual(len(blocks),1)
        self.assertIn('幻灯片 1/图片OCR',blocks[0]['locator'])

    def test_http_upload_review_publish_retrieve_pptx(self):
        class Embedding:
            def profile(self):return {'mode':'test','model':'constant','fingerprint':'test'}
            def matches(self,p):return p==self.profile()
            def embed(self,texts):return [[1.,1.] for text in texts]
        with tempfile.TemporaryDirectory() as root:
            app=create_app(Settings(Path(root),'admin-test-token-123','read-test-token-123'),embedder=Embedding(),model_factory=lambda:None)
            with TestClient(app) as client:
                client.headers['Authorization']='Bearer admin-test-token-123'
                kb=client.post('/api/v1/admin/knowledge-bases',json={'name':'培训资料'}).json()['id']
                uploaded=client.post('/api/v1/admin/documents',data={'kb_id':kb,'document_type':'reference'},files={'file':('training.pptx',pptx_bytes())})
                self.assertEqual(uploaded.status_code,202,uploaded.text)
                task=client.get('/api/v1/admin/tasks/'+uploaded.json()['task_id']).json();self.assertEqual(task['status'],'succeeded',task)
                doc=client.get('/api/v1/admin/documents/'+uploaded.json()['document_id']).json()
                review=client.post('/api/v1/admin/documents/'+doc['id']+'/review',json={'expected_version':doc['edit_version'],'target':'description','decision':'approved'})
                self.assertEqual(review.status_code,200,review.text)
                app.state.store.publish(kb,app.state.embedder)
                client.headers['Authorization']='Bearer read-test-token-123'
                hits=client.post('/api/v1/retrieve',json={'kb_id':kb,'query':'润滑'}).json()['items']
                self.assertTrue(hits)
                self.assertIn('幻灯片',hits[0]['source']['locator'])
                self.assertEqual(client.get('/api/v1/capabilities').json()['formats'],['md','docx','pdf','pptx'])


if __name__=='__main__': unittest.main()

"""Pinned parser worker. JSON/PNG checkpoints are reused only for identical input/config."""
import json
import os
import re
import signal
from pathlib import Path
import sys


def main():
    from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling_core.transforms.chunker.hierarchical_chunker import HierarchicalChunker
    source, folder = Path(sys.argv[1]), Path(sys.argv[2])
    options = PdfPipelineOptions()
    options.do_ocr = True
    options.ocr_options = RapidOcrOptions(force_full_page_ocr=True, lang=['ch'])
    options.do_table_structure = True
    options.do_formula_enrichment = os.environ.get('DOCLING_FORMULA', 'false').lower() == 'true'
    options.generate_page_images = True
    options.generate_picture_images = True
    options.images_scale = 1.5
    options.document_timeout = 240
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options), InputFormat.IMAGE: ImageFormatOption(pipeline_options=options)})
    if source.suffix == '.pdf':
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(source)
        count = len(pdf); pdf.close()
    else:
        # Docling's native PPTX backend retains slide provenance without OCR/PDF round trip.
        count = 1
    manifest = {'model_revisions': {'docling-models':'fc0f2d45e2218ea24bce5045f58a389aed16dc23', 'docling-layout-heron':'8f39ad3c0b4c58e9c2d2c84a38465abf757272d8', 'CodeFormulaV2':'ecedbe111d15c2dc60bfd4a823cbe80127b58af4'}, 'parser': 'docling', 'version': '2.145.0', 'total': count, 'pages': [], 'failed_pages': {}, 'unresolved_pages': {}, 'review_required': True}
    selected = [int(n) for n in sys.argv[3].split(',')] if len(sys.argv) > 3 else range(1, count+1)
    def deadline(signum, frame):
        raise TimeoutError('该处理单元超时，保留其他成功页，需重试或调整解析方案')
    if hasattr(signal, 'SIGALRM'): signal.signal(signal.SIGALRM, deadline)
    for page in selected:
        checkpoint = folder/f'page-{page}.json'
        print(f'Docling 识别 {page}/{count}'+('（复用检查点）' if checkpoint.exists() else ''), flush=True)
        try:
            if not checkpoint.exists():
                if hasattr(signal, 'SIGALRM'): signal.alarm(int(os.environ.get('DOCLING_PAGE_TIMEOUT', '300')))
                result = converter.convert(source, page_range=(page,page)) if source.suffix == '.pdf' else converter.convert(source)
                # A changed upstream model must not silently reuse this parser version.
                hub = Path(os.environ.get('HF_HOME', Path.home()/'.cache/huggingface'))/'hub'
                for model, revision in manifest['model_revisions'].items():
                    if model == 'CodeFormulaV2' and not options.do_formula_enrichment: continue
                    ref = hub/('models--docling-project--'+model)/'refs'/('v2.3.0' if model == 'docling-models' else 'main')
                    if ref.exists() and ref.read_text().strip() != revision:
                        raise ValueError(f'模型{model}版本变化，须验证后更新解析版本，不能复用旧检查点')
                if str(result.status.value) != 'success': raise ValueError(str(result.errors))
                doc = result.document
                blocks = []
                for i, chunk in enumerate(HierarchicalChunker().chunk(doc)):
                    items = chunk.meta.doc_items
                    labels = [item.label.value for item in items]
                    if labels and all(label in ('page_header','page_footer','section_header','title') for label in labels): continue
                    provenance = [prov for item in items for prov in item.prov]
                    pages = sorted({prov.page_no for prov in provenance}) or [page]
                    headings = list(chunk.meta.headings or [])
                    text = chunk.text.strip()
                    if not text: continue
                    blocks.append({'text': '\n'.join([*headings, text]), 'locator': ('幻灯片 ' if source.suffix == '.pptx' else 'PDF物理页 ')+','.join(map(str,pages))+f' / 内容 {i+1} / '+','.join(labels),
                        'kind': 'uncertain' if 'formula' in labels else 'description', 'review_required': True, 'heading': False, 'section': ' / '.join(headings), 'page': pages[0], 'page_title': ' / '.join(headings),
                        'extraction': 'docling', 'content_types': labels, 'bbox': [p.bbox.model_dump(mode='json') for p in provenance]})
                if source.suffix == '.pptx':
                    for image_number, picture in enumerate(doc.pictures, 1):
                        image = picture.get_image(doc)
                        if image is None: continue
                        asset = folder/f'picture-{image_number}.png'; image.save(asset)
                        slide = picture.prov[0].page_no if picture.prov else 1
                        try:
                            image_doc = converter.convert(asset).document
                            text = image_doc.export_to_markdown().strip()
                            if text:
                                blocks.append({'text': '幻灯片图片文字（OCR，待人工核对）：\n'+text, 'locator': f'幻灯片 {slide} / 图片 {image_number} OCR',
                                    'page': slide, 'page_title': f'幻灯片 {slide}', 'section': f'幻灯片 {slide}', 'heading': False,
                                    'kind': 'uncertain' if any(item.label.value == 'formula' for item, _ in image_doc.iterate_items()) else 'description', 'extraction': 'docling_image_ocr', 'content_types': ['picture_ocr'], 'asset': asset.name, 'review_required': True})
                        except Exception as error:
                            raise ValueError(f'幻灯片 {slide} 图片 {image_number} 未解析：{error}') from error
                # TOC can be detected as a table by the layout model; retain it
                # for review but never use a list of page references as evidence.
                for block in blocks:
                    if len(re.findall(r'[章节目]', block['text'])) >= 3 and len(re.findall(r'…|\.{3}', block['text'])) >= 3:
                        block.update(excluded=True, content_types=['table_of_contents'])
                # Keep the complete structured document (tables, formula text, pictures and provenance).
                (folder/f'page-{page}.docling.json').write_text(json.dumps(doc.export_to_dict(), ensure_ascii=False))
                (folder/f'page-{page}.md').write_text(doc.export_to_markdown())
                for number, rendered in doc.pages.items():
                    if rendered.image: rendered.image.pil_image.save(folder/f'preview-{number}.png')
                if not blocks:
                    blocks = [{'text': '【图像或空白页：未识别到可检索文字，必须核对原页】', 'kind':'uncertain', 'heading':False, 'page':page, 'section':'', 'page_title':'', 'locator':f'PDF物理页 {page} / 待复核图像', 'content_types':['unresolved_picture'], 'review_required':True}]
                tmp = checkpoint.with_suffix('.tmp')
                tmp.write_text(json.dumps({'blocks': blocks}, ensure_ascii=False)); tmp.replace(checkpoint)
            saved = json.loads(checkpoint.read_text())
            if any('unresolved_picture' in b.get('content_types', []) for b in saved['blocks']):
                manifest['unresolved_pages'][str(page)] = '已保留原页，缺少可检索文字；必须人工核对，未加入公共检索'
            manifest['pages'].append(page)
        except Exception as error:
            manifest['failed_pages'][str(page)] = str(error)
        if hasattr(signal, 'SIGALRM'): signal.alarm(0)
        (folder/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 1 if manifest['failed_pages'] else 0


if __name__ == '__main__': sys.exit(main())

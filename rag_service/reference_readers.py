"""Page-aware text readers. Images, charts and scanned pages require OCR."""
import io
import math
import posixpath
import re
import zipfile
from collections import Counter
from xml.etree import ElementTree as ET

NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
REL = '{http://schemas.openxmlformats.org/package/2006/relationships}Relationship'
FIELD = re.compile(r'^(?:机理现象|机理原因|解决措施|处理措施|机理规则|诊断规则|判断条件|故障名称)\s*[:：]')
BULLET = re.compile(r'^\s*(?:[•●▪◆\-]|\d+[.、)）]|[（(]\d+[)）])\s*')


def paragraphs(text):
    """Join soft line wraps, retaining list and labelled-field boundaries."""
    groups, current = [], []
    def flush():
        if current:
            joined = current[0]
            for line in current[1:]:
                # Chinese wraps need no invented spaces; English words do.
                separator = ' ' if re.search(r'[A-Za-z0-9]$', joined) and re.match(r'[A-Za-z0-9]', line) else ''
                joined += separator+line
            groups.append(joined)
            current.clear()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush(); continue
        if FIELD.match(line) or BULLET.match(line):
            flush()
        current.append(line)
        if re.search(r'[。！？；.!?;：:]$', line):
            flush()
    flush()
    return groups


def pdf_blocks(content, max_pages=100, progress=None):
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError('不支持加密 PDF，请先提供可读取的文字型 PDF')
        if len(reader.pages) > max_pages:
            raise ValueError(f'PDF 超过 {max_pages} 页，请拆分文件')
        pages, ocr_pages = [], {}
        from .ocr import recognize_pdf_page
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ''
            # An image page with just a page number also needs OCR.
            if not text.strip() or (len(text.strip()) < 40 and len(page.images)):
                if progress:
                    progress(f'OCR识别 PDF 第 {index+1}/{len(reader.pages)} 页')
                try:
                    result = recognize_pdf_page(content, index)
                except ValueError as error:
                    raise ValueError(f'PDF 第 {index+1} 页：{error}') from None
                if result['text'].strip():
                    text = result['text']
                    ocr_pages[index+1] = result
                elif not text.strip():
                    text = ''
                else:
                    raise ValueError(f'PDF 第 {index+1} 页图片未识别出正文，请人工复核或重新扫描')
            pages.append(text)
        missing = [str(i+1) for i, text in enumerate(pages) if not text.strip()]
        if missing:
            raise ValueError('PDF 以下页面没有可提取文字：'+', '.join(missing)+'；可能是扫描、图片或空白页，需要 OCR 或人工移除空白页后上传；OCR未能识别有效正文，不会跳过这些页面发布不完整知识。')
        outlines = {}
        def walk(items):
            for item in items:
                if isinstance(item, list):
                    walk(item)
                else:
                    try:
                        page = reader.get_destination_page_number(item)
                        if page is not None and page >= 0:
                            outlines[page+1] = str(item.title)
                    except (AttributeError, ValueError, KeyError):
                        continue
        try:
            walk(reader.outline)
        except (ValueError, KeyError):
            pass
        # Only recurring margin lines on portrait pages are treated as headers.
        portrait = [float(page.mediabox.height) >= float(page.mediabox.width) for page in reader.pages]
        margins = Counter()
        for text, book in zip(pages, portrait):
            if book:
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                margins.update(set(lines[:1]+lines[-1:]))
        repeated = {line for line, count in margins.items() if count >= max(3, math.ceil(len(pages)*.6)) and len(line) < 100 and not re.search(r'\d|[≥≤<>]|规则|条件|阈值', line)}
        blocks, chapter = [], ''
        for number, (text, book) in enumerate(zip(pages, portrait), 1):
            chapter = outlines.get(number, chapter)
            lines = text.splitlines()
            nonempty = [i for i, line in enumerate(lines) if line.strip()]
            if book and nonempty:
                for index in {nonempty[0], nonempty[-1]}:
                    if lines[index].strip() in repeated:
                        lines[index] = ''
            # Remove only a trailing explicit page marker matching the actual page.
            if nonempty and re.fullmatch(rf'(?:第\s*)?{number}\s*页', lines[nonempty[-1]].strip()):
                lines[nonempty[-1]] = ''
            title = chapter
            first = next((line.strip() for line in lines if line.strip()), '')
            slide_title = not book and len(nonempty) >= 2 and len(first) <= 60 and not FIELD.match(first) and not BULLET.match(first) and not re.search(r'[≥≤<>]|大于|小于|峭度|冲击|有效值|阈值', first)
            if slide_title:
                title = first
                first_index = next(i for i, line in enumerate(lines) if line.strip())
                lines[first_index] = ''
            section = title or f'第 {number} 页'
            if slide_title:
                blocks.append({'text': title, 'locator': f'第 {number} 页/标题', 'heading': True, 'section': section, 'page': number, 'page_title': section})
            for index, paragraph in enumerate(paragraphs('\n'.join(lines)), 1):
                blocks.append({'text': paragraph, 'locator': f'第 {number} 页/段落 {index}', 'heading': False, 'section': section, 'page': number, 'page_title': section})
        for block in blocks:
            if block['page'] in ocr_pages:
                block.update(extraction='ocr', ocr_confidence=ocr_pages[block['page']]['confidence'])
                block['locator'] += '/OCR'
        return blocks
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法解析 PDF，请上传可提取文字的有效 PDF') from None


def pptx_blocks(content, max_pages=100, max_unpacked=40*1024*1024, progress=None):
    """Read OOXML in presentation order; never follow external relationships."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > max_unpacked:
                raise ValueError('PPTX 解压内容超过40 MiB，请拆分文件')
            def read(name):
                data = archive.read(name)
                if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
                    raise ValueError('PPTX 含不支持的 XML 声明')
                return ET.fromstring(data)
            presentation = read('ppt/presentation.xml')
            relationships = {node.attrib['Id']: node.attrib for node in read('ppt/_rels/presentation.xml.rels') if node.tag == REL}
            slides = presentation.findall('p:sldIdLst/p:sldId', NS)
            if len(slides) > max_pages:
                raise ValueError(f'PPTX 超过 {max_pages} 页，请拆分文件')
            blocks, missing = [], []
            for number, slide in enumerate(slides, 1):
                if progress:
                    progress(f'解析幻灯片 {number}/{len(slides)}（含图片OCR）')
                rel = relationships.get(slide.get('{'+NS['r']+'}id'), {})
                target = rel.get('Target', '')
                if rel.get('TargetMode') == 'External' or ':' in target:
                    raise ValueError('不支持外部幻灯片引用')
                name = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('ppt', target))
                if not name.startswith('ppt/slides/') or not name.endswith('.xml'):
                    raise ValueError('PPTX 幻灯片路径无效')
                root = read(name)
                rel_path = posixpath.join(posixpath.dirname(name), '_rels', posixpath.basename(name)+'.rels')
                image_rels = {node.attrib['Id']:node.attrib for node in read(rel_path) if node.tag == REL} if rel_path in archive.namelist() else {}
                tree = root.find('p:cSld/p:spTree', NS)
                if tree is None:
                    missing.append(str(number)); continue
                def text_lines(node):
                    return [] if node is None else [''.join(t.text or '' for t in p.findall('.//a:t', NS)).strip() for p in node.findall('.//a:p', NS)]
                title = ''
                for shape in tree.findall('.//p:sp', NS):
                    placeholder = shape.find('p:nvSpPr/p:nvPr/p:ph', NS)
                    if placeholder is not None and placeholder.get('type') in ('title', 'ctrTitle'):
                        title = '\n'.join(line for line in text_lines(shape) if line)
                        break
                section = title or f'幻灯片 {number}'
                rows = []
                # XML order preserves the authoring order, including grouped shapes.
                for shape in tree.iter():
                    if shape.tag == '{'+NS['p']+'}sp':
                        placeholder = shape.find('p:nvSpPr/p:nvPr/p:ph', NS)
                        if placeholder is not None and placeholder.get('type') in ('dt', 'ftr', 'sldNum'):
                            continue
                        is_title = placeholder is not None and placeholder.get('type') in ('title', 'ctrTitle')
                        for text in filter(None, text_lines(shape)):
                            rows.append({'text': text, 'heading': is_title, 'locator': f'幻灯片 {number}/文本 {len(rows)+1}'})
                    elif shape.tag == '{'+NS['p']+'}pic':
                        blip = shape.find('.//a:blip', NS)
                        image_rel = image_rels.get(blip.get('{'+NS['r']+'}embed') if blip is not None else '', {})
                        if image_rel and image_rel.get('TargetMode') != 'External':
                            image_name = posixpath.normpath(posixpath.join(posixpath.dirname(name), image_rel.get('Target', '')))
                            if not image_name.startswith('ppt/media/'):
                                raise ValueError('PPTX图片路径无效')
                            from .ocr import recognize_image
                            try:
                                result = recognize_image(archive.read(image_name))
                            except ValueError as error:
                                raise ValueError(f'幻灯片 {number} 图片：{error}') from None
                            if result['text'].strip():
                                rows.append({'text':result['text'], 'heading':False, 'extraction':'ocr', 'ocr_confidence':result['confidence'], 'locator':f'幻灯片 {number}/图片OCR {len(rows)+1}'})
                    elif shape.tag == '{'+NS['a']+'}tbl':
                        table_rows = [[' / '.join(filter(None, text_lines(cell))) for cell in row.findall('a:tc', NS)] for row in shape.findall('a:tr', NS)]
                        for i, values in enumerate(table_rows):
                            headers = table_rows[0]
                            text = '；'.join((headers[j]+'：' if i and j < len(headers) and headers[j] else '')+value for j, value in enumerate(values))
                            if text.strip('；'):
                                rows.append({'text': text, 'heading': False, 'locator': f'幻灯片 {number}/表格行 {i+1}'})
                if not rows or (root.findall('.//p:pic', NS) and all(row['heading'] for row in rows)):
                    missing.append(str(number))
                for row in rows:
                    blocks.append({**row, 'section': section, 'page': number, 'page_title': section})
            if missing:
                raise ValueError('PPTX 以下幻灯片没有可提取文字：'+', '.join(missing)+'；可能为图片页，需 OCR 或人工转换文字，不能跳过后直接发布。')
            return blocks
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法解析 PPTX，请提供有效的 .pptx；旧版 .ppt 请先另存为 .pptx') from None

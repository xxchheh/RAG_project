"""Local Chinese/English OCR. No source documents are uploaded to a service."""
import io
import os
import threading
from functools import lru_cache

_LOCK = threading.Lock()
_PDF_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _engine():
    try:
        from rapidocr import RapidOCR
    except ImportError:
        raise ValueError('扫描资料需要本地OCR依赖，请安装 pip install ".[rag,ocr]"') from None
    try:
        return RapidOCR()
    except Exception:
        raise ValueError('OCR模型初始化失败，请检查首次模型下载网络或预先准备本地模型缓存') from None


def recognize_image(image):
    from PIL import Image, ImageStat
    if os.environ.get('RAG_OCR_ENABLED', 'true').lower() in ('0', 'false', 'no'):
        raise ValueError('当前关闭OCR，扫描页需启用 RAG_OCR_ENABLED=true 后重新解析')
    try:
        loaded = Image.open(io.BytesIO(image)) if isinstance(image, bytes) else image
        if loaded.width*loaded.height > 20_000_000:
            raise ValueError('OCR图片超过2000万像素，请降低分辨率')
        rgb = loaded.convert('RGB')
        # Blank pages need no model download or inference.
        if max(ImageStat.Stat(rgb.convert('L')).stddev) < 1:
            return {'text': '', 'confidence': None}
        with _LOCK:
            output = _engine()(rgb)
        texts = list(output.txts or [])
        scores = list(output.scores or [])
        return {'text': '\n'.join(text for text in texts if text.strip()),
                'confidence': min(float(score) for score in scores) if scores else None}
    except ValueError:
        raise
    except Exception:
        raise ValueError('本地OCR识别失败，请检查图片和运行环境') from None


def _recognize_pdf_page(content, page_index):
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ValueError('扫描PDF需要页面渲染依赖，请安装 pip install ".[rag,ocr]"') from None
    try:
        with pdfium.PdfDocument(content) as document:
            page = document[page_index]
            try:
                width, height = page.get_size()
                scale = min(2.5, (16_000_000/max(1, width*height))**.5)
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil().copy()
                    try:
                        return recognize_image(image)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
    except ValueError:
        raise
    except Exception:
        raise ValueError('扫描PDF页面渲染失败，无法进行OCR') from None


def recognize_pdf_page(content, page_index):
    # PDFium rendering is serialized across background task threads.
    with _PDF_LOCK:
        return _recognize_pdf_page(content, page_index)

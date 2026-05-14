import os
import re
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

BOOKS_DIR = Path("storage/books")
BOOKS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Text extractors ─────────────────────────────────────────────────────────

def extract_text_from_txt(path: str) -> list[tuple[int, str]]:
    """Returns list of (page_number, text)."""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    # Split into ~2000-char chunks as "pages"
    chunks = []
    size = 2000
    for i, start in enumerate(range(0, len(content), size), 1):
        chunks.append((i, content[start:start + size].strip()))
    return [(p, t) for p, t in chunks if t]


def extract_text_from_docx(path: str) -> list[tuple[int, str]]:
    try:
        from docx import Document
        doc = Document(path)
        full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        size = 2000
        chunks = []
        for i, start in enumerate(range(0, len(full_text), size), 1):
            chunk = full_text[start:start + size].strip()
            if chunk:
                chunks.append((i, chunk))
        return chunks
    except Exception as e:
        logger.error(f"DOCX extract error: {e}")
        return []


def extract_text_from_pdf(path: str) -> tuple[list[tuple[int, str]], bool]:
    """
    Returns (pages, is_scanned).
    pages = list of (page_number, text)
    is_scanned = True if very little text found (likely scanned)
    """
    pages = []
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = page.extract_text() or ""
                text = text.strip()
                if text:
                    pages.append((i, text))
    except Exception as e:
        logger.error(f"pdfplumber error: {e}")

    # Determine if scanned: if less than 20% of pages have text
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
    except Exception:
        total = max(len(pages), 1)

    is_scanned = len(pages) < max(1, total * 0.2)
    return pages, is_scanned


def extract_text_with_ocr(path: str) -> list[tuple[int, str]]:
    """
    OCR for scanned PDFs using pytesseract.
    Returns list of (page_number, text).
    """
    pages = []
    try:
        import pytesseract
        from pdf2image import convert_from_path
        images = convert_from_path(path, dpi=200)
        for i, img in enumerate(images, 1):
            text = pytesseract.image_to_string(img, lang="rus+kaz+eng")
            text = text.strip()
            if text:
                pages.append((i, text))
        logger.info(f"OCR extracted {len(pages)} pages")
    except ImportError:
        logger.warning("pytesseract/pdf2image not installed — OCR unavailable")
    except Exception as e:
        logger.error(f"OCR error: {e}")
    return pages


# ─── Chunk splitter ───────────────────────────────────────────────────────────

def split_into_chunks(page_number: int, text: str,
                       chunk_size: int = 500, overlap: int = 100) -> list[tuple[int, str]]:
    """
    Split page text into overlapping chunks.
    Returns list of (page_number, chunk_text).
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= chunk_size:
        return [(page_number, text)]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append((page_number, chunk))
        start += chunk_size - overlap
    return chunks


# ─── Main loader ──────────────────────────────────────────────────────────────

async def load_book(file_path: str, original_name: str, book_id: int) -> dict:
    """
    Process a book file: extract text, split into chunks, save to DB.
    Returns status dict.
    """
    import database as db

    ext = Path(original_name).suffix.lower()
    result = {
        "book_id": book_id,
        "title": Path(original_name).stem,
        "page_count": 0,
        "chunk_count": 0,
        "status": "ok",
        "message": "",
        "is_scanned": False,
        "ocr_used": False,
    }

    # ── Extract pages ────────────────────────────────────────────
    pages: list[tuple[int, str]] = []

    if ext == ".txt":
        pages = extract_text_from_txt(file_path)

    elif ext == ".docx":
        pages = extract_text_from_docx(file_path)
        if not pages:
            result["status"] = "error"
            result["message"] = "Не удалось извлечь текст из DOCX."
            await db.update_book_status(book_id, "error")
            return result

    elif ext == ".pdf":
        pages, is_scanned = extract_text_from_pdf(file_path)
        result["is_scanned"] = is_scanned
        if is_scanned:
            logger.info(f"PDF is scanned — trying OCR for book_id={book_id}")
            ocr_pages = extract_text_with_ocr(file_path)
            if ocr_pages:
                pages = ocr_pages
                result["ocr_used"] = True
            else:
                result["status"] = "scanned_no_ocr"
                result["message"] = (
                    "Книга загружена, но текст почти не найден. "
                    "Похоже, это скан. OCR не смог распознать страницы — "
                    "попробуйте загрузить текстовый PDF или DOCX."
                )
                await db.update_book_status(book_id, "scanned")
                return result
    else:
        result["status"] = "error"
        result["message"] = f"Неподдерживаемый формат: {ext}"
        await db.update_book_status(book_id, "error")
        return result

    if not pages:
        result["status"] = "error"
        result["message"] = "Текст не найден в файле."
        await db.update_book_status(book_id, "error")
        return result

    # ── Save chunks to DB ────────────────────────────────────────
    book_title = result["title"]
    chunk_count = 0
    for page_num, page_text in pages:
        chunks = split_into_chunks(page_num, page_text)
        for _, chunk_text in chunks:
            await db.save_book_chunk(book_id, book_title, page_num, chunk_text)
            chunk_count += 1

    result["page_count"] = len(pages)
    result["chunk_count"] = chunk_count

    await db.update_book_status(book_id, "ready", len(pages))

    if result["ocr_used"]:
        result["message"] = (
            f"✅ Книга загружена через OCR. "
            f"Распознано страниц: {len(pages)}, фрагментов: {chunk_count}."
        )
    else:
        result["message"] = (
            f"✅ Книга загружена и прочитана. "
            f"Найдено страниц: {len(pages)}, фрагментов: {chunk_count}."
        )

    return result


async def download_and_save_book(bot, file_id: str, original_name: str) -> str:
    """Download file from Telegram and save to books folder."""
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\.\-]", "_", original_name)
    dest = BOOKS_DIR / safe_name

    tg_file = await bot.get_file(file_id)
    await tg_file.download_to_drive(str(dest))
    logger.info(f"Book saved to {dest}")
    return str(dest)

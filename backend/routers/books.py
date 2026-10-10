import os
import io
import uuid
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, DATA_DIR
from models import Book, BookFile
from services.pdf_service import extract_pages, detect_book_level, get_page_text
from services.ai_service import ocr_page, analyze_page_image, read_page_context

router = APIRouter(prefix="/books", tags=["books"])

# Vercel Functions reject request bodies over 4.5 MB (hard platform limit), and
# storing multi-MB rows in the database gets costly, so cap uploads below that
# limit. Override with MAX_UPLOAD_BYTES when self-hosting with a higher ceiling.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(4 * 1024 * 1024)))

# Legacy on-disk location (pre-DB storage). Retained so existing local installs
# can still read books that were saved before bytes moved into the database.
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads", "books")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MIMES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _book_ext(book: Book) -> str:
    """Best-effort file extension from the book's logical filename/path."""
    for path in (book.filename, book.storage_path):
        if path:
            ext = os.path.splitext(path)[1].lower()
            if ext:
                return ext
    return ""


def _store_file(db: Session, book_id: str, data: bytes) -> None:
    """Insert or replace the stored bytes for a book."""
    existing = db.query(BookFile).filter(BookFile.book_id == book_id).first()
    if existing:
        existing.data = data
    else:
        db.add(BookFile(book_id=book_id, data=data))


def _load_file_bytes(db: Session, book: Book) -> bytes:
    """Return the book bytes from the database, falling back to legacy disk."""
    row = db.query(BookFile).filter(BookFile.book_id == book.id).first()
    if row is not None and row.data is not None:
        return bytes(row.data)
    legacy = book.storage_path
    if legacy and os.path.isfile(legacy):
        with open(legacy, "rb") as f:
            return f.read()
    raise HTTPException(404, "Book file not found")


@router.post("/upload")
async def upload_book(user_id: str, title: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    ext = os.path.splitext((file.filename or "").lower())[1]
    is_image = ext in IMAGE_EXTS
    is_pdf = ext == ".pdf"

    if not is_image and not is_pdf:
        raise HTTPException(400, "Only PDF and image files (JPG, PNG, WEBP) are supported")

    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File too large ({len(file_bytes) // (1024 * 1024)} MB). "
            f"The maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    if is_image:
        total_pages = 1
        dominant_level = "A1"
        file_type = "image"
        pages = []
    else:
        pages = extract_pages(file_bytes)
        dominant_level = detect_book_level(pages)
        total_pages = len(pages)
        file_type = "pdf"

    book_id = str(uuid.uuid4())

    book = Book(
        id=book_id,
        user_id=user_id,
        title=title,
        filename=file.filename,
        storage_path=f"{book_id}{ext}",
        total_pages=total_pages,
        dominant_level=dominant_level,
        file_type=file_type,
    )
    db.add(book)
    _store_file(db, book_id, file_bytes)
    db.commit()
    db.refresh(book)

    return {"book": _book_dict(book), "pages": pages}


@router.get("/")
def list_books(user_id: str, db: Session = Depends(get_db)):
    books = db.query(Book).filter(Book.user_id == user_id).order_by(Book.created_at.desc()).all()
    return [_book_dict(b) for b in books]


@router.get("/{book_id}/pages")
def get_book_pages(book_id: str, user_id: str, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    if (book.file_type or "pdf") == "image":
        return {"book": _book_dict(book), "pages": []}
    file_bytes = _load_file_bytes(db, book)
    pages = extract_pages(file_bytes)
    return {"book": _book_dict(book), "pages": pages}


@router.get("/{book_id}/pages/{page_num}/text")
async def get_page_text_endpoint(book_id: str, page_num: int, user_id: str, db: Session = Depends(get_db)):
    """Extract text from a single page. Falls back to Gemini Vision OCR for scanned PDFs and images."""
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")

    file_bytes = _load_file_bytes(db, book)

    file_type = book.file_type or "pdf"

    if file_type == "image":
        ext = _book_ext(book)
        mime = IMAGE_MIMES.get(ext, "image/jpeg")
        try:
            text = await ocr_page(file_bytes, mime_type=mime)
            method = "ocr"
        except Exception:
            text = "(Could not extract text from this image)"
            method = "failed"
    else:
        if page_num < 1 or page_num > book.total_pages:
            raise HTTPException(400, f"Page must be between 1 and {book.total_pages}")
        text, image_bytes = get_page_text(file_bytes, page_num)
        if len(text.strip()) < 80:
            try:
                text = await ocr_page(image_bytes)
                method = "ocr"
            except Exception:
                text = text or "(Could not extract text from this page)"
                method = "failed"
        else:
            method = "native"

    return {"text": text, "page": page_num, "method": method}


@router.get("/{book_id}/file")
def serve_book_file(book_id: str, user_id: str, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book file not found")
    ext = _book_ext(book)
    media_type = IMAGE_MIMES.get(ext, "application/pdf")
    data = _load_file_bytes(db, book)
    filename = book.filename or f"{book.id}{ext}"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.delete("/{book_id}")
def delete_book(book_id: str, user_id: str, db: Session = Depends(get_db)):
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    db.query(BookFile).filter(BookFile.book_id == book_id).delete()
    if book.storage_path and os.path.isfile(book.storage_path):
        os.remove(book.storage_path)
    from models import PageContext
    db.query(PageContext).filter(PageContext.book_id == book_id).delete()
    db.delete(book)
    db.commit()
    return {"ok": True}


@router.post("/{book_id}/overwrite")
async def overwrite_book_file(
    book_id: str,
    user_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Replace the stored PDF bytes with a newly annotated version from the client."""
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    if (book.file_type or "pdf") != "pdf":
        raise HTTPException(400, "Only PDF books can be overwritten")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"File too large. The maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    _store_file(db, book.id, content)
    db.commit()
    return {"ok": True}


@router.get("/{book_id}/ocr-region")
async def ocr_region(
    book_id: str,
    user_id: str,
    x: float,
    y: float,
    w: float,
    h: float,
    db: Session = Depends(get_db),
):
    """Crop a region of an image book server-side and OCR it."""
    from PIL import Image as PILImage, ImageOps
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    if (book.file_type or "pdf") != "image":
        raise HTTPException(400, "Only image books support region OCR")
    file_bytes = _load_file_bytes(db, book)
    with PILImage.open(io.BytesIO(file_bytes)) as _raw:
        img = ImageOps.exif_transpose(_raw)
        iw, ih = img.size
        left = max(0, int(x * iw))
        top = max(0, int(y * ih))
        right = min(iw, int((x + w) * iw))
        bottom = min(ih, int((y + h) * ih))
        if right <= left or bottom <= top:
            raise HTTPException(400, "Invalid region")
        cropped = img.crop((left, top, right, bottom))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        image_bytes = buf.getvalue()
    try:
        text = await ocr_page(image_bytes, mime_type="image/png")
        return {"text": text.strip()}
    except Exception as e:
        raise HTTPException(500, str(e))


class RegionItem(BaseModel):
    x: float
    y: float
    w: float
    h: float

class OCRRegionsRequest(BaseModel):
    regions: list[RegionItem]


@router.post("/{book_id}/pages/{page_num}/ocr-regions")
async def ocr_regions_batch(
    book_id: str,
    page_num: int,
    user_id: str,
    req: OCRRegionsRequest,
    db: Session = Depends(get_db),
):
    """OCR multiple rectangular regions from a PDF page or image book using Gemini Vision."""
    from PIL import Image as PILImage, ImageOps
    from services.ai_service import ocr_region_corrected

    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    if not req.regions:
        return {"texts": []}

    file_type = book.file_type or "pdf"

    # Render the full page once at high resolution so Gemini gets surrounding
    # context for each region (no crop-edge ambiguity, no hallucination).
    if file_type == "image":
        file_bytes = _load_file_bytes(db, book)
        with PILImage.open(io.BytesIO(file_bytes)) as raw:
            oriented = ImageOps.exif_transpose(raw).convert("RGB")
        buf = io.BytesIO()
        oriented.save(buf, format="PNG")
        full_page_bytes = buf.getvalue()
    else:
        file_bytes = _load_file_bytes(db, book)
        if page_num < 1 or page_num > book.total_pages:
            raise HTTPException(400, "Invalid page number")
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            page = pdf.pages[page_num - 1]
            pil_page = page.to_image(resolution=300)
            buf = io.BytesIO()
            pil_page.save(buf, format="PNG")
            full_page_bytes = buf.getvalue()

    texts: list[str] = []
    for region in req.regions:
        try:
            text = await ocr_region_corrected(
                full_page_bytes,
                {"x": region.x, "y": region.y, "w": region.w, "h": region.h},
            )
        except Exception:
            text = ""
        texts.append(text)

    return {"texts": texts}


@router.get("/{book_id}/pages/{page_num}/analyze")
async def analyze_page(
    book_id: str,
    page_num: int,
    user_id: str,
    lang_name: str = "English",
    db: Session = Depends(get_db),
):
    """Analyze a full page with Gemini and return a study explanation."""
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    file_bytes = _load_file_bytes(db, book)
    file_type = book.file_type or "pdf"
    if file_type == "image":
        from PIL import Image as PILImage, ImageOps
        import io as _io
        with PILImage.open(_io.BytesIO(file_bytes)) as _raw:
            oriented = ImageOps.exif_transpose(_raw)
            buf = _io.BytesIO()
            oriented.save(buf, format="PNG")
            image_bytes = buf.getvalue()
        mime = "image/png"
    else:
        if page_num < 1 or page_num > book.total_pages:
            raise HTTPException(400, "Invalid page number")
        _, image_bytes = get_page_text(file_bytes, page_num)
        mime = "image/png"
    try:
        analysis = await analyze_page_image(image_bytes, mime_type=mime, lang_name=lang_name)
        return {"analysis": analysis}
    except Exception as e:
        raise HTTPException(500, str(e))


class ReaderChatMsg(BaseModel):
    role: str
    content: str

class ReaderChatRequest(BaseModel):
    messages: list[ReaderChatMsg]
    user_level: str = "B1"
    lang_name: str = "English"


@router.post("/{book_id}/pages/{page_num}/read-context")
async def read_context(
    book_id: str,
    page_num: int,
    user_id: str,
    db: Session = Depends(get_db),
):
    """Extract and cache a deep vision-based content summary for a page."""
    from models import PageContext
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")

    file_type = book.file_type or "pdf"
    file_bytes = _load_file_bytes(db, book)

    if file_type == "image":
        ext = _book_ext(book)
        mime = IMAGE_MIMES.get(ext, "image/jpeg")
        image_bytes = file_bytes
    else:
        if page_num < 1 or page_num > book.total_pages:
            raise HTTPException(400, "Invalid page number")
        _, image_bytes = get_page_text(file_bytes, page_num)
        mime = "image/png"

    try:
        content = await read_page_context(image_bytes, mime_type=mime)
    except Exception as e:
        raise HTTPException(500, f"Page read failed: {e}")

    existing = db.query(PageContext).filter(
        PageContext.book_id == book_id, PageContext.page_num == page_num
    ).first()
    if existing:
        existing.content = content
        existing.created_at = __import__("datetime").datetime.utcnow()
    else:
        db.add(PageContext(book_id=book_id, page_num=page_num, content=content))
    db.commit()
    return {"ok": True, "length": len(content)}


@router.get("/{book_id}/pages/{page_num}/context")
def get_context(
    book_id: str,
    page_num: int,
    user_id: str,
    db: Session = Depends(get_db),
):
    """Return the cached page context if it exists."""
    from models import PageContext
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    ctx = db.query(PageContext).filter(
        PageContext.book_id == book_id, PageContext.page_num == page_num
    ).first()
    if not ctx:
        return {"ready": False}
    return {"ready": True, "content": ctx.content}


@router.delete("/{book_id}/pages/{page_num}/context")
def delete_context(
    book_id: str,
    page_num: int,
    user_id: str,
    db: Session = Depends(get_db),
):
    """Delete the cached page context (called when user starts a new page chat)."""
    from models import PageContext
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")
    db.query(PageContext).filter(
        PageContext.book_id == book_id, PageContext.page_num == page_num
    ).delete()
    db.commit()
    return {"ok": True}


@router.post("/{book_id}/pages/{page_num}/chat")
async def reader_chat(
    book_id: str,
    page_num: int,
    user_id: str,
    req: ReaderChatRequest,
    db: Session = Depends(get_db),
):
    from services.ai_service import chat_reader
    from models import PageContext
    book = db.query(Book).filter(Book.id == book_id, Book.user_id == user_id).first()
    if not book:
        raise HTTPException(404, "Book not found")

    # Prefer cached deep context; fall back to raw text extraction
    ctx = db.query(PageContext).filter(
        PageContext.book_id == book_id, PageContext.page_num == page_num
    ).first()

    if ctx:
        page_context = ctx.content
        page_text = None
    else:
        page_context = None
        file_type = book.file_type or "pdf"
        file_bytes = _load_file_bytes(db, book)
        if file_type == "image":
            ext = _book_ext(book)
            mime = IMAGE_MIMES.get(ext, "image/jpeg")
            try:
                page_text = await ocr_page(file_bytes, mime_type=mime)
            except Exception:
                page_text = ""
        else:
            if page_num < 1 or page_num > book.total_pages:
                raise HTTPException(400, "Invalid page number")
            page_text, _ = get_page_text(file_bytes, page_num)

    try:
        reply = await chat_reader(
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            user_level=req.user_level,
            lang_name=req.lang_name,
            page_context=page_context,
            page_text=page_text,
        )
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/transcribe-audio")
async def transcribe_audio_endpoint(file: UploadFile = File(...)):
    """Transcribe spoken audio via Gemini (used by voice input in reader chat)."""
    from services.ai_service import transcribe_audio
    audio_bytes = await file.read()
    try:
        text = await transcribe_audio(audio_bytes, file.content_type or "audio/webm")
        return {"text": text}
    except Exception as e:
        raise HTTPException(500, str(e))


def _book_dict(book: Book) -> dict:
    return {
        "id": book.id,
        "user_id": book.user_id,
        "title": book.title,
        "filename": book.filename,
        "storage_path": book.storage_path,
        "total_pages": book.total_pages,
        "dominant_level": book.dominant_level,
        "file_type": book.file_type or "pdf",
        "created_at": book.created_at.isoformat() if book.created_at else None,
    }

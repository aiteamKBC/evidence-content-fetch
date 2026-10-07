import re
from pathlib import Path

from django.core.files.storage import default_storage
from docx import Document
from pypdf import PdfReader


WORD_RE = re.compile(r"[^\W_]+(?:[''][^\W_]+)?", re.UNICODE)


def count_words(text):
    if not text:
        return 0
    return len(WORD_RE.findall(text))


def extract_text(storage_name):
    suffix = Path(storage_name).suffix.lower()
    try:
        if suffix == ".pdf":
            text = _extract_pdf(storage_name)
            if not text.strip():
                return _result(
                    "",
                    False,
                    "The PDF may be scanned or image based.",
                )
            return _result(text, True)
        if suffix == ".docx":
            return _result(_extract_docx(storage_name), True)
        if suffix == ".txt":
            return _result(_extract_txt(storage_name), True)
        return _result("", False, "Unsupported file type for text extraction.")
    except Exception as exc:
        return _result("", False, f"Text extraction failed: {exc}")


def _result(text, text_extracted, processing_note=None):
    text = text or ""
    return {
        "text": text,
        "word_count": count_words(text),
        "character_count": len(text),
        "text_extracted": text_extracted,
        "processing_note": processing_note,
    }


def _extract_pdf(storage_name):
    with default_storage.open(storage_name, "rb") as handle:
        reader = PdfReader(handle)
        return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_docx(storage_name):
    with default_storage.open(storage_name, "rb") as handle:
        document = Document(handle)
    return "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text)


def _extract_txt(storage_name):
    with default_storage.open(storage_name, "rb") as handle:
        data = handle.read()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

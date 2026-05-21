"""Text extraction for non-plain-text documents — PDF and Word (.docx).

`read_file` routes `.pdf` and `.docx` paths through here so the assistant
can read résumés, contracts, reports, letters, etc. without the user
converting them to plain text first.

- DOCX — an Office Open XML file is a ZIP of XML parts. We pull the text
  out of `word/document.xml` with the standard library; no dependency.
- PDF  — needs the `pypdf` package. If it isn't installed we raise a
  clear, actionable DocumentError instead of a stack trace.

The old binary `.doc` format (pre-2007 Word) is not supported — it's an
OLE compound file with no stdlib reader. Ask the user to "Save As .docx".
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


# Extensions this module knows how to turn into text.
SUPPORTED = {".pdf", ".docx"}

# Stop pulling text out of a huge document once we have comfortably more
# than read_file will ever surface — keeps a 2000-page PDF from hanging.
_EXTRACT_CHAR_CAP = 120_000


class DocumentError(RuntimeError):
    """Raised when a document can't be read (bad file, missing dep, locked)."""


def is_document(path: Path) -> bool:
    """True if `path` is a format extract_text() can handle."""
    return path.suffix.lower() in SUPPORTED


def extract_text(path: Path) -> str:
    """Extract plain text from a supported document.

    Raises DocumentError with a human-readable reason on any failure.
    """
    ext = path.suffix.lower()
    if ext == ".docx":
        return _extract_docx(path)
    if ext == ".pdf":
        return _extract_pdf(path)
    if ext == ".doc":
        raise DocumentError(
            "the old binary .doc format isn't supported — open it in Word "
            "and 'Save As' .docx, then try again"
        )
    raise DocumentError(f"unsupported document type: {ext or '(no extension)'}")


def read_text_or_document(path: Path) -> str:
    """Read any file as text — transparently extracting PDF/DOCX.

    Plain files are read with errors='replace'. Raises DocumentError on a
    document failure, OSError on a plain-file IO error.
    """
    if is_document(path):
        return extract_text(path)
    return path.read_text(errors="replace")


# --------------------------------------------------------------- DOCX -------

# WordprocessingML main namespace — stable across every .docx since 2007.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx(path: Path) -> str:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DocumentError(
            f"'{path.name}' is not a valid .docx (not a ZIP archive — "
            f"maybe it's an old .doc?): {e}"
        ) from e
    except OSError as e:
        raise DocumentError(f"could not open '{path.name}': {e}") from e

    with zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError as e:
            raise DocumentError(
                f"'{path.name}' is missing word/document.xml — corrupt or "
                f"not a Word document"
            ) from e

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise DocumentError(f"could not parse '{path.name}' XML: {e}") from e

    body = root.find(f"{_W}body")
    if body is None:
        body = root

    paragraphs: list[str] = []
    total = 0
    # Each <w:p> is a paragraph; walking its subtree in document order keeps
    # text runs, tabs and line breaks in the sequence they appear. Paragraphs
    # inside tables are picked up too (iter() recurses), just flattened.
    for para in body.iter(f"{_W}p"):
        parts: list[str] = []
        for node in para.iter():
            tag = node.tag
            if tag == f"{_W}t":                       # a run of text
                parts.append(node.text or "")
            elif tag == f"{_W}tab":                   # tab stop
                parts.append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):       # line / carriage break
                parts.append("\n")
        paragraphs.append("".join(parts))
        total += len(paragraphs[-1])
        if total > _EXTRACT_CHAR_CAP:
            paragraphs.append("… [document truncated — it's large; ask for a "
                              "specific section]")
            break

    text = "\n".join(paragraphs).strip()
    return text or "(the document has no readable text)"


# --------------------------------------------------------------- PDF --------

def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DocumentError(
            "reading PDF files needs the 'pypdf' package. Install it with:\n"
            "  pip install pypdf"
        ) from None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        # pypdf eagerly imports the optional native 'cryptography' package.
        # A broken build of it can fail with a Rust-level panic, which is a
        # BaseException (not Exception) — catch broadly so a bad optional
        # dependency degrades into a clear message instead of crashing.
        raise DocumentError(
            f"the 'pypdf' package failed to load ({type(e).__name__}: {e}). "
            f"Try reinstalling it:  pip install --force-reinstall pypdf"
        ) from None

    try:
        reader = PdfReader(str(path))
    except FileNotFoundError as e:
        raise DocumentError(f"PDF not found: {e}") from e
    except Exception as e:
        raise DocumentError(
            f"could not open '{path.name}': {type(e).__name__}: {e}"
        ) from e

    # Many PDFs are 'encrypted' only with an empty user password — try that
    # before giving up so ordinary protected-but-readable files still work.
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            pass

    try:
        pages = list(reader.pages)
    except Exception as e:
        raise DocumentError(
            f"'{path.name}' looks password-protected or damaged "
            f"({type(e).__name__}: {e})"
        ) from e

    if not pages:
        return "(the PDF has no pages)"

    out: list[str] = []
    total = 0
    extracted_any = False
    for i, page in enumerate(pages, 1):
        try:
            txt = (page.extract_text() or "").strip()
        except Exception as e:
            txt = f"[page {i}: text extraction failed — {type(e).__name__}: {e}]"
        if txt and not txt.startswith("[page "):
            extracted_any = True
        header = f"[page {i} of {len(pages)}]"
        out.append(f"{header}\n{txt}" if txt else f"{header}  (no extractable text)")
        total += len(txt)
        if total > _EXTRACT_CHAR_CAP:
            out.append(f"… [stopped after {i} pages — the PDF is large; ask "
                       f"for a specific page or section]")
            break

    if not extracted_any:
        # A PDF that's all scanned images yields no text layer.
        return ("(no extractable text — this PDF is likely scanned images. "
                "It would need OCR, which Cagentic doesn't do.)")

    return "\n\n".join(out)

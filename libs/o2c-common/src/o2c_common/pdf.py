from io import BytesIO

from pypdf import PdfReader


def pdf_text(data: bytes, mode: str = "layout") -> str:
    """Layout mode keeps table columns aligned (finding from the PoC)."""
    return "\n".join(
        page.extract_text(extraction_mode=mode) or ""
        for page in PdfReader(BytesIO(data)).pages
    )

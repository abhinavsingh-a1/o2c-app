import logging
import math
from io import BytesIO

from pypdf import PdfReader
from pypdf.generic import ContentStream

# pypdf logs recoverable problems of the file structure (e.g. "incorrect startxref
# pointer") as warnings; they say nothing about the text and flood the service logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Text drawn smaller than this (in points, after the text and transformation matrices)
# is not readable on paper: drop_invisible removes it (hidden prompt injection, ADR-010).
INVISIBLE_FONT_SIZE = 4.0
SHOW_TEXT = {b"Tj", b"TJ", b"'", b'"'}
IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]

Matrix = list[float]  # [a, b, c, d, e, f]


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    """m x n for PDF matrices [a b c d e f]."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return [
        a * A + b * C,
        a * B + b * D,
        c * A + d * C,
        c * B + d * D,
        e * A + f * C + E,
        e * B + f * D + F,
    ]


def effective_font_size(font_size: float, tm: Matrix, cm: Matrix) -> float:
    """Height of the text on the page: font size scaled by the text matrix and the CTM."""
    m = _multiply([float(x) for x in tm], [float(x) for x in cm])
    return abs(float(font_size)) * math.hypot(m[2], m[3])


def _has_invisible_text(page) -> bool:
    """Whether the page draws any text below INVISIBLE_FONT_SIZE (pypdf's visitor_text;
    it reports the text matrix and font size, but only in plain extraction mode)."""
    found = []

    def visit(text, cm, tm, font_dict, font_size):
        if (
            text.strip()
            and effective_font_size(font_size, tm, cm) < INVISIBLE_FONT_SIZE
        ):
            found.append(text)

    page.extract_text(visitor_text=visit)
    return bool(found)


def _drop_invisible_text(page, reader) -> None:
    """Remove text-showing operators below INVISIBLE_FONT_SIZE from the page content.
    Tracks Tf, Tm/BT, cm and q/Q; Td/TD/T* only move the text, they do not scale it."""
    content = ContentStream(page.get_contents(), reader)
    kept = []
    cm, font_size, tm = list(IDENTITY), 0.0, list(IDENTITY)
    stack = []
    for operands, operator in content.operations:
        if operator == b"q":
            stack.append((cm, font_size))
        elif operator == b"Q" and stack:
            cm, font_size = stack.pop()
        elif operator == b"cm":
            cm = _multiply([float(x) for x in operands], cm)
        elif operator == b"BT":
            tm = list(IDENTITY)
        elif operator == b"Tm":
            tm = [float(x) for x in operands]
        elif operator == b"Tf":
            font_size = float(operands[1])
        elif (
            operator in SHOW_TEXT
            and effective_font_size(font_size, tm, cm) < INVISIBLE_FONT_SIZE
        ):
            continue
        kept.append((operands, operator))
    content.operations = kept
    page.replace_contents(content)


def pdf_text(data: bytes, mode: str = "layout", drop_invisible: bool = False) -> str:
    """Layout mode keeps table columns aligned (finding from the PoC).
    drop_invisible: leave out text drawn below INVISIBLE_FONT_SIZE points. Pages without
    such text are extracted exactly as without the option."""
    reader = PdfReader(BytesIO(data))
    texts = []
    for page in reader.pages:
        if drop_invisible and _has_invisible_text(page):
            _drop_invisible_text(page, reader)
        texts.append(page.extract_text(extraction_mode=mode) or "")
    return "\n".join(texts)

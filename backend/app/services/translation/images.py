"""Картинки для docx: проверка, приведение к PNG / JPEG, растеризация SVG."""

import pymupdf

from app.core.logging import get_logger
from app.services.translation.model import Image

log = get_logger(__name__)

# больше этого по длинной стороне картинка уменьшается: иначе docx раздувается на десятки мегабайт
MAX_IMAGE_PX = 2400
MIN_IMAGE_PX = 4
SVG_ZOOM = 2.0
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _is_svg(data: bytes) -> bool:
    head = data[:1024].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _fit(pixmap: pymupdf.Pixmap) -> pymupdf.Pixmap:
    """RGB без CMYK и не больше MAX_IMAGE_PX по длинной стороне."""
    if pixmap.colorspace is not None and pixmap.colorspace.n > 3:
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
    longest = max(pixmap.width, pixmap.height)
    if longest > MAX_IMAGE_PX:
        scale = MAX_IMAGE_PX / longest
        pixmap = pymupdf.Pixmap(
            pixmap, max(1, round(pixmap.width * scale)), max(1, round(pixmap.height * scale))
        )
    return pixmap


def from_pixmap(pixmap: pymupdf.Pixmap, width_cm: float | None = None) -> Image:
    pixmap = _fit(pixmap)
    return Image(pixmap.tobytes("png"), "png", pixmap.width, pixmap.height, width_cm)


def render_svg(data: bytes, width_cm: float | None = None) -> Image | None:
    try:
        with pymupdf.open(stream=data, filetype="svg") as document:
            pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(SVG_ZOOM, SVG_ZOOM), alpha=False)
    except Exception as exc:
        log.warning("image_svg_failed", error=f"{type(exc).__name__}: {exc}")
        return None
    if min(pixmap.width, pixmap.height) < MIN_IMAGE_PX:
        return None
    return from_pixmap(pixmap, width_cm)


def make_image(data: bytes, width_cm: float | None = None) -> Image | None:
    """Картинка из байтов файла; None — формат не распознан или картинка пустая."""
    if _is_svg(data):
        return render_svg(data, width_cm)
    try:
        pixmap = pymupdf.Pixmap(data)
    except Exception as exc:
        log.warning("image_decode_failed", error=f"{type(exc).__name__}: {exc}", size=len(data))
        return None
    if min(pixmap.width, pixmap.height) < MIN_IMAGE_PX:
        return None
    # PNG и JPEG python-docx вставляет как есть: без перекодирования и потери качества
    keep = data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)
    if keep and max(pixmap.width, pixmap.height) <= MAX_IMAGE_PX:
        ext = "png" if data.startswith(_PNG_MAGIC) else "jpeg"
        return Image(data, ext, pixmap.width, pixmap.height, width_cm)
    return from_pixmap(pixmap, width_cm)

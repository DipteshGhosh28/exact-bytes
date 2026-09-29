"""Exact Bytes - a small, privacy-friendly PDF compressor."""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path

import fitz  # PyMuPDF
from flask import Flask, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename


MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_UNITS = {"KB": 1024, "MB": 1024 * 1024}
MAX_RASTER_DPI = 180
MAX_RASTER_PAGES = 100
MAX_PIXELS_PER_PAGE = 20_000_000
MAX_TOTAL_RASTER_PIXELS = 200_000_000
MAX_RASTER_WORK_PIXELS = 600_000_000
MAX_RASTER_ATTEMPTS = 10

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


class PdfInputError(ValueError):
    """Raised when the uploaded file is not a usable PDF."""


class TargetRangeError(ValueError):
    """Raised when a requested size range cannot be reached safely."""


@dataclass(frozen=True)
class CompressionResult:
    data: bytes
    original_size: int
    method: str

    @property
    def compressed_size(self) -> int:
        return len(self.data)


def human_size(size: int) -> str:
    """Return a compact binary file-size label."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{size / 1024:.1f} KB"


def parse_target_range(form: dict[str, str]) -> tuple[int, int]:
    """Validate the submitted range and convert it to bytes."""
    unit = form.get("unit", "").upper()
    if unit not in ALLOWED_UNITS:
        raise ValueError("Choose either KB or MB as the size unit.")

    try:
        minimum = Decimal(form.get("min_size", ""))
        maximum = Decimal(form.get("max_size", ""))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter valid numbers for the minimum and maximum size.") from None

    if not minimum.is_finite() or not maximum.is_finite():
        raise ValueError("Enter finite numbers for the target sizes.")
    if minimum <= 0 or maximum <= 0:
        raise ValueError("Target sizes must be greater than zero.")
    if minimum > maximum:
        raise ValueError("Minimum size cannot be greater than maximum size.")

    multiplier = ALLOWED_UNITS[unit]
    if maximum > Decimal(MAX_UPLOAD_BYTES) / multiplier:
        raise ValueError("The maximum target size cannot exceed the 50 MB upload limit.")
    try:
        minimum_bytes = math.ceil(minimum * multiplier)
        maximum_bytes = math.floor(maximum * multiplier)
    except (DecimalException, OverflowError):
        raise ValueError("Enter target sizes no larger than 50 MB.") from None
    if minimum_bytes > maximum_bytes:
        raise ValueError("The selected range is too narrow. Use larger values.")
    return minimum_bytes, maximum_bytes


def _lossless_copy(document: fitz.Document) -> bytes:
    """Remove unused data and compress streams without changing page appearance."""
    return document.tobytes(
        garbage=4,
        clean=True,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
        use_objstms=1,
        compression_effort=100,
    )


def _rasterized_copy(
    document: fitz.Document, *, dpi: int, jpeg_quality: int
) -> bytes:
    """Create an image-based copy for PDFs that resist lossless compression."""
    output = fitz.open()
    try:
        metadata = {
            key: value
            for key, value in document.metadata.items()
            if value
            and key
            in {
                "author",
                "creationDate",
                "creator",
                "keywords",
                "modDate",
                "producer",
                "subject",
                "title",
                "trapped",
            }
        }
        if metadata:
            output.set_metadata(metadata)

        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)
        for page in document:
            pixmap = page.get_pixmap(
                matrix=matrix,
                colorspace=fitz.csRGB,
                alpha=False,
                annots=True,
            )
            image = pixmap.tobytes("jpeg", jpg_quality=jpeg_quality)
            target_page = output.new_page(width=page.rect.width, height=page.rect.height)
            target_page.insert_image(target_page.rect, stream=image)

        return output.tobytes(
            garbage=4,
            deflate=True,
            use_objstms=1,
            compression_effort=100,
        )
    finally:
        output.close()


def _raster_attempt_budget(document: fitz.Document) -> int:
    """Reject raster jobs that could exhaust memory and bound total pixel work."""
    if document.page_count > MAX_RASTER_PAGES:
        raise TargetRangeError(
            f"This PDF has more than {MAX_RASTER_PAGES} pages. Choose a larger "
            "maximum size so it can stay lossless."
        )

    total_pixels = 0
    for page in document:
        width = page.rect.width
        height = page.rect.height
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0
            or height <= 0
        ):
            raise PdfInputError("The PDF contains an invalid page size.")
        pixel_width = math.ceil(width * MAX_RASTER_DPI / 72)
        pixel_height = math.ceil(height * MAX_RASTER_DPI / 72)
        page_pixels = pixel_width * pixel_height
        if page_pixels > MAX_PIXELS_PER_PAGE:
            raise TargetRangeError(
                "A page is too large to image-compress safely. Choose a larger "
                "maximum size."
            )
        total_pixels += page_pixels
        if total_pixels > MAX_TOTAL_RASTER_PIXELS:
            raise TargetRangeError(
                "This PDF is too large to image-compress safely. Choose a larger "
                "maximum size."
            )

    work_limited_attempts = MAX_RASTER_WORK_PIXELS // max(total_pixels, 1)
    return max(3, min(MAX_RASTER_ATTEMPTS, work_limited_attempts))


class _RasterSearchBudgetReached(Exception):
    """Internal signal used to end the bounded raster search."""


def _find_rasterized_candidate(
    document: fitz.Document,
    minimum_bytes: int,
    maximum_bytes: int,
    max_attempts: int,
) -> bytes | None:
    """Search bounded DPI/quality values for the largest file below the maximum."""
    best_under: bytes | None = None
    tried: dict[tuple[int, int], int] = {}

    def render(dpi: int, quality: int) -> bytes:
        key = (dpi, quality)
        if len(tried) >= max_attempts:
            raise _RasterSearchBudgetReached
        candidate = _rasterized_copy(document, dpi=dpi, jpeg_quality=quality)
        tried[key] = len(candidate)
        return candidate

    def remember(candidate: bytes) -> None:
        nonlocal best_under
        if len(candidate) <= maximum_bytes and (
            best_under is None or len(candidate) > len(best_under)
        ):
            best_under = candidate

    # At a stable JPEG quality, PDF size is largely driven by render resolution.
    # Binary search keeps the request bounded to at most eight full renders.
    try:
        low_dpi, high_dpi = 36, MAX_RASTER_DPI
        for _ in range(8):
            if low_dpi > high_dpi:
                break
            dpi = (low_dpi + high_dpi) // 2
            candidate = render(dpi, 75)
            size = len(candidate)
            remember(candidate)
            if minimum_bytes <= size <= maximum_bytes:
                return candidate
            if size > maximum_bytes:
                high_dpi = dpi - 1
            else:
                low_dpi = dpi + 1

        # JPEG quality gives finer control when adjacent DPI values straddle a
        # narrow target range. Try the closest known resolutions on either side.
        closest_dpis = sorted(
            {dpi for dpi, _ in tried},
            key=lambda dpi: min(
                abs(size - maximum_bytes)
                for (seen_dpi, _), size in tried.items()
                if seen_dpi == dpi
            ),
        )[:2]
        for dpi in closest_dpis:
            low_quality, high_quality = 20, 92
            for _ in range(7):
                if low_quality > high_quality:
                    break
                quality = (low_quality + high_quality) // 2
                if (dpi, quality) in tried:
                    size = tried[(dpi, quality)]
                    candidate = None
                else:
                    candidate = render(dpi, quality)
                    size = len(candidate)
                    remember(candidate)
                if minimum_bytes <= size <= maximum_bytes:
                    if candidate is None:
                        candidate = _rasterized_copy(
                            document, dpi=dpi, jpeg_quality=quality
                        )
                    return candidate
                if size > maximum_bytes:
                    high_quality = quality - 1
                else:
                    low_quality = quality + 1

        # One final low-resolution attempt lets us give an honest error for very
        # small targets instead of looping indefinitely.
        if best_under is None:
            candidate = render(30, 20)
            remember(candidate)
            if minimum_bytes <= len(candidate) <= maximum_bytes:
                return candidate
    except _RasterSearchBudgetReached:
        pass

    return best_under


def compress_pdf(
    source: bytes,
    minimum_bytes: int,
    maximum_bytes: int,
    *,
    allow_rasterization: bool = False,
) -> CompressionResult:
    """Compress a PDF into the requested range when it is safely achievable."""
    if not source:
        raise PdfInputError("The selected file is empty.")
    # The PDF header must appear within the first 1,024 bytes. Limit the slice
    # so whitespace stripping never duplicates an entire large upload.
    if not source[:1024].lstrip().startswith(b"%PDF-"):
        raise PdfInputError("The selected file is not a valid PDF.")

    try:
        document = fitz.open(stream=source, filetype="pdf")
    except (fitz.FileDataError, RuntimeError, ValueError):
        raise PdfInputError("The PDF is damaged or cannot be read.") from None

    try:
        if (
            document.needs_pass
            or document.is_encrypted
            or bool(document.metadata.get("encryption"))
        ):
            raise PdfInputError("Encrypted or password-protected PDFs are not supported.")
        if document.page_count == 0:
            raise PdfInputError("The PDF has no pages.")

        original_size = len(source)
        if original_size < minimum_bytes:
            raise TargetRangeError(
                f"This PDF is already only {human_size(original_size)}, below your "
                f"minimum of {human_size(minimum_bytes)}. Choose a lower minimum."
            )
        if original_size <= maximum_bytes:
            return CompressionResult(source, original_size, "already-in-range")

        lossless = _lossless_copy(document)
        if minimum_bytes <= len(lossless) <= maximum_bytes:
            return CompressionResult(lossless, original_size, "lossless")

        if not allow_rasterization:
            raise TargetRangeError(
                "Lossless compression cannot reach this range. To try stronger "
                "compression, enable the image-based fallback and acknowledge "
                "that it creates a flattened PDF."
            )

        best_under = lossless if len(lossless) <= maximum_bytes else None
        attempt_budget = _raster_attempt_budget(document)
        rasterized = _find_rasterized_candidate(
            document, minimum_bytes, maximum_bytes, attempt_budget
        )
        if rasterized is not None and (
            best_under is None or len(rasterized) > len(best_under)
        ):
            best_under = rasterized

        if best_under is not None and len(best_under) >= minimum_bytes:
            return CompressionResult(best_under, original_size, "flattened-image")
        if best_under is not None:
            raise TargetRangeError(
                f"The closest result is {human_size(len(best_under))}, below your "
                f"minimum of {human_size(minimum_bytes)}. Lower the minimum and try again."
            )

        raise TargetRangeError(
            f"This PDF cannot be reduced below {human_size(maximum_bytes)} without "
            "making it unreadable. Choose a larger maximum size."
        )
    except (PdfInputError, TargetRangeError):
        raise
    except (RuntimeError, ValueError, MemoryError, OverflowError):
        raise PdfInputError(
            "The PDF could not be processed safely. Try another file or a larger range."
        ) from None
    finally:
        document.close()


def _render_error(message: str, status: int, values=None):
    if values is None:
        values = request.form
    return render_template("index.html", error=message, values=values), status


@app.get("/")
def index():
    """Serve the homepage with the upload form."""
    return render_template("index.html", values={})


@app.post("/compress")
def compress():
    """Validate, compress, and immediately return one uploaded PDF."""
    upload = request.files.get("pdf_file")
    if upload is None or not upload.filename:
        return _render_error("Choose a PDF file to compress.", 400)

    safe_name = secure_filename(upload.filename)
    if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
        return _render_error("Only files with a .pdf extension are accepted.", 400)

    try:
        minimum_bytes, maximum_bytes = parse_target_range(request.form)
        result = compress_pdf(
            upload.read(),
            minimum_bytes,
            maximum_bytes,
            allow_rasterization=request.form.get("allow_flatten") == "yes",
        )
    except ValueError as error:
        status = 422 if isinstance(error, (PdfInputError, TargetRangeError)) else 400
        return _render_error(str(error), status)

    suffix = "_flattened.pdf" if result.method == "flattened-image" else "_compressed.pdf"
    output_name = f"{Path(safe_name).stem}{suffix}"
    response = send_file(
        io.BytesIO(result.data),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=output_name,
        max_age=0,
    )
    response.headers["X-Original-Size"] = str(result.original_size)
    response.headers["X-Compressed-Size"] = str(result.compressed_size)
    response.headers["X-Compression-Method"] = result.method
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    # Accessing request.form here would try to parse the oversized body again.
    return _render_error("PDFs must be 50 MB or smaller.", 413, values={})


if __name__ == "__main__":
    app.run(debug=False)

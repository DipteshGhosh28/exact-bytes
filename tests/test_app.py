import io
import random
import unittest

import fitz

from app import PdfInputError, TargetRangeError, app, compress_pdf, parse_target_range


def make_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Exact Bytes test document")
    data = document.tobytes()
    document.close()
    return data


class ExactBytesTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.pdf = make_pdf()

    def post_pdf(self, *, filename="sample.pdf", minimum="0.1", maximum="100"):
        return self.client.post(
            "/compress",
            data={
                "pdf_file": (io.BytesIO(self.pdf), filename),
                "unit": "KB",
                "min_size": minimum,
                "max_size": maximum,
            },
            content_type="multipart/form-data",
        )

    def test_homepage_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Compress a PDF", response.data)

    def test_target_range_supports_decimal_mb_values(self):
        minimum, maximum = parse_target_range(
            {"unit": "MB", "min_size": "0.25", "max_size": "1.5"}
        )
        self.assertEqual(minimum, 262_144)
        self.assertEqual(maximum, 1_572_864)

    def test_extreme_target_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "50 MB"):
            parse_target_range(
                {"unit": "KB", "min_size": "1", "max_size": "1e1000000"}
            )

    def test_missing_file_is_rejected(self):
        response = self.client.post(
            "/compress",
            data={"unit": "KB", "min_size": "1", "max_size": "10"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Choose a PDF", response.data)

    def test_inverted_range_is_rejected(self):
        response = self.post_pdf(minimum="100", maximum="10")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Minimum size cannot", response.data)

    def test_non_pdf_extension_is_rejected(self):
        response = self.post_pdf(filename="sample.txt")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b".pdf extension", response.data)

    def test_invalid_pdf_content_is_rejected(self):
        response = self.client.post(
            "/compress",
            data={
                "pdf_file": (io.BytesIO(b"not a pdf"), "sample.pdf"),
                "unit": "KB",
                "min_size": "1",
                "max_size": "100",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"not a valid PDF", response.data)

    def test_pdf_in_range_downloads_without_disk_storage(self):
        response = self.post_pdf()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertIn(
            "sample_compressed.pdf", response.headers["Content-Disposition"]
        )
        self.assertEqual(
            int(response.headers["X-Compressed-Size"]), len(response.data)
        )
        self.assertEqual(
            response.headers["X-Compression-Method"], "already-in-range"
        )
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_pdf_below_minimum_returns_actionable_error(self):
        response = self.post_pdf(minimum="100", maximum="200")
        self.assertEqual(response.status_code, 422)
        self.assertIn(b"below your minimum", response.data)

    def test_lossless_compression_path_reaches_range(self):
        document = fitz.open(stream=self.pdf, filetype="pdf")
        compact = document.tobytes(
            garbage=4,
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            use_objstms=1,
            compression_effort=100,
        )
        document.close()

        padded = self.pdf + (b"\n% unused padding" * 800)
        minimum = max(1, len(compact) - 50)
        result = compress_pdf(padded, minimum, len(compact) + 50)

        self.assertEqual(result.method, "lossless")
        self.assertLess(result.compressed_size, result.original_size)
        self.assertGreaterEqual(result.compressed_size, minimum)
        self.assertLessEqual(result.compressed_size, len(compact) + 50)

    def test_image_optimized_path_reaches_range(self):
        width = height = 600
        samples = random.Random(42).randbytes(width * height * 3)
        pixmap = fitz.Pixmap(fitz.csRGB, width, height, samples, False)
        image = pixmap.tobytes("png")

        document = fitz.open()
        page = document.new_page(width=600, height=600)
        page.insert_image(page.rect, stream=image)
        source = document.tobytes(deflate=False)
        document.close()

        with self.assertRaisesRegex(TargetRangeError, "enable the image-based"):
            compress_pdf(source, 60 * 1024, 120 * 1024)

        result = compress_pdf(
            source,
            60 * 1024,
            120 * 1024,
            allow_rasterization=True,
        )

        self.assertEqual(result.method, "flattened-image")
        self.assertGreaterEqual(result.compressed_size, 60 * 1024)
        self.assertLessEqual(result.compressed_size, 120 * 1024)
        # The output must still be a readable one-page PDF.
        output = fitz.open(stream=result.data, filetype="pdf")
        self.assertEqual(output.page_count, 1)
        output.close()

    def test_encrypted_pdf_is_rejected_even_with_blank_user_password(self):
        document = fitz.open(stream=self.pdf, filetype="pdf")
        encrypted = document.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="",
            permissions=fitz.PDF_PERM_PRINT,
        )
        document.close()

        with self.assertRaisesRegex(PdfInputError, "Encrypted"):
            compress_pdf(encrypted, 1, len(encrypted) + 100)

    def test_oversized_page_is_not_rasterized(self):
        document = fitz.open()
        document.new_page(width=14_400, height=14_400)
        source = document.tobytes()
        document.close()

        with self.assertRaisesRegex(TargetRangeError, "too large"):
            compress_pdf(
                source,
                1,
                100,
                allow_rasterization=True,
            )


if __name__ == "__main__":
    unittest.main()

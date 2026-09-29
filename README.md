# ⚡ Exact Bytes — PDF Compressor

A beginner-friendly college MVP that compresses a PDF into a user-specified
size range (KB or MB). Built with Python, Flask, and PyMuPDF.

The first end-to-end MVP slice is complete: upload one PDF, validate the target
range, compress it, and receive the result as an immediate download. Files are
processed in memory and are never written to disk.

---

## Tech stack

| Layer      | Tool              |
|------------|-------------------|
| Backend    | Python + Flask    |
| PDF engine | PyMuPDF (fitz)    |
| Frontend   | HTML + Bootstrap 5 (CDN) |

---

## Project structure

```
Exact Bytes/
├── app.py               ← Flask routes
├── requirements.txt     ← Python dependencies
├── .gitignore
├── README.md
├── templates/
│   └── index.html       ← Upload form
└── static/
    └── style.css        ← Custom styles
```

---

## How to run (first time)

```bash
# 1. Create a virtual environment
python -m venv venv

# 2. Activate it
#    Windows PowerShell:
venv\Scripts\Activate.ps1
#    Mac / Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the server
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## Run the tests

```bash
python -m unittest discover -s tests -v
```

## Compression behavior

- A PDF already inside the requested range is returned unchanged.
- The app first tries lossless cleanup and stream compression.
- If the user explicitly allows it, the stronger fallback creates a flattened,
  image-only copy and searches bounded resolution/quality settings for the
  requested range. This can remove selectable text, links, forms, signatures,
  bookmarks, attachments, and accessibility data; flattened downloads are
  clearly named `_flattened.pdf`.
- If the original is below the minimum, or no readable result fits the range,
  the app explains how to adjust the target instead of returning a misleading
  file.
- Uploads are limited to 50 MB. Invalid, empty, corrupt, and password-protected
  PDFs are rejected with a friendly message.
- Raster work is bounded by page, pixel, and attempt limits so unusually large
  page dimensions or page counts fail safely instead of exhausting the server.

---

## Limitations (MVP)

- PDF compression only (no JPEG, no other formats).
- No user accounts or cloud storage — files are processed only in memory.
- Single-user development server; not suitable for public production traffic.
- Hitting an exact byte range is best-effort; some text-only PDFs resist compression.

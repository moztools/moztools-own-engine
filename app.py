import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="MozTools Documents Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://moztools.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
LIBREOFFICE_BIN = "libreoffice"
TEMP_ROOT = Path(tempfile.gettempdir()) / "moztools-engine"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

# Free-tier safety limits
PDF_TO_DOCX_MAX_PAGES = 2
PDF_TO_DOCX_MAX_BYTES = 3 * 1024 * 1024       # 3 MB

IMAGE_TO_PDF_MAX_BYTES = 3 * 1024 * 1024      # 3 MB per image
IMAGE_TO_PDF_MAX_IMAGES = 5                   # max 5 images per conversion

MERGE_PDF_MAX_FILES = 5                       # max 5 PDFs per merge

OUTPUT_FORMATS = [
    "csv", "docx", "html", "odp", "odt", "pdf",
    "ppt", "pptx", "rtf", "txt", "xls", "xlsx"
]

LIBREOFFICE_FORMATS = {
    "csv", "html", "odp", "odt", "pdf",
    "ppt", "pptx", "rtf", "txt", "xls", "xlsx"
}

ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png"}


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def cleanup_dir(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def unique_workdir() -> Path:
    workdir = TEMP_ROOT / uuid.uuid4().hex
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def convert_with_libreoffice(input_path: Path, output_dir: Path, target_format: str) -> Path:
    cmd = [
        LIBREOFFICE_BIN,
        "--headless",
        "--norestore",
        "--convert-to",
        target_format,
        "--outdir",
        str(output_dir),
        str(input_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Conversion timed out. The file may be too large or too complex.",
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="LibreOffice is not available on the server.",
        )

    expected = output_dir / f"{input_path.stem}.{target_format}"
    if expected.exists():
        return expected

    matches = list(output_dir.glob(f"*.{target_format}"))
    if matches:
        return matches[0]

    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    raise HTTPException(
        status_code=500,
        detail=f"LibreOffice could not produce a {target_format} file. {stderr}",
    )


def count_pdf_pages(input_path: Path) -> int:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="PDF page counting library is not available on the server.",
            )

    try:
        doc = pymupdf.open(str(input_path))
        page_count = doc.page_count
        doc.close()
        return page_count
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read the PDF file: {exc}",
        )


def convert_pdf_to_docx(input_path: Path, output_path: Path) -> Path:
    size = input_path.stat().st_size
    if size > PDF_TO_DOCX_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                "PDF -> DOCX on the free tier supports files up to "
                f"{PDF_TO_DOCX_MAX_BYTES // (1024 * 1024)} MB. "
                "Please use a smaller PDF."
            ),
        )

    pages = count_pdf_pages(input_path)
    if pages > PDF_TO_DOCX_MAX_PAGES:
        raise HTTPException(
            status_code=413,
            detail=(
                "PDF -> DOCX on the free tier supports up to "
                f"{PDF_TO_DOCX_MAX_PAGES} pages. "
                "Your PDF has "
                f"{pages} pages. Please use a smaller PDF. "
                "Tip: DOCX -> PDF has no page limit."
            ),
        )

    try:
        from pdf2docx import Converter
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="pdf2docx is not installed on the server.",
        )

    try:
        cv = Converter(str(input_path))
        cv.convert(str(output_path))
        cv.close()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PDF to DOCX conversion failed: {exc}",
        )

    if not output_path.exists():
        raise HTTPException(
            status_code=500,
            detail="PDF to DOCX conversion did not produce an output file.",
        )

    return output_path


# ------------------------------------------------------------
# NEW: image -> PDF (JPG / JPEG / PNG) using pymupdf
# ------------------------------------------------------------
def images_to_pdf(image_paths: List[Path], output_path: Path) -> Path:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="PDF library is not available on the server.",
            )

    try:
        doc = pymupdf.open()

        for img_path in image_paths:
            img_bytes = img_path.read_bytes()

            # pymupdf can insert from bytes; it detects the image type.
            img = pymupdf.open(stream=img_bytes, filetype=img_path.suffix.lstrip(".").lower())

            rect = img[0].rect
            pdf_bytes = img.convert_to_pdf()
            pdf_page = pymupdf.open("pdf", pdf_bytes)

            doc.insert_pdf(pdf_page)

            pdf_page.close()
            img.close()

        if doc.page_count == 0:
            doc.close()
            raise HTTPException(
                status_code=400,
                detail="No valid images were found to convert.",
            )

        doc.save(str(output_path))
        doc.close()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Image to PDF conversion failed: {exc}",
        )

    if not output_path.exists():
        raise HTTPException(
            status_code=500,
            detail="Image to PDF conversion did not produce an output file.",
        )

    return output_path


# ------------------------------------------------------------
# NEW: merge multiple PDFs into one using pymupdf
# ------------------------------------------------------------
def merge_pdfs(pdf_paths: List[Path], output_path: Path) -> Path:
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="PDF library is not available on the server.",
            )

    try:
        merged = pymupdf.open()

        for pdf_path in pdf_paths:
            src = pymupdf.open(str(pdf_path))
            merged.insert_pdf(src)
            src.close()

        if merged.page_count == 0:
            merged.close()
            raise HTTPException(
                status_code=400,
                detail="The uploaded PDFs have no pages.",
            )

        merged.save(str(output_path))
        merged.close()

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"PDF merge failed: {exc}",
        )

    if not output_path.exists():
        raise HTTPException(
            status_code=500,
            detail="PDF merge did not produce an output file.",
        )

    return output_path


# ------------------------------------------------------------
# Existing endpoints (unchanged)
# ------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "moztools-documents-engine"}


@app.get("/formats")
def formats():
    return {
        "output_formats": OUTPUT_FORMATS,
        "note": "Actual conversions depend on the input file and LibreOffice support.",
        "special": ["image->pdf", "merge-pdf"],
    }


# ------------------------------------------------------------
# Existing convert endpoint (unchanged)
# ------------------------------------------------------------
@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    output_format: str = Form(...),
):
    output_format = output_format.lower().strip()

    if output_format not in OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported output format: {output_format}",
        )

    original_name = file.filename or "input"
    suffix = Path(original_name).suffix.lower()

    workdir = unique_workdir()
    input_path = workdir / f"input{suffix}"
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with input_path.open("wb") as buffer:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                buffer.write(chunk)
    except Exception as exc:
        cleanup_dir(workdir)
        raise HTTPException(
            status_code=500,
            detail=f"Could not save uploaded file: {exc}",
        )

    produced_path: Optional[Path] = None

    try:
        if suffix == ".pdf" and output_format == "docx":
            produced_path = output_dir / "result.docx"
            convert_pdf_to_docx(input_path, produced_path)

        elif output_format in LIBREOFFICE_FORMATS:
            produced_path = convert_with_libreoffice(
                input_path=input_path,
                output_dir=output_dir,
                target_format=output_format,
            )

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Conversion from {suffix or 'unknown'} to {output_format} is not supported by this engine yet.",
            )

        if produced_path is None or not produced_path.exists():
            raise HTTPException(
                status_code=500,
                detail="Conversion finished without producing an output file.",
            )

        download_name = f"{Path(original_name).stem}.{output_format}"
        return FileResponse(
            path=str(produced_path),
            media_type="application/octet-stream",
            filename=download_name,
            background=BackgroundTask(cleanup_dir, workdir),
        )

    except HTTPException:
        cleanup_dir(workdir)
        raise

    except Exception as exc:
        cleanup_dir(workdir)
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected conversion error: {exc}",
        )


# ------------------------------------------------------------
# NEW endpoint: image -> PDF
# ------------------------------------------------------------
@app.post("/convert-image")
async def convert_image(
    files: List[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="No images were uploaded.")

    if len(files) > IMAGE_TO_PDF_MAX_IMAGES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Image -> PDF on the free tier supports up to "
                f"{IMAGE_TO_PDF_MAX_IMAGES} images per conversion. "
                f"You uploaded {len(files)}."
            ),
        )

    workdir = unique_workdir()
    image_paths: List[Path] = []
    output_path = workdir / "result.pdf"

    try:
        for idx, upload in enumerate(files):
            name = upload.filename or f"image{idx}"
            suffix = Path(name).suffix.lower()

            if suffix not in ALLOWED_IMAGE_EXT:
                cleanup_dir(workdir)
                raise HTTPException(
                    status_code=400,
                    detail=f"Only JPG, JPEG and PNG images are supported. Got: {suffix or 'unknown'}.",
                )

            target = workdir / f"img_{idx}{suffix}"

            with target.open("wb") as buffer:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    buffer.write(chunk)

            if target.stat().st_size > IMAGE_TO_PDF_MAX_BYTES:
                cleanup_dir(workdir)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Each image must be up to "
                        f"{IMAGE_TO_PDF_MAX_BYTES // (1024 * 1024)} MB on the free tier. "
                        f"{name} is larger."
                    ),
                )

            image_paths.append(target)

        images_to_pdf(image_paths, output_path)

        return FileResponse(
            path=str(output_path),
            media_type="application/pdf",
            filename="moztools-images.pdf",
            background=BackgroundTask(cleanup_dir, workdir),
        )

    except HTTPException:
        cleanup_dir(workdir)
        raise

    except Exception as exc:
        cleanup_dir(workdir)
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected image conversion error: {exc}",
        )


# ------------------------------------------------------------
# NEW endpoint: merge PDFs
# ------------------------------------------------------------
@app.post("/merge-pdf")
async def merge_pdf(
    files: List[UploadFile] = File(...),
):
    if not files:
        raise HTTPException(status_code=400, detail="No PDFs were uploaded.")

    if len(files) < 2:
        raise HTTPException(
            status_code=400,
            detail="Merging requires at least 2 PDF files.",
        )

    if len(files) > MERGE_PDF_MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Merging on the free tier supports up to "
                f"{MERGE_PDF_MAX_FILES} PDFs per merge. "
                f"You uploaded {len(files)}."
            ),
        )

    workdir = unique_workdir()
    pdf_paths: List[Path] = []
    output_path = workdir / "merged.pdf"

    try:
        for idx, upload in enumerate(files):
            name = upload.filename or f"file{idx}"
            suffix = Path(name).suffix.lower()

            if suffix != ".pdf":
                cleanup_dir(workdir)
                raise HTTPException(
                    status_code=400,
                    detail=f"Only PDF files can be merged. Got: {suffix or 'unknown'}.",
                )

            target = workdir / f"doc_{idx}.pdf"

            with target.open("wb") as buffer:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    buffer.write(chunk)

            pdf_paths.append(target)

        merge_pdfs(pdf_paths, output_path)

        return FileResponse(
            path=str(output_path),
            media_type="application/pdf",
            filename="moztools-merged.pdf",
            background=BackgroundTask(cleanup_dir, workdir),
        )

    except HTTPException:
        cleanup_dir(workdir)
        raise

    except Exception as exc:
        cleanup_dir(workdir)
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected merge error: {exc}",
        )

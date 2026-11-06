import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional

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

# Free-tier safety limits for pdf2docx.
# pdf2docx uses a lot of RAM; on Render Free (512 MB) larger PDFs crash the instance.
PDF_TO_DOCX_MAX_PAGES = 2
PDF_TO_DOCX_MAX_BYTES = 3 * 1024 * 1024  # 3 MB

OUTPUT_FORMATS = [
    "csv", "docx", "html", "odp", "odt", "pdf",
    "ppt", "pptx", "rtf", "txt", "xls", "xlsx"
]

LIBREOFFICE_FORMATS = {
    "csv", "html", "odp", "odt", "pdf",
    "ppt", "pptx", "rtf", "txt", "xls", "xlsx"
}


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
    # Size guard first.
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

    # Page count guard.
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
    }


# ------------------------------------------------------------
# Conversion endpoint
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

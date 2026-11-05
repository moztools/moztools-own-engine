import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

app = FastAPI(title="MozTools Documents Engine", version="1.0.0")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
TIMEOUT_SECONDS = int(os.getenv("CONVERSION_TIMEOUT_SECONDS", "120"))

# LibreOffice handles common office/document conversions.
SUPPORTED_OUTPUTS = {
    "pdf", "docx", "odt", "rtf", "txt", "html",
    "xlsx", "xls", "csv", "pptx", "odp", "ppt"
}

@app.get("/health")
def health():
    return {"status": "ok", "service": "moztools-documents-engine"}

@app.get("/formats")
def formats():
    return {
        "output_formats": sorted(SUPPORTED_OUTPUTS),
        "note": "Actual conversions depend on the input file and LibreOffice support."
    }

@app.post("/convert")
async def convert(
    file: UploadFile = File(...),
    output_format: str = Form(...)
):
    output_format = output_format.lower().strip().lstrip(".")
    if output_format not in SUPPORTED_OUTPUTS:
        raise HTTPException(status_code=400, detail="Unsupported output format.")

    original_name = Path(file.filename or "upload").name
    input_suffix = Path(original_name).suffix.lower()
    if not input_suffix:
        raise HTTPException(status_code=400, detail="The uploaded file must have an extension.")

    workdir = Path(tempfile.mkdtemp(prefix="moztools-"))
    try:
        input_path = workdir / f"input{input_suffix}"
        output_dir = workdir / "output"
        output_dir.mkdir()

        size = 0
        with input_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"Maximum upload size is {MAX_UPLOAD_MB} MB.")
                target.write(chunk)

        command = [
            "soffice", "--headless", "--nologo", "--nodefault",
            "--nolockcheck", "--convert-to", output_format,
            "--outdir", str(output_dir), str(input_path)
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Conversion timed out. Try a smaller file.")

        expected = output_dir / f"input.{output_format}"
        if result.returncode != 0 or not expected.exists():
            detail = (result.stderr or result.stdout or "This input-to-output conversion is not supported.")
            raise HTTPException(status_code=422, detail=detail[-1200:])

        download_name = f"{Path(original_name).stem}.{output_format}"
        return FileResponse(
            path=str(expected),
            filename=download_name,
            media_type="application/octet-stream",
            background=None
        )
    finally:
        # FileResponse streams after this endpoint returns, so don't remove
        # the temporary directory here. The container's periodic cleanup
        # or host lifecycle should remove it; see README for production notes.
        pass

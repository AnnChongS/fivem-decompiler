"""
FiveM FXAP Decompiler — Web Interface
FastAPI app with file upload and SSE progress
"""

import os
import uuid
import asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from decompiler import process_resource

app = FastAPI(title="FiveM FXAP Decompiler")
BASE_DIR = Path(__file__).parent
UPLOAD_DIR = Path("/tmp/fivem_uploads")
RESULT_DIR = Path("/tmp/fivem_results")
UPLOAD_DIR.mkdir(exist_ok=True)
RESULT_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Track job progress
jobs = {}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    license_key: str = Form(...)
):
    if not file.filename.endswith(".zip"):
        return JSONResponse({"error": "File must be a .zip"}, status_code=400)

    if not license_key.strip():
        return JSONResponse({"error": "License key is required"}, status_code=400)

    job_id = str(uuid.uuid4())[:8]

    # Save uploaded file
    zip_path = UPLOAD_DIR / f"{job_id}.zip"
    content = await file.read()
    zip_path.write_bytes(content)

    # Start processing in background
    jobs[job_id] = {"status": "queued", "progress": "Waiting...", "result": None, "error": None}
    asyncio.create_task(run_job(job_id, str(zip_path), license_key.strip()))

    return JSONResponse({"job_id": job_id})


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job)


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("result"):
        return JSONResponse({"error": "Not ready"}, status_code=404)
    return FileResponse(
        job["result"],
        media_type="application/zip",
        filename=Path(job["result"]).name
    )


async def run_job(job_id: str, zip_path: str, license_key: str):
    def callback(msg):
        jobs[job_id]["progress"] = msg

    try:
        jobs[job_id]["status"] = "processing"
        loop = asyncio.get_event_loop()
        result_zip = await loop.run_in_executor(
            None, lambda: process_resource(zip_path, license_key, callback)
        )
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = result_zip
        jobs[job_id]["progress"] = "Complete!"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["progress"] = f"Error: {e}"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

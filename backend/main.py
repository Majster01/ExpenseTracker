"""Unauthenticated HTTP API for uploading bank statements.

Run locally with:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000

This service is intended for a private or trusted network. It has no login
endpoint or application-level authentication.
"""
import os

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import processor

app = FastAPI(title="Expense Tracker API")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_PARSERS = {"nlb", "otp"}
SCOPES = processor.SCOPES

def _sheets_service():
    credentials_path = os.getenv("SERVICE_ACCOUNT_FILE")
    if credentials_path:
        credentials = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
    else:
        credentials, _project_id = google.auth.default(scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials)


@app.post("/statements")
async def upload_statement(
    file: UploadFile = File(...),
    parser_type: str = Form(...),
):
    if parser_type not in ALLOWED_PARSERS:
        raise HTTPException(status_code=400, detail="Unsupported parser type")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="A PDF statement is required")

    try:
        pdf_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(pdf_bytes) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded file is too large")
        if len(pdf_bytes) < 4 or pdf_bytes[:4] != b"%PDF":
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF")

        result = processor.process_and_track_statement(
            parser_type,
            pdf_bytes,
            _sheets_service(),
        )
        return result
    except HTTPException:
        raise
    except FileNotFoundError as error:
        raise HTTPException(status_code=500, detail="Backend configuration is incomplete") from error
    except Exception as error:
        raise HTTPException(status_code=500, detail="Statement processing failed") from error
    finally:
        await file.close()

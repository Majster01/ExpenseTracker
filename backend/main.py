"""HTTP API for uploading bank statements with Google Sheets authorization.

Run locally with:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000

When configured with a Google client ID, uploads require caller OAuth access
to the working spreadsheet.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from google.oauth2 import credentials as user_credentials
from googleapiclient.discovery import build

from . import processor

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Expense Tracker API")
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_PARSERS = {"nlb", "otp"}
SCOPES = processor.SCOPES
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000,https://majster01.github.io").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

def _require_google_user(authorization: Optional[str]):
    log.info(
        "Google Sheets authorization check configured=%s required_scopes=%s spreadsheet_id=%s",
        bool(GOOGLE_CLIENT_ID),
        SCOPES,
        processor.SPREADSHEET_ID,
    )
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    access_token = authorization[7:].strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    log.info("Google Sheets caller token received token_length=%d", len(access_token))

    credentials = user_credentials.Credentials(
        token=access_token,
        scopes=SCOPES,
    )
    sheets_service = build("sheets", "v4", credentials=credentials)
    try:
        sheets_service.spreadsheets().get(
            spreadsheetId=processor.SPREADSHEET_ID,
            fields="spreadsheetId",
        ).execute()
    except RefreshError as error:
        log.warning("Google Sheets authorization failed reason=token_refresh_failed")
        raise HTTPException(status_code=401, detail="Invalid or expired Google sign-in") from error
    except HttpError as error:
        log.warning(
            "Google Sheets authorization failed status_code=%s reason=%s",
            error.resp.status,
            _google_error_reason(error),
        )
        if error.resp.status == 401:
            raise HTTPException(status_code=401, detail="Invalid or expired Google sign-in") from error
        if error.resp.status == 403:
            raise HTTPException(
                status_code=403,
                detail="Your Google account does not have access to the working spreadsheet",
            ) from error
        raise HTTPException(status_code=502, detail="Could not validate Google Sheets access") from error
    log.info("Validated caller access to spreadsheet client_id=%s", GOOGLE_CLIENT_ID)
    return sheets_service


def _google_error_reason(error: HttpError) -> str:
    """Return a short Google API error reason without logging response content."""
    try:
        error_data = json.loads(error.content.decode("utf-8"))
        errors = error_data.get("error", {}).get("errors", [])
        if errors and errors[0].get("reason"):
            return errors[0]["reason"]
        return error_data.get("error", {}).get("status", "unknown")
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return "unknown"


@app.post("/statements")
async def upload_statement(
    file: UploadFile = File(...),
    parser_type: str = Form(...),
    authorization: Optional[str] = Header(default=None),
):
    request_id = str(uuid4())
    sheets_service = _require_google_user(authorization)
    filename = file.filename or ""
    log.info(
        "Statement upload started request_id=%s parser_type=%s filename=%s",
        request_id,
        parser_type,
        filename,
    )
    if parser_type not in ALLOWED_PARSERS:
        log.warning("Statement rejected request_id=%s reason=unsupported_parser", request_id)
        raise HTTPException(status_code=400, detail="Unsupported parser type")
    if not filename.lower().endswith(".pdf"):
        log.warning("Statement rejected request_id=%s reason=not_pdf", request_id)
        raise HTTPException(status_code=400, detail="A PDF statement is required")

    try:
        log.info("Reading PDF request_id=%s max_bytes=%d", request_id, MAX_UPLOAD_BYTES)
        pdf_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
        log.info("PDF read request_id=%s bytes=%d", request_id, len(pdf_bytes))
        if len(pdf_bytes) > MAX_UPLOAD_BYTES:
            log.warning("Statement rejected request_id=%s reason=too_large", request_id)
            raise HTTPException(status_code=413, detail="Uploaded file is too large")
        if len(pdf_bytes) < 4 or pdf_bytes[:4] != b"%PDF":
            log.warning("Statement rejected request_id=%s reason=invalid_pdf_header", request_id)
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF")

        log.info("Processing statement request_id=%s parser_type=%s", request_id, parser_type)
        result = processor.process_and_track_statement(
            parser_type,
            pdf_bytes,
            sheets_service,
        )
        log.info(
            "Statement upload completed request_id=%s rows_added=%s duplicates_removed=%s",
            request_id,
            result.get("rows_added"),
            result.get("duplicates_removed"),
        )
        return result
    except HTTPException as error:
        log.warning(
            "Statement failed request_id=%s status_code=%d detail=%s",
            request_id,
            error.status_code,
            error.detail,
        )
        raise
    except FileNotFoundError as error:
        log.exception("Statement failed request_id=%s reason=missing_file", request_id)
        raise HTTPException(status_code=500, detail="Backend configuration is incomplete") from error
    except Exception as error:
        log.exception("Statement failed request_id=%s reason=unexpected_error", request_id)
        raise HTTPException(status_code=500, detail="Statement processing failed") from error
    finally:
        await file.close()

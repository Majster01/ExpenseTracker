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
import base64
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import Cookie, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleRequest
from google.auth.exceptions import RefreshError
from google.oauth2 import id_token
from googleapiclient.errors import HttpError
from google.oauth2 import credentials as user_credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from urllib import parse, request as url_request
from urllib.error import HTTPError

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
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://expensetracker-git-178545711969.europe-west1.run.app")
SESSION_COOKIE = "expense_session"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "expense_tracker_oauth")
TOKEN_ENCRYPTION_KEY = os.getenv("TOKEN_ENCRYPTION_KEY")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
_firestore_client = None
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8000,https://expensetracker-git-178545711969.europe-west1.run.app").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


class OAuthCodeRequest(BaseModel):
    code: str


def _get_firestore():
    global _firestore_client
    if _firestore_client is None:
        from google.cloud import firestore

        _firestore_client = firestore.Client()
    return _firestore_client


def _get_encryption_key() -> bytes:
    if not TOKEN_ENCRYPTION_KEY:
        raise HTTPException(status_code=500, detail="OAuth token encryption is not configured")
    try:
        key = base64.urlsafe_b64decode(TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, base64.binascii.Error) as error:
        raise HTTPException(status_code=500, detail="OAuth token encryption is not configured") from error
    if len(key) not in (16, 24, 32):
        raise HTTPException(status_code=500, detail="OAuth token encryption is not configured")
    return key


def _encrypt_refresh_token(refresh_token: str) -> str:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_get_encryption_key()).encrypt(nonce, refresh_token.encode(), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode()


def _decrypt_refresh_token(value: str) -> str:
    encrypted = base64.urlsafe_b64decode(value.encode())
    return AESGCM(_get_encryption_key()).decrypt(encrypted[:12], encrypted[12:], None).decode()


def _oauth_token_request(values: dict[str, str]) -> dict:
    body = parse.urlencode(values).encode()
    http_request = url_request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with url_request.urlopen(http_request, timeout=15) as result:
            payload = json.loads(result.read().decode())
    except HTTPError as error:
        try:
            error_payload = json.loads(error.read().decode())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            error_payload = {}
        log.warning(
            "OAuth token request failed status=%s error=%s description=%s",
            error.code,
            error_payload.get("error", "unknown"),
            error_payload.get("error_description", "none"),
        )
        raise HTTPException(status_code=401, detail="Google authorization failed") from error
    except Exception as error:
        log.warning("OAuth token request failed reason=%s", type(error).__name__)
        raise HTTPException(status_code=401, detail="Google authorization failed") from error
    if payload.get("error"):
        log.warning("OAuth token request rejected error=%s", payload["error"])
        raise HTTPException(status_code=401, detail="Google authorization failed")
    return payload


def _validate_identity(payload: dict) -> str:
    identity_token = payload.get("id_token")
    if not identity_token:
        raise HTTPException(status_code=401, detail="Google identity was not returned")
    try:
        claims = id_token.verify_oauth2_token(identity_token, GoogleRequest(), GOOGLE_CLIENT_ID)
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Google identity could not be verified") from error
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Google identity could not be verified")
    return subject


def _create_session(subject: str) -> str:
    session_id = secrets.token_urlsafe(32)
    _get_firestore().collection(FIRESTORE_COLLECTION).document(f"session:{session_id}").set({
        "type": "session",
        "subject": subject,
        "expires_at": datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS),
    })
    return session_id


def _get_session(session_id: Optional[str]) -> dict:
    if not session_id:
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    snapshot = _get_firestore().collection(FIRESTORE_COLLECTION).document(f"session:{session_id}").get()
    session = snapshot.to_dict() if snapshot.exists else None
    if not session or session.get("expires_at", datetime.min.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    return session


def _store_google_tokens(subject: str, payload: dict) -> None:
    refresh_token = payload.get("refresh_token")
    document = _get_firestore().collection(FIRESTORE_COLLECTION).document(f"user:{subject}")
    values = {
        "type": "google_tokens",
        "subject": subject,
        "scopes": payload.get("scope", "").split(),
        "updated_at": datetime.now(timezone.utc),
    }
    if refresh_token:
        values["refresh_token"] = _encrypt_refresh_token(refresh_token)
    elif not document.get().exists:
        raise HTTPException(status_code=401, detail="Google refresh access was not granted")
    document.set(values, merge=True)


def _refresh_google_access_token(subject: str) -> dict:
    snapshot = _get_firestore().collection(FIRESTORE_COLLECTION).document(f"user:{subject}").get()
    stored = snapshot.to_dict() if snapshot.exists else None
    if not stored or not stored.get("refresh_token"):
        raise HTTPException(status_code=401, detail="Google sign-in is required")
    try:
        payload = _oauth_token_request({
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET or "",
            "refresh_token": _decrypt_refresh_token(stored["refresh_token"]),
            "grant_type": "refresh_token",
        })
    except HTTPException:
        raise HTTPException(status_code=401, detail="Google sign-in has expired")
    _store_google_tokens(subject, payload)
    return payload


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )


@app.post("/auth/login")
def login(
    request: OAuthCodeRequest,
    response: Response,
    x_requested_with: Optional[str] = Header(default=None),
):
    if x_requested_with != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="Invalid authorization request")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Google OAuth is not configured")
    token_payload = _oauth_token_request({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": request.code,
        "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    })
    if not set(SCOPES).issubset(set(token_payload.get("scope", "").split())):
        raise HTTPException(status_code=403, detail="Google Sheets access was not granted")
    subject = _validate_identity(token_payload)
    _store_google_tokens(subject, token_payload)
    _set_session_cookie(response, _create_session(subject))
    return {
        "access_token": token_payload["access_token"],
        "expires_in": token_payload.get("expires_in", 3600),
    }


@app.post("/auth/refresh")
def refresh_access_token(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    session = _get_session(session_id)
    token_payload = _refresh_google_access_token(session["subject"])
    _set_session_cookie(response, session_id)
    return {
        "access_token": token_payload["access_token"],
        "expires_in": token_payload.get("expires_in", 3600),
    }


@app.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if session_id:
        _get_firestore().collection(FIRESTORE_COLLECTION).document(f"session:{session_id}").delete()
    response.delete_cookie(SESSION_COOKIE)


def _require_google_user(authorization: Optional[str] = None, session_id: Optional[str] = None):
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
    if session_id:
        session = _get_session(session_id)
        access_token = _refresh_google_access_token(session["subject"])["access_token"]
    else:
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
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    request_id = str(uuid4())
    sheets_service = _require_google_user(authorization, session_id)
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


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

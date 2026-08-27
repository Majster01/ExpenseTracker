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

from fastapi import Cookie, FastAPI, File, Form, Header, HTTPException, Path, Request, Response, UploadFile
from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
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
from .rules import DEFAULT_COLLECTION, RulesRepository

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Expense Tracker API")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
ALLOWED_PARSERS = {"nlb", "otp"}
SCOPES = processor.SCOPES
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
SHEET_URL = os.getenv(
    "SHEET_URL",
    "https://docs.google.com/spreadsheets/d/1JlIH41lNNVPEa3WJa9E7mZP5q3yY5YHm52vdJVK2PnI/edit?gid=1091925467#gid=1091925467",
)
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://expensetracker-git-178545711969.europe-west1.run.app")
SESSION_COOKIE = "expense_session"
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(30 * 24 * 60 * 60)))
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "expense_tracker_oauth")
RULES_COLLECTION = os.getenv("RULES_COLLECTION", DEFAULT_COLLECTION)
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", "").split(",")
    if email.strip()
}
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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)


class OAuthCodeRequest(BaseModel):
    code: str


class RuleRequest(BaseModel):
    keywords: list[str]
    order: int
    original_category: Optional[str] = None


class CategoryRequest(BaseModel):
    category: str


def _wants_html(hx_request) -> bool:
    """True only for a real htmx-initiated request.

    Route handlers are also called directly (without going through FastAPI's
    request handling) by unit tests in tests/test_main.py, which never pass
    this argument. In that case its value is the raw `Header(...)` marker
    object rather than a string, so the isinstance check keeps those calls on
    the original JSON code path.
    """
    return isinstance(hx_request, str) and bool(hx_request)


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


def _validate_identity(payload: dict) -> dict:
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
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise HTTPException(status_code=401, detail="Google identity email could not be verified")
    return claims


def _create_session(subject: str, email: str) -> str:
    session_id = secrets.token_urlsafe(32)
    _get_firestore().collection(FIRESTORE_COLLECTION).document(f"session:{session_id}").set({
        "type": "session",
        "subject": subject,
        "email": email.lower(),
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
    claims = _validate_identity(token_payload)
    subject = claims["sub"]
    _store_google_tokens(subject, token_payload)
    session_id = _create_session(subject, claims["email"])
    _set_session_cookie(response, session_id)
    return {
        "access_token": token_payload["access_token"],
        "expires_in": token_payload.get("expires_in", 3600),
        "is_admin": claims["email"].lower() in ADMIN_EMAILS,
    }


@app.post("/auth/refresh")
def refresh_access_token(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
):
    session = _get_session(session_id)
    token_payload = _refresh_google_access_token(session["subject"])
    is_admin = session.get("email", "").lower() in ADMIN_EMAILS
    if _wants_html(hx_request):
        # A returned Response (the TemplateResponse) replaces the injected
        # `response` entirely, so cookies/headers must be set on it directly
        # rather than on `response` -- mutating `response` here would be silently discarded.
        # No HX-Trigger here: #auth-poller (base.html) listens for `auth-changed`
        # itself, so announcing it on every routine refresh would re-trigger
        # this same request forever. The rules panel gets its own `load`
        # trigger instead (index.html) for the returning-admin case.
        template_response = templates.TemplateResponse(http_request, "partials/topbar.html", {"state": "signed_in"})
        _set_session_cookie(template_response, session_id)
        return template_response
    _set_session_cookie(response, session_id)
    return {
        "access_token": token_payload["access_token"],
        "expires_in": token_payload.get("expires_in", 3600),
        "is_admin": is_admin,
    }


@app.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
):
    if session_id:
        _get_firestore().collection(FIRESTORE_COLLECTION).document(f"session:{session_id}").delete()
    if _wants_html(hx_request):
        template_response = templates.TemplateResponse(http_request, "partials/topbar.html", {"state": "signed_out"})
        template_response.delete_cookie(SESSION_COOKIE)
        template_response.headers["HX-Trigger"] = "auth-changed"
        return template_response
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


def _require_admin(session_id: Optional[str]) -> dict:
    session = _get_session(session_id)
    if session.get("email", "").lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Category rule administration is not enabled for this account")
    return session


def _is_admin(session_id: Optional[str]) -> bool:
    """Non-raising admin check for pages that only need to know whether to
    show admin nav links, as opposed to `_require_admin`, which gates access
    to admin actions and must reject a missing/expired session."""
    if not session_id:
        return False
    try:
        session = _get_session(session_id)
    except HTTPException:
        return False
    return session.get("email", "").lower() in ADMIN_EMAILS


def _rules_repository() -> RulesRepository:
    return RulesRepository(_get_firestore(), collection_name=RULES_COLLECTION)


def _validate_category(category: str) -> str:
    category = category.strip()
    if not category or len(category) > 80 or "/" in category:
        raise HTTPException(status_code=422, detail="Category must be 1-80 characters and cannot contain '/'")
    return category


def _validate_rule_request(request: RuleRequest) -> list[str]:
    if request.order < 0:
        raise HTTPException(status_code=422, detail="Rule order must be zero or greater")
    keywords = []
    for keyword in request.keywords:
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 200:
            raise HTTPException(status_code=422, detail="Keywords must be non-empty strings of 200 characters or fewer")
        keywords.append(keyword.strip())
    return keywords


def _sync_category_to_sheets(sheets_service, category: str) -> None:
    try:
        processor.add_category_to_named_range(sheets_service, category)
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except HttpError as error:
        log.warning("Category sync failed status_code=%s reason=%s", error.resp.status, _google_error_reason(error))
        raise HTTPException(status_code=502, detail="Could not update AvailableCategories in Google Sheets") from error
    except Exception as error:
        log.exception("Category sync failed unexpectedly category=%s", category)
        raise HTTPException(status_code=502, detail="Could not update AvailableCategories in Google Sheets") from error


@app.get("/rules")
def list_rules(
    request: Request,
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    try:
        _require_admin(session_id)
    except HTTPException:
        return RedirectResponse("/", status_code=303)
    rules = _rules_repository().list_rules()
    return templates.TemplateResponse(request, "rules.html", {"rules": rules, "is_admin": True})


@app.post("/categories")
def create_category(
    request: CategoryRequest,
    authorization: Optional[str] = Header(default=None),
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
):
    _require_admin(session_id)
    category = _validate_category(request.category)
    sheets_service = _require_google_user(authorization, session_id)
    repository = _rules_repository()
    rules = repository.get_rules()
    existing_category = next(
        (name for name in rules if name.casefold() == category.casefold()),
        None,
    )
    if existing_category is not None:
        _sync_category_to_sheets(sheets_service, existing_category)
        result = {"category": existing_category, "order": list(rules).index(existing_category), "created": False}
    else:
        order = len(rules)
        _sync_category_to_sheets(sheets_service, category)
        repository.save(category, [], order)
        result = {"category": category, "order": order, "created": True}

    if _wants_html(hx_request):
        message = "Category already exists." if not result["created"] else "Category added."
        return templates.TemplateResponse(
            http_request, "partials/rules_section.html", {"rules": repository.list_rules(), "message": message}
        )
    return result


@app.put("/rules/{category}")
def save_rule(
    request: RuleRequest,
    category: str = Path(..., min_length=1, max_length=80),
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
):
    _require_admin(session_id)
    category = _validate_category(category)
    keywords = _validate_rule_request(request)
    repository = _rules_repository()
    if request.original_category and request.original_category != category:
        repository.delete(_validate_category(request.original_category))
    repository.save(category, keywords, request.order)

    if _wants_html(hx_request):
        return templates.TemplateResponse(
            http_request,
            "partials/rules_section.html",
            {"rules": repository.list_rules(), "message": "Category rule saved."},
        )
    return {"category": category, "keywords": [keyword.lower() for keyword in keywords], "order": request.order}


@app.delete("/rules/{category}", status_code=204)
def delete_rule(
    category: str = Path(..., min_length=1, max_length=80),
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
):
    _require_admin(session_id)
    repository = _rules_repository()
    repository.delete(_validate_category(category))
    if _wants_html(hx_request):
        return templates.TemplateResponse(
            http_request,
            "partials/rules_section.html",
            {"rules": repository.list_rules(), "message": "Category rule deleted."},
        )


@app.post("/statements")
async def upload_statement(
    file: UploadFile = File(...),
    parser_type: str = Form(...),
    authorization: Optional[str] = Header(default=None),
    session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
    hx_request: Optional[str] = Header(default=None, alias="HX-Request"),
    http_request: Request = None,
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
        if _wants_html(hx_request):
            return templates.TemplateResponse(
                http_request,
                "partials/result_panel.html",
                {
                    "show": True,
                    "rows_added": result.get("rows_added", 0),
                    "needs_categorization": result.get("needs_categorization", 0),
                    "sheet_url": SHEET_URL,
                },
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


@app.exception_handler(HTTPException)
async def htmx_aware_http_exception_handler(request: Request, exc: HTTPException):
    if request.headers.get("HX-Request"):
        if request.url.path.startswith("/statements"):
            return templates.TemplateResponse(
                request,
                "partials/upload_panel.html",
                {"message": exc.detail, "error": True},
                status_code=exc.status_code,
            )
        if request.url.path.startswith("/auth/"):
            return templates.TemplateResponse(
                request, "partials/topbar.html", {"state": "signed_out"}, status_code=exc.status_code
            )
        try:
            rules = _rules_repository().list_rules()
        except Exception:
            log.exception("Could not load rules while rendering an htmx error fragment")
            rules = []
        return templates.TemplateResponse(
            request,
            "partials/rules_section.html",
            {"rules": rules, "message": exc.detail, "error": True},
            status_code=exc.status_code,
        )
    return await default_http_exception_handler(request, exc)


@app.get("/")
def index(request: Request, session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "google_client_id": GOOGLE_CLIENT_ID,
            "sheet_url": SHEET_URL,
            "rules": [],
            "message": "",
            "is_admin": _is_admin(session_id),
        },
    )


@app.get("/review")
def review(request: Request, session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    return templates.TemplateResponse(request, "review.html", {"is_admin": _is_admin(session_id)})


@app.get("/settings")
def settings_page(request: Request, session_id: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    try:
        _require_admin(session_id)
    except HTTPException:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "settings.html", {"is_admin": True})


@app.get("/statements/new")
def new_statement_form(request: Request):
    return templates.TemplateResponse(request, "partials/upload_panel.html", {"message": ""})


app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")

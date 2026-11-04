"""Gmail API client."""

import base64
import os
import re
from html import unescape
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from job_email_reader.models import Email

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = Path("credentials.json")
TOKEN_PATH = Path("token.json")
BODY_LIMIT = 4000

_BLOCK_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>|<!--.*?-->")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _parts(payload: dict) -> tuple[str | None, str | None]:
    plain = None
    html = None
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime == "text/plain":
        plain = _decode(data)
    elif data and mime == "text/html":
        html = _decode(data)
    for part in payload.get("parts") or []:
        child_plain, child_html = _parts(part)
        if plain is None:
            plain = child_plain
        if html is None:
            html = child_html
    return plain, html


def _body_text(payload: dict) -> str:
    plain, html = _parts(payload)
    if plain:
        text = plain.strip()
    elif html:
        text = _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", _BLOCK_RE.sub(" ", html)))).strip()
    else:
        text = ""
    return text[:BODY_LIMIT]


def _header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for header in headers:
        if header.get("name", "").lower() == target:
            return header.get("value", "")
    return ""


def _env_int(name: str, default: int) -> int:
    load_dotenv()
    raw = os.getenv(name, str(default)).strip() or str(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return value


def _max_emails() -> int:
    return _env_int("MAX_EMAILS", 50)


def lookback_days() -> int:
    return _env_int("LOOKBACK_DAYS", 1)


def fetch_job_emails(days: int | None = None, max_results: int | None = None) -> list[Email]:
    if days is None:
        days = lookback_days()
    elif days < 1:
        raise RuntimeError("days must be at least 1")
    if max_results is None:
        max_results = _max_emails()
    service = _service()
    query = (
        f"newer_than:{days}d (application OR applied OR interview OR assessment "
        f'OR recruiter OR "thank you for applying" OR unfortunately OR offer) '
        "-category:promotions"
    )
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(500, max_results - len(ids)),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(message["id"] for message in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    emails: list[Email] = []
    for message_id in ids[:max_results]:
        message = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []
        emails.append(
            Email(
                id=message["id"],
                thread_id=message.get("threadId", ""),
                from_=_header(headers, "From"),
                subject=_header(headers, "Subject"),
                date=_header(headers, "Date"),
                snippet=message.get("snippet", ""),
                body_text=_body_text(payload),
            )
        )
    return emails

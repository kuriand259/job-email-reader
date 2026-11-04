"""Local cache of classified job emails for the daily summary."""

import json
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from job_email_reader.models import DayEmail, Email, JobEmailResult

CACHE_PATH = Path("out/emails.jsonl")


def local_day(value: str) -> str:
    raw = value.strip()
    parsed = None
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            parsed = None
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
    if parsed is None:
        return datetime.now().astimezone().date().isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone().date().isoformat()


def _records() -> list[dict]:
    if not CACHE_PATH.exists():
        return []
    rows: list[dict] = []
    for line in CACHE_PATH.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def cache_email(email: Email, result: JobEmailResult) -> None:
    if any(row.get("id") == email.id for row in _records()):
        return
    record = {
        "id": email.id,
        "day": local_day(email.date),
        "from": email.from_,
        "subject": email.subject,
        "body": email.body_text or email.snippet,
        "category": result.category,
        "company": result.company,
        "role": result.role,
        "status": result.status,
        "next_step": result.next_step,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("a") as cache:
        cache.write(json.dumps(record) + "\n")


def _day_email(row: dict) -> DayEmail:
    return DayEmail(
        id=row.get("id", ""),
        day=row.get("day", ""),
        from_=row.get("from", ""),
        subject=row.get("subject", ""),
        body=row.get("body", ""),
        company=row.get("company"),
        role=row.get("role"),
        category=row.get("category", "other"),
        status=row.get("status"),
        next_step=row.get("next_step"),
    )


def load_all() -> list[DayEmail]:
    return [_day_email(row) for row in _records()]


def load_day(day: str) -> list[DayEmail]:
    return [email for email in load_all() if email.day == day]


def clear_cache() -> None:
    CACHE_PATH.unlink(missing_ok=True)


def cached_days() -> list[str]:
    return sorted({str(row["day"]) for row in _records() if row.get("day")})

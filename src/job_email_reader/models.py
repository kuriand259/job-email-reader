"""Shared models."""

from typing import Literal

from pydantic import BaseModel


class Email(BaseModel):
    id: str
    thread_id: str
    from_: str
    subject: str
    date: str
    snippet: str
    body_text: str


Category = Literal[
    "application_ack",
    "rejection",
    "recruiter_contact",
    "assessment",
    "interview",
    "offer",
    "other",
]


class DayEmail(BaseModel):
    id: str = ""
    day: str = ""
    from_: str = ""
    subject: str = ""
    body: str = ""
    company: str | None = None
    role: str | None = None
    category: Category
    status: str | None = None
    next_step: str | None = None


class SummaryParagraph(BaseModel):
    paragraph: str


class DailySummary(BaseModel):
    date: str
    paragraph: str
    total: int
    counts: dict[Category, int]


class JobEmailResult(BaseModel):
    is_job_related: bool
    category: Category
    company: str | None
    role: str | None
    status: Literal[
        "Applied",
        "Rejected",
        "Recruiter contact",
        "Interviewing",
        "Offer",
    ] | None
    next_step: str | None
    confidence: float

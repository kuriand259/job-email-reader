"""Email classification and daily summaries."""

from job_email_reader.llm import LLMProvider
from job_email_reader.models import (
    Category,
    DailySummary,
    DayEmail,
    Email,
    JobEmailResult,
    SummaryParagraph,
)

CATEGORIES: tuple[Category, ...] = (
    "application_ack",
    "rejection",
    "recruiter_contact",
    "assessment",
    "interview",
    "offer",
    "other",
)

SYSTEM = (
    "Classify the single email in the user message. "
    "Return one JSON object, never an array. "
    "Set is_job_related to false for newsletters, promotions, and anything else. "
    "category: application_ack, rejection, recruiter_contact, assessment, interview, offer, or other. "
    "status: Applied, Rejected, Recruiter contact, Interviewing, Offer, or null.\n\n"
    "Examples:\n"
)

EXAMPLES = """\
From: jobs@acme.com | Subject: Thank you for applying — Engineer
{"is_job_related": true, "category": "application_ack", "company": "Acme", "role": "Engineer", "status": "Applied", "next_step": null, "confidence": 0.95}

From: talent@beta.io | Subject: Update on your application
{"is_job_related": true, "category": "rejection", "company": "Beta", "role": null, "status": "Rejected", "next_step": null, "confidence": 0.9}

From: deals@shop.com | Subject: Weekend sale inside
{"is_job_related": false, "category": "other", "company": null, "role": null, "status": null, "next_step": null, "confidence": 0.98}
"""


def classify(email: Email, provider: LLMProvider) -> JobEmailResult:
    user = (
        f"From: {email.from_}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date}\n\n"
        f"{email.body_text or email.snippet}"
    )
    parsed = provider.complete_json(SYSTEM + EXAMPLES, user, JobEmailResult)
    if isinstance(parsed, JobEmailResult):
        return parsed
    return JobEmailResult.model_validate(parsed)


SUMMARY_SYSTEM = (
    "Write the daily job-search summary for the emails in the user message. "
    "The applicant is Kurian Dilip. These emails are his. "
    "Describe what happened to him. Do not describe him as the sender or as another person in the thread. "
    "Use only those emails. Do not invent companies, roles, statuses, or next steps. "
    "Mention each next step that is present. "
    "Return one JSON object, never an array, with a single string field named paragraph."
)


def count_categories(rows: list[DayEmail]) -> dict[Category, int]:
    counts: dict[Category, int] = {category: 0 for category in CATEGORIES}
    for row in rows:
        counts[row.category] += 1
    return counts


def summary_emails(rows: list[DayEmail]) -> str:
    blocks: list[str] = []
    for row in rows:
        blocks.append(f"From: {row.from_}\nSubject: {row.subject}\n\n{row.body}")
    return "\n\n---\n\n".join(blocks)


def summarize_day(rows: list[DayEmail], provider: LLMProvider, day: str) -> DailySummary:
    counts = count_categories(rows)
    if not rows:
        paragraph = "No job emails."
    else:
        listed = "\n".join(
            f"{category}: {counts[category]}" for category in CATEGORIES if counts[category]
        )
        user = f"Date: {day}\nTotal: {len(rows)}\n{listed}\n\n{summary_emails(rows)}"
        parsed = provider.complete_json(SUMMARY_SYSTEM, user, SummaryParagraph)
        if not isinstance(parsed, SummaryParagraph):
            parsed = SummaryParagraph.model_validate(parsed)
        paragraph = parsed.paragraph.strip()
    return DailySummary(date=day, paragraph=paragraph, total=len(rows), counts=counts)

import json
import os
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from job_email_reader.classifier import classify as classify_email
from job_email_reader.classifier import summarize_day
from job_email_reader.email_cache import cache_email, clear_cache, load_all
from job_email_reader.gmail_client import fetch_job_emails, lookback_days
from job_email_reader.llm import get_provider
from job_email_reader.notion_tracker import is_processed, upsert_application, upsert_summary

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main(model: str | None = typer.Option(None, "--model")) -> None:
    """Read and classify job-related emails."""
    if model:
        os.environ["LLM_PROVIDER"] = model


@app.command()
def run() -> None:
    """Placeholder command."""
    typer.echo("not implemented")


@app.command()
def fetch(days: int | None = typer.Option(None, "--days")) -> None:
    """Print recent job-related emails."""
    window = days if days is not None else lookback_days()
    table = Table()
    table.add_column("from")
    table.add_column("subject")
    table.add_column("date")
    for email in fetch_job_emails(days=window):
        table.add_row(email.from_, email.subject, email.date)
    Console().print(table)


@app.command()
def classify(
    days: int | None = typer.Option(None, "--days"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Classify recent emails, sync them to Notion, and save job-related results."""
    window = days if days is not None else lookback_days()
    console = Console()
    console.print(f"Fetching emails from the last {window} days...")
    emails = fetch_job_emails(days=window)
    console.print(f"Classifying {len(emails)} emails...")
    provider = get_provider(model)
    rows: list[dict] = []
    called = False
    for index, email in enumerate(emails, start=1):
        subject = " ".join(email.subject.split())[:70]
        console.print(f"[{index}/{len(emails)}] {subject}")
        if is_processed(email.id):
            console.print("  already synced")
            continue
        if called:
            time.sleep(4)
        called = True
        result = classify_email(email, provider)
        if not result.is_job_related:
            console.print("  skipped")
            continue
        console.print(f"  kept  {result.category}")
        cache_email(email, result)
        page_id = upsert_application(result, email)
        if page_id:
            console.print("  synced")
        else:
            console.print("  no company")
        rows.append(
            {
                "id": email.id,
                "from": email.from_,
                "subject": email.subject,
                "date": email.date,
                **result.model_dump(),
            }
        )

    table = Table()
    table.add_column("company")
    table.add_column("role")
    table.add_column("status")
    table.add_column("category")
    table.add_column("confidence")
    for row in rows:
        table.add_row(
            row["company"] or "",
            row["role"] or "",
            row["status"] or "",
            row["category"],
            f"{row['confidence']:.2f}",
        )
    Console().print(table)

    path = Path("out/results.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n")
    console.print(f"Saved {len(rows)} job emails to {path}")


@app.command()
def summary(model: str | None = typer.Option(None, "--model")) -> None:
    """Summarize cached job emails, save the paragraph, then delete the cache."""
    console = Console()
    rows = load_all()
    if not rows:
        console.print("No cached emails.")
        return
    day = rows[0].day
    result = summarize_day(rows, get_provider(model), day)
    path = Path("out/summaries") / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.paragraph + "\n")
    upsert_summary(day, result.paragraph)
    clear_cache()
    console.print(result.paragraph)
    console.print(f"Cleared email cache. Saved {path} and Notion")


if __name__ == "__main__":
    app()

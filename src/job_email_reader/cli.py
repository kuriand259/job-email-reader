import json
import os
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from job_email_reader.agent.loop import (
    drafted_threads,
    load_reply_emails,
    remember_draft,
    run_agent,
    should_skip,
)
from job_email_reader.classifier import classify as classify_email
from job_email_reader.classifier import summarize_day
from job_email_reader.email_cache import cache_email, clear_cache, load_all, local_day
from job_email_reader.gmail_client import create_draft, fetch_job_emails, get_thread, lookback_days
from job_email_reader.llm import ChatMessage, ToolSpec, get_provider
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
                "thread_id": email.thread_id,
                "day": local_day(email.date),
                "from": email.from_,
                "subject": email.subject,
                "date": email.date,
                "body": email.body_text or email.snippet,
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


@app.command("draft-test")
def draft_test(thread_id: str = typer.Option(..., "--thread-id")) -> None:
    """Create a one-line reply draft on a Gmail thread."""
    messages = get_thread(thread_id)
    if not messages:
        raise typer.BadParameter(f"Thread {thread_id} has no messages")
    last = messages[-1]
    created = create_draft(
        thread_id,
        to=last["from"],
        subject=last["subject"],
        body="Test draft.",
        in_reply_to_message_id=last["message_id"],
    )
    Console().print(f"Draft {created['id']} on thread {created['thread_id']}")


@app.command("tool-smoke")
def tool_smoke(model: str | None = typer.Option(None, "--model")) -> None:
    """Ask the model to call a fake get_time tool."""
    console = Console()
    provider = get_provider(model)
    spec = ToolSpec(
        name="get_time",
        description="Return the current local time.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    messages = [
        ChatMessage(role="system", content="Call get_time. Do not guess the time."),
        ChatMessage(role="user", content="What time is it?"),
    ]
    turn = provider.chat_with_tools(messages, [spec])
    call = next((item for item in turn.tool_calls if item.name == "get_time"), None)
    if call is None:
        console.print(turn.content or "Model did not call get_time.")
        raise typer.Exit(code=1)
    current = datetime.now().astimezone().isoformat(timespec="seconds")
    messages.append(ChatMessage(role="assistant", content=turn.content, tool_calls=turn.tool_calls))
    messages.append(
        ChatMessage(
            role="tool",
            content=json.dumps({"time": current}),
            tool_call_id=call.id,
            name="get_time",
        )
    )
    follow = provider.chat_with_tools(messages, [spec])
    console.print(follow.content or current)


@app.command("draft-replies")
def draft_replies(
    days: int = typer.Option(7, "--days"),
    model: str | None = typer.Option(None, "--model"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    max_steps: int = typer.Option(6, "--max-steps"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    """Draft follow-ups for cached job emails that need a reply."""
    console = Console()
    if days < 1:
        raise typer.BadParameter("days must be at least 1")
    if limit < 1:
        raise typer.BadParameter("limit must be at least 1")
    drafted = drafted_threads()
    emails = load_reply_emails(days)
    if not emails:
        console.print("No emails to draft.")
        return
    provider = None
    sent = 0
    for email in emails:
        if sent >= limit:
            break
        reason = should_skip(email, drafted)
        company = email.company or ""
        if reason:
            console.print(f"{company} / skip / {reason} / draft created: false")
            continue
        if provider is None:
            provider = get_provider(model)
        result = run_agent(email, provider, dry_run=dry_run, max_steps=max_steps)
        if result.draft_created and email.thread_id:
            remember_draft(email.thread_id)
            drafted.add(email.thread_id)
        created = "true" if result.draft_created else "false"
        console.print(
            f"{company} / {result.decision or 'none'} / {result.reason} / draft created: {created}"
        )
        sent += 1


if __name__ == "__main__":
    app()

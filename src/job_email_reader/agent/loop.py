"""Tool loop for follow-up drafts."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

from job_email_reader.agent.tools import AgentDeps, default_deps, execute, tool_specs
from job_email_reader.email_cache import load_all
from job_email_reader.llm import ChatMessage, LLMProvider
from job_email_reader.models import DayEmail

console = Console()
DRAFTED_PATH = Path("out/drafted.json")
RESULTS_PATH = Path("out/results.json")
_SKIP_CATEGORIES = {"application_ack", "rejection"}


class AgentResult(BaseModel):
    decision: str | None = None
    reason: str = ""
    draft_created: bool = False
    company: str | None = None


def _prompt() -> str:
    candidates = [
        Path("prompts/followup_agent.md"),
        Path(__file__).resolve().parents[3] / "prompts" / "followup_agent.md",
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text()
    raise RuntimeError("prompts/followup_agent.md was not found")


def _email_message(email: DayEmail) -> str:
    return (
        f"Company: {email.company or ''}\n"
        f"Role: {email.role or ''}\n"
        f"Category: {email.category}\n"
        f"Status: {email.status or ''}\n"
        f"Next step: {email.next_step or ''}\n"
        f"From: {email.from_}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.day}\n"
        f"Thread ID: {email.thread_id}\n\n"
        f"{email.body}"
    )


def _trace(trace_dir: Path, record: dict) -> None:
    day = datetime.now().astimezone().date().isoformat()
    path = trace_dir / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def run_agent(
    email: DayEmail,
    provider: LLMProvider,
    dry_run: bool = False,
    max_steps: int = 6,
    deps: AgentDeps | None = None,
    trace_dir: Path | None = None,
) -> AgentResult:
    tools = tool_specs()
    deps = deps or default_deps()
    traces = trace_dir or Path("out/agent_traces")
    messages = [
        ChatMessage(role="system", content=_prompt()),
        ChatMessage(role="user", content=_email_message(email)),
    ]
    result = AgentResult(company=email.company)
    for step in range(1, max_steps + 1):
        turn = provider.chat_with_tools(messages, tools)
        messages.append(
            ChatMessage(role="assistant", content=turn.content, tool_calls=turn.tool_calls)
        )
        if not turn.tool_calls:
            messages.append(ChatMessage(role="user", content="Call a tool. When you are done, call finish."))
            continue
        finished = False
        for call in turn.tool_calls:
            payload = execute(call.name, call.arguments, dry_run=dry_run, deps=deps)
            console.print(f"{call.name} {json.dumps(call.arguments, default=str)}")
            try:
                _trace(
                    traces,
                    {
                        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "email_id": email.id,
                        "thread_id": email.thread_id,
                        "step": step,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": payload,
                    },
                )
            except OSError as exc:
                console.print(f"trace write failed: {exc}")
            messages.append(
                ChatMessage(
                    role="tool",
                    content=json.dumps(payload, default=str),
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            if call.name == "create_gmail_draft" and "error" not in payload and not payload.get("dry_run"):
                result.draft_created = True
            if call.name == "finish" and "error" not in payload:
                result.decision = str(payload.get("decision") or "")
                result.reason = str(payload.get("reason") or "")
                finished = True
        if finished:
            return result
    if not result.reason:
        result.reason = "stopped after max steps"
    return result


def drafted_threads() -> set[str]:
    if not DRAFTED_PATH.exists():
        return set()
    data = json.loads(DRAFTED_PATH.read_text())
    if not isinstance(data, list):
        return set()
    return {str(item) for item in data}


def remember_draft(thread_id: str) -> None:
    ids = drafted_threads()
    ids.add(thread_id)
    DRAFTED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRAFTED_PATH.write_text(json.dumps(sorted(ids), indent=2) + "\n")


def _from_result(row: dict) -> DayEmail:
    return DayEmail(
        id=row.get("id", ""),
        thread_id=row.get("thread_id", ""),
        day=row.get("day", ""),
        from_=row.get("from", ""),
        subject=row.get("subject", ""),
        body=row.get("body", ""),
        company=row.get("company"),
        role=row.get("role"),
        category=row.get("category") or "other",
        status=row.get("status"),
        next_step=row.get("next_step"),
    )


def load_reply_emails(days: int) -> list[DayEmail]:
    rows = load_all()
    if not rows and RESULTS_PATH.exists():
        data = json.loads(RESULTS_PATH.read_text())
        if isinstance(data, list):
            rows = [_from_result(item) for item in data if isinstance(item, dict)]
    cutoff = datetime.now().astimezone().date() - timedelta(days=days)
    kept: list[DayEmail] = []
    for row in rows:
        if row.day:
            try:
                if datetime.fromisoformat(row.day).date() < cutoff:
                    continue
            except ValueError:
                pass
        kept.append(row)
    return kept


def should_skip(email: DayEmail, drafted: set[str]) -> str | None:
    if email.category in _SKIP_CATEGORIES:
        return email.category
    if not email.thread_id:
        return "no thread id"
    if email.thread_id in drafted:
        return "draft already created for this thread"
    return None

"""Tools the follow-up agent can call."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from job_email_reader.gmail_client import create_draft, get_thread
from job_email_reader.llm import ToolSpec
from job_email_reader.notion_tracker import lookup_tracker_row

console = Console()

_OBJECT = "object"


def tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="get_thread",
            description="Read one Gmail thread. Each message includes from, to, date, subject, body_text, and message_id (the Message-ID header).",
            parameters={
                "type": _OBJECT,
                "properties": {
                    "thread_id": {"type": "string", "description": "Gmail thread id"},
                },
                "required": ["thread_id"],
            },
        ),
        ToolSpec(
            name="lookup_tracker_row",
            description="Look up the Notion job-application row for a company.",
            parameters={
                "type": _OBJECT,
                "properties": {
                    "company": {"type": "string", "description": "Company name"},
                },
                "required": ["company"],
            },
        ),
        ToolSpec(
            name="create_gmail_draft",
            description=(
                "Create an unsent Gmail reply draft on a thread. "
                "The body is under 120 words, plain English, and ends with 'Regards, Kurian'. "
                "Use [AVAILABILITY] or [CONFIRM] instead of inventing a fact."
            ),
            parameters={
                "type": _OBJECT,
                "properties": {
                    "thread_id": {"type": "string"},
                    "to": {"type": "string", "description": "Reply address, usually the sender"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "in_reply_to_message_id": {
                        "type": "string",
                        "description": "Message-ID header of the message being answered",
                    },
                },
                "required": ["thread_id", "to", "subject", "body", "in_reply_to_message_id"],
            },
        ),
        ToolSpec(
            name="finish",
            description="Stop. decision is draft or skip. reason is one sentence.",
            parameters={
                "type": _OBJECT,
                "properties": {
                    "decision": {"type": "string", "enum": ["draft", "skip"]},
                    "reason": {"type": "string"},
                },
                "required": ["decision", "reason"],
            },
        ),
    ]


@dataclass
class AgentDeps:
    get_thread: Callable[[str], list[dict[str, str]]]
    lookup_tracker_row: Callable[[str], dict[str, Any]]
    create_draft: Callable[..., dict[str, str]]


def default_deps() -> AgentDeps:
    return AgentDeps(
        get_thread=get_thread,
        lookup_tracker_row=lookup_tracker_row,
        create_draft=create_draft,
    )


def _missing(arguments: dict[str, Any], *names: str) -> str | None:
    missing = [name for name in names if not str(arguments.get(name, "")).strip()]
    if not missing:
        return None
    return "Missing " + ", ".join(missing)


def execute(name: str, arguments: dict[str, Any], *, dry_run: bool, deps: AgentDeps) -> dict[str, Any]:
    try:
        if not isinstance(arguments, dict):
            return {"error": "Tool arguments must be an object"}
        if name == "get_thread":
            missing = _missing(arguments, "thread_id")
            if missing:
                return {"error": missing}
            return {"messages": deps.get_thread(str(arguments["thread_id"]))}
        if name == "lookup_tracker_row":
            missing = _missing(arguments, "company")
            if missing:
                return {"error": missing}
            row = deps.lookup_tracker_row(str(arguments["company"]))
            return row if isinstance(row, dict) else {"result": row}
        if name == "create_gmail_draft":
            missing = _missing(arguments, "thread_id", "to", "subject", "body", "in_reply_to_message_id")
            if missing:
                return {"error": missing}
            if dry_run:
                console.print(
                    f"dry run, not creating a draft on thread {arguments['thread_id']}"
                )
                return {"dry_run": True}
            created = deps.create_draft(
                thread_id=str(arguments["thread_id"]),
                to=str(arguments["to"]),
                subject=str(arguments["subject"]),
                body=str(arguments["body"]),
                in_reply_to_message_id=str(arguments["in_reply_to_message_id"]),
            )
            return created if isinstance(created, dict) else {"result": created}
        if name == "finish":
            decision = str(arguments.get("decision", "")).strip()
            reason = str(arguments.get("reason", "")).strip()
            if decision not in {"draft", "skip"}:
                return {"error": "decision must be draft or skip"}
            if not reason:
                return {"error": "reason is required"}
            return {"decision": decision, "reason": reason}
        return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        return {"error": str(exc)}

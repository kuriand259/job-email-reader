"""Follow-up agent loop tests. No network."""

from pathlib import Path

from job_email_reader.agent.loop import run_agent
from job_email_reader.agent.tools import AgentDeps
from job_email_reader.llm import LLMTurn, ToolCall
from job_email_reader.models import DayEmail

ROOT = Path(__file__).resolve().parents[1]


class ScriptedProvider:
    def __init__(self, turns: list[LLMTurn]) -> None:
        self.turns = turns
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: type) -> object:
        raise AssertionError("complete_json")

    def chat_with_tools(self, messages: list, tools: list) -> LLMTurn:
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn


def _email() -> DayEmail:
    return DayEmail(
        id="m1",
        thread_id="t1",
        from_="recruiter@acme.com",
        subject="Interview",
        body="Can you meet tomorrow?",
        company="Acme",
        role="Engineer",
        category="interview",
        status="Interviewing",
    )


def _deps(create) -> AgentDeps:
    def get_thread(thread_id: str) -> list[dict[str, str]]:
        return [
            {
                "from": "recruiter@acme.com",
                "to": "kurian@example.com",
                "date": "Mon, 2 Nov 2026 10:00:00 +0000",
                "subject": "Interview",
                "body_text": "Can you meet tomorrow?",
                "message_id": "<m@acme.com>",
            }
        ]

    return AgentDeps(
        get_thread=get_thread,
        lookup_tracker_row=lambda company: {"found": True, "company": company},
        create_draft=create,
    )


def _draft_call(thread_id: str = "t1") -> ToolCall:
    return ToolCall(
        id="draft",
        name="create_gmail_draft",
        arguments={
            "thread_id": thread_id,
            "to": "recruiter@acme.com",
            "subject": "Interview",
            "body": "Thank you. I can talk.\n\nRegards, Kurian",
            "in_reply_to_message_id": "<m@acme.com>",
        },
    )


def test_skip_does_not_create_a_draft(tmp_path: Path) -> None:
    def create(**kwargs: object) -> dict[str, str]:
        raise AssertionError(kwargs)

    provider = ScriptedProvider(
        [
            LLMTurn(tool_calls=[ToolCall(id="1", name="get_thread", arguments={"thread_id": "t1"})]),
            LLMTurn(
                tool_calls=[
                    ToolCall(id="2", name="finish", arguments={"decision": "skip", "reason": "auto-ack"})
                ]
            ),
        ]
    )
    result = run_agent(
        _email(),
        provider,
        dry_run=False,
        deps=_deps(create),
        trace_dir=tmp_path,
    )
    assert result.decision == "skip"
    assert result.draft_created is False


def test_draft_uses_the_thread_id(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def create(**kwargs: str) -> dict[str, str]:
        seen.append(kwargs)
        return {"id": "d1", "thread_id": kwargs["thread_id"]}

    provider = ScriptedProvider(
        [
            LLMTurn(tool_calls=[ToolCall(id="1", name="get_thread", arguments={"thread_id": "t1"})]),
            LLMTurn(
                tool_calls=[
                    ToolCall(id="2", name="lookup_tracker_row", arguments={"company": "Acme"})
                ]
            ),
            LLMTurn(tool_calls=[_draft_call("t1")]),
            LLMTurn(
                tool_calls=[
                    ToolCall(
                        id="4",
                        name="finish",
                        arguments={"decision": "draft", "reason": "Interview invite"},
                    )
                ]
            ),
        ]
    )
    result = run_agent(
        _email(),
        provider,
        dry_run=False,
        deps=_deps(create),
        trace_dir=tmp_path,
    )
    assert len(seen) == 1
    assert seen[0]["thread_id"] == "t1"
    assert result.draft_created is True
    assert result.decision == "draft"


def test_max_steps_stops_the_loop(tmp_path: Path) -> None:
    def create(**kwargs: object) -> dict[str, str]:
        raise AssertionError(kwargs)

    provider = ScriptedProvider(
        [LLMTurn(tool_calls=[ToolCall(id="1", name="get_thread", arguments={"thread_id": "t1"})])]
    )
    result = run_agent(
        _email(),
        provider,
        dry_run=False,
        max_steps=3,
        deps=_deps(create),
        trace_dir=tmp_path,
    )
    assert provider.calls == 3
    assert result.decision is None
    assert result.draft_created is False


def test_dry_run_does_not_call_gmail(tmp_path: Path) -> None:
    def create(**kwargs: object) -> dict[str, str]:
        raise AssertionError("gmail api")

    provider = ScriptedProvider(
        [
            LLMTurn(tool_calls=[_draft_call("t1")]),
            LLMTurn(
                tool_calls=[
                    ToolCall(id="2", name="finish", arguments={"decision": "draft", "reason": "dry run"})
                ]
            ),
        ]
    )
    result = run_agent(
        _email(),
        provider,
        dry_run=True,
        deps=_deps(create),
        trace_dir=tmp_path,
    )
    assert result.draft_created is False
    traces = list(tmp_path.glob("*.jsonl"))
    assert traces
    assert '"dry_run": true' in traces[0].read_text()


def test_source_never_sends_mail() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        assert "messages().send" not in path.read_text()

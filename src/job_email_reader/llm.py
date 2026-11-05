"""LLM providers."""

import json
import os
import sys
import time
from typing import Any, Literal, Protocol

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from groq import Groq
from pydantic import BaseModel, Field, ValidationError

_GEMINI_RETRY_SECONDS = (8, 16, 32)


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class LLMTurn(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...

    def chat_with_tools(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMTurn: ...


def _api_key(name: str) -> str:
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


class GeminiProvider:
    def __init__(self) -> None:
        self._client = genai.Client(api_key=_api_key("GEMINI_API_KEY"))

    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        response = None
        for attempt in range(len(_GEMINI_RETRY_SECONDS) + 1):
            try:
                response = self._client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=user,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        response_schema=schema,
                    ),
                )
                break
            except errors.APIError as exc:
                retryable = isinstance(exc, errors.ServerError) or exc.code == 429
                if not retryable or attempt == len(_GEMINI_RETRY_SECONDS):
                    raise
                delay = _GEMINI_RETRY_SECONDS[attempt]
                print(
                    f"Gemini busy ({exc.code}). Retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
        assert response is not None
        parsed = response.parsed
        if isinstance(parsed, schema):
            return parsed
        if isinstance(parsed, dict):
            return schema.model_validate(parsed)
        return schema.model_validate_json(response.text or "")

    def chat_with_tools(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMTurn:
        system, contents = _gemini_contents(messages)
        response = self._client.models.generate_content(
            model="gemini-3.8-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system or None,
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=tool.name,
                                description=tool.description,
                                parameters_json_schema=tool.parameters,
                            )
                            for tool in tools
                        ]
                    )
                ],
            ),
        )
        return _gemini_turn(response)


def _one_object(content: str) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return json.dumps(data[0])
    return content


class GroqProvider:
    def __init__(self) -> None:
        self._client = Groq(api_key=_api_key("GROQ_API_KEY"))

    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel:
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"{system}\n\nRespond with one JSON object, not an array, matching this schema:\n"
                    f"{json.dumps(schema.model_json_schema())}"
                ),
            },
            {"role": "user", "content": user},
        ]
        last_error: ValidationError | None = None
        for attempt in range(2):
            completion = self._client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                response_format={"type": "json_object"},
                reasoning_effort="low",
            )
            content = completion.choices[0].message.content or ""
            try:
                return schema.model_validate_json(_one_object(content))
            except ValidationError as exc:
                last_error = exc
                if attempt:
                    break
                if content:
                    messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"Return one JSON object, not an array.\n{exc}",
                    }
                )
        assert last_error is not None
        raise last_error

    def chat_with_tools(self, messages: list[ChatMessage], tools: list[ToolSpec]) -> LLMTurn:
        completion = self._client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=_groq_messages(messages),
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ],
            reasoning_effort="low",
        )
        message = completion.choices[0].message
        calls: list[ToolCall] = []
        for index, call in enumerate(message.tool_calls or []):
            calls.append(
                ToolCall(
                    id=call.id or f"call_{index}",
                    name=call.function.name,
                    arguments=_object(call.function.arguments),
                )
            )
        return LLMTurn(content=message.content or "", tool_calls=calls)


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(value, dict):
        try:
            return json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            return {}
    if value is None:
        return {}
    try:
        mapped = dict(value)
    except (TypeError, ValueError):
        return {}
    try:
        parsed = json.loads(json.dumps(mapped, default=str))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _gemini_contents(messages: list[ChatMessage]) -> tuple[str, list[types.Content]]:
    system = "\n\n".join(message.content for message in messages if message.role == "system")
    contents: list[types.Content] = []
    pending: list[types.Part] = []

    def flush_tools() -> None:
        if not pending:
            return
        contents.append(types.Content(role="user", parts=list(pending)))
        pending.clear()

    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            payload = _object(message.content)
            if not payload:
                payload = {"result": message.content}
            pending.append(
                types.Part.from_function_response(name=message.name or "", response=payload)
            )
            continue
        flush_tools()
        if message.role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part.from_text(text=message.content or " ")])
            )
            continue
        parts: list[types.Part] = []
        if message.content:
            parts.append(types.Part.from_text(text=message.content))
        for call in message.tool_calls:
            parts.append(types.Part.from_function_call(name=call.name, args=call.arguments))
        if parts:
            contents.append(types.Content(role="model", parts=parts))
    flush_tools()
    return system, contents


def _gemini_turn(response: Any) -> LLMTurn:
    candidates = getattr(response, "candidates", None) or []
    if not candidates or not getattr(candidates[0], "content", None):
        return LLMTurn()
    texts: list[str] = []
    calls: list[ToolCall] = []
    for index, part in enumerate(candidates[0].content.parts or []):
        function_call = getattr(part, "function_call", None)
        if function_call and getattr(function_call, "name", None):
            calls.append(
                ToolCall(
                    id=getattr(function_call, "id", None) or f"call_{index}",
                    name=function_call.name,
                    arguments=_object(getattr(function_call, "args", None) or {}),
                )
            )
            continue
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
    return LLMTurn(content="".join(texts), tool_calls=calls)


def _groq_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            converted.append({"role": "system", "content": message.content})
        elif message.role == "user":
            converted.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            content: str | None = message.content or None
            item: dict[str, Any] = {"role": "assistant", "content": content}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
        elif message.role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id or "",
                    "content": message.content,
                }
            )
    return converted


def get_provider(model: str | None = None) -> LLMProvider:
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    name = (model or os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    if name == "gemini":
        return GeminiProvider()
    if name == "groq":
        return GroqProvider()
    raise ValueError(f"Unknown LLM provider: {name}")

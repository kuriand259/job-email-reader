"""LLM providers."""

import json
import os
import sys
import time
from typing import Protocol

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from groq import Groq
from pydantic import BaseModel, ValidationError

_GEMINI_RETRY_SECONDS = (8, 16, 32)


class LLMProvider(Protocol):
    def complete_json(self, system: str, user: str, schema: type[BaseModel]) -> BaseModel: ...


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


def get_provider(model: str | None = None) -> LLMProvider:
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    name = (model or os.getenv("LLM_PROVIDER") or "gemini").strip().lower()
    if name == "gemini":
        return GeminiProvider()
    if name == "groq":
        return GroqProvider()
    raise ValueError(f"Unknown LLM provider: {name}")

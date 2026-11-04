"""Notion job application tracker."""

import json
import os
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from notion_client import Client

from job_email_reader.models import Email, JobEmailResult

PROCESSED_PATH = Path("out/processed.json")
ENV_PATH = Path(".env")
_STATUS_ORDER = ("Applied", "Recruiter contact", "Interviewing", "Offer")
_STATUS_OPTIONS = (*_STATUS_ORDER, "Rejected")
_NOTE_LIMIT = 2000
_APPLICATION_DB = "Job applications"
_SUMMARY_DB = "Daily summaries"

_APPLICATION_SCHEMA: dict[str, Any] = {
    "Company": {"title": {}},
    "Role": {"rich_text": {}},
    "Status": {"select": {"options": [{"name": name} for name in _STATUS_OPTIONS]}},
    "Last update": {"date": {}},
    "Next step": {"rich_text": {}},
    "Thread ID": {"rich_text": {}},
}
_SUMMARY_SCHEMA: dict[str, Any] = {
    "Date": {"title": {}},
    "Summary": {"rich_text": {}},
}

_source_cache: dict[str, tuple[str, dict[str, Any]]] = {}


def _optional_env(name: str) -> str:
    load_dotenv(os.path.join(os.getcwd(), ".env"))
    return os.getenv(name, "").strip()


def _env(name: str) -> str:
    value = _optional_env(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def _notion_id(value: str) -> str:
    token = value.strip().split("?")[0].rstrip("/").split("/")[-1].replace("-", "")
    if len(token) >= 32 and all(char in "0123456789abcdefABCDEF" for char in token[-32:]):
        return token[-32:]
    return value.strip()


def _save_env(name: str, value: str) -> None:
    os.environ[name] = value
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    found = False
    updated: list[str] = []
    for line in lines:
        key, sep, _current = line.partition("=")
        if sep and key.strip() == name:
            updated.append(f"{name}={value}")
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(updated) + "\n")


def _client() -> Client:
    return Client(auth=_env("NOTION_TOKEN"))


def _child_databases(client: Client, page_id: str) -> dict[str, str]:
    found: dict[str, str] = {}
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        response = client.blocks.children.list(**kwargs)
        for block in response.get("results") or []:
            if block.get("type") != "child_database":
                continue
            title = ((block.get("child_database") or {}).get("title") or "").strip()
            if title:
                found[title.casefold()] = block["id"]
        if not response.get("has_more"):
            return found
        cursor = response.get("next_cursor")
        if not cursor:
            return found


def _ensure_page() -> str:
    current = _optional_env("NOTION_PAGE_ID")
    if not current:
        raise RuntimeError(
            "NOTION_PAGE_ID is not set. "
            "Create a Notion page, share it with the integration, "
            "and set NOTION_PAGE_ID in .env to that page's id."
        )
    return _notion_id(current)


def _create_database(client: Client, page_id: str, title: str, schema: dict[str, Any]) -> str:
    created = client.databases.create(
        parent={"type": "page_id", "page_id": page_id},
        title=[{"type": "text", "text": {"content": title}}],
        is_inline=True,
        initial_data_source={"properties": schema},
    )
    database_id = created.get("id")
    if not database_id:
        raise RuntimeError(f"Notion did not return an id for {title}")
    print(f"Created Notion database {title}.", file=sys.stderr)
    return database_id


def _ensure_database(
    client: Client,
    *,
    env_name: str,
    title: str,
    schema: dict[str, Any],
) -> str:
    current = _optional_env(env_name)
    if current:
        return _notion_id(current)
    page_id = _ensure_page()
    database_id = _child_databases(client, page_id).get(title.casefold())
    if database_id is None:
        database_id = _create_database(client, page_id, title, schema)
    _save_env(env_name, database_id)
    print(f"Saved {env_name} to .env.", file=sys.stderr)
    return _notion_id(database_id)


def _ensure_columns(
    client: Client,
    source_id: str,
    properties: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    names = {name.casefold() for name in properties}
    title_name = next(
        (name for name, prop in properties.items() if prop.get("type") == "title"),
        None,
    )
    wanted_title = next(name for name, spec in schema.items() if "title" in spec)
    if title_name is None:
        updates[wanted_title] = {"title": {}}
    elif title_name.casefold() != wanted_title.casefold() and wanted_title.casefold() not in names:
        updates[title_name] = {"name": wanted_title}
    for name, spec in schema.items():
        if "title" in spec or name.casefold() in names:
            continue
        updates[name] = spec
    if not updates:
        return properties
    client.data_sources.update(data_source_id=source_id, properties=updates)
    source = client.data_sources.retrieve(data_source_id=source_id)
    return source.get("properties") or properties


def _load_source(
    client: Client, database_id: str, schema: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    cached = _source_cache.get(database_id)
    if cached is not None:
        return cached
    database = client.databases.retrieve(database_id=database_id)
    sources = database.get("data_sources") or []
    if not sources:
        raise RuntimeError("Notion database has no data source")
    source_id = sources[0]["id"]
    source = client.data_sources.retrieve(data_source_id=source_id)
    properties = _ensure_columns(client, source_id, source.get("properties") or {}, schema)
    _source_cache[database_id] = (source_id, properties)
    return source_id, properties


def _summary_source(client: Client) -> tuple[str, dict[str, Any]]:
    database_id = _ensure_database(
        client,
        env_name="NOTION_SUMMARY_DB_ID",
        title=_SUMMARY_DB,
        schema=_SUMMARY_SCHEMA,
    )
    return _load_source(client, database_id, _SUMMARY_SCHEMA)


def _source(client: Client) -> tuple[str, dict[str, Any]]:
    database_id = _ensure_database(
        client,
        env_name="NOTION_DB_ID",
        title=_APPLICATION_DB,
        schema=_APPLICATION_SCHEMA,
    )
    _summary_source(client)
    return _load_source(client, database_id, _APPLICATION_SCHEMA)


def _field(properties: dict[str, Any], name: str) -> tuple[str, str]:
    for key, value in properties.items():
        if key.casefold() == name.casefold():
            return key, value.get("type", "")
    found = ", ".join(properties) or "(none)"
    raise RuntimeError(f"Notion database is missing a {name} property. Found: {found}")


def _plain(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    kind = prop.get("type")
    if kind in {"title", "rich_text"}:
        return "".join(piece.get("plain_text", "") for piece in prop.get(kind) or [])
    if kind in {"select", "status"}:
        chosen = prop.get(kind) or {}
        return chosen.get("name", "") if isinstance(chosen, dict) else ""
    if kind == "date":
        date = prop.get("date") or {}
        return date.get("start") or ""
    return ""


def _same(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _equals_filter(name: str, prop_type: str, value: str) -> dict[str, Any] | None:
    if prop_type in {"title", "rich_text"}:
        return {"property": name, prop_type: {"equals": value}}
    return None


def _query_filter(
    company_name: str,
    company_type: str,
    company: str,
    role_name: str | None,
    role_type: str | None,
    role: str | None,
) -> dict[str, Any] | None:
    parts: list[dict[str, Any]] = []
    company_filter = _equals_filter(company_name, company_type, company)
    if company_filter is not None:
        parts.append(company_filter)
    if role and role_name and role_type:
        role_filter = _equals_filter(role_name, role_type, role)
        if role_filter is not None:
            parts.append(role_filter)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"and": parts}


def _text_chunks(text: str) -> list[dict[str, Any]]:
    content = text or ""
    if not content:
        return [{"type": "text", "text": {"content": ""}}]
    chunks = [
        {"type": "text", "text": {"content": content[index : index + _NOTE_LIMIT]}}
        for index in range(0, len(content), _NOTE_LIMIT)
    ]
    if len(chunks) > 100:
        raise RuntimeError("Text is longer than Notion allows in one property")
    return chunks


def _notion_value(prop_type: str, text: str) -> dict[str, Any]:
    content = text[:_NOTE_LIMIT]
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": content}}]}
    if prop_type == "rich_text":
        return {"rich_text": _text_chunks(text)}
    if prop_type == "select":
        return {"select": {"name": text}}
    if prop_type == "status":
        return {"status": {"name": text}}
    if prop_type == "date":
        return {"date": {"start": text}}
    raise RuntimeError(f"Unsupported Notion property type: {prop_type}")


def _fill_title(properties: dict[str, Any], payload: dict[str, Any], company: str) -> None:
    for name, prop in properties.items():
        if prop.get("type") == "title":
            payload.setdefault(name, _notion_value("title", company))
            return


def _canonical_status(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip().casefold()
    if not key:
        return None
    if key == "rejected":
        return "Rejected"
    for status in _STATUS_ORDER:
        if status.casefold() == key:
            return status
    return None


def _choose_status(current: str | None, incoming: str | None) -> str | None:
    current_status = _canonical_status(current)
    incoming_status = _canonical_status(incoming)
    if incoming_status == "Rejected" or current_status == "Rejected":
        return "Rejected"
    if incoming_status is None:
        return current_status
    if current_status is None:
        return incoming_status
    if _STATUS_ORDER.index(incoming_status) > _STATUS_ORDER.index(current_status):
        return incoming_status
    return current_status


def _iso_date(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    parsed = None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.date().isoformat()


def _load_processed() -> set[str]:
    if not PROCESSED_PATH.exists():
        return set()
    data = json.loads(PROCESSED_PATH.read_text())
    if not isinstance(data, list):
        raise RuntimeError("out/processed.json must be a list of Gmail message ids")
    return {str(item) for item in data}


def is_processed(message_id: str) -> bool:
    return message_id in _load_processed()


def _mark_processed(message_id: str) -> None:
    ids = _load_processed()
    ids.add(message_id)
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_PATH.write_text(json.dumps(sorted(ids), indent=2) + "\n")


def _find_page(client: Client, company: str, role: str | None) -> dict[str, Any] | None:
    company = company.strip()
    if not company:
        return None
    role_text = role.strip() if role and role.strip() else None
    source_id, properties = _source(client)
    company_name, company_type = _field(properties, "Company")
    role_name = None
    role_type = None
    if role_text is not None:
        role_name, role_type = _field(properties, "Role")
    query_filter = _query_filter(
        company_name, company_type, company, role_name, role_type, role_text
    )
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"data_source_id": source_id, "page_size": 100}
        if query_filter is not None:
            kwargs["filter"] = query_filter
        if cursor:
            kwargs["start_cursor"] = cursor
        response = client.data_sources.query(**kwargs)
        for page in response.get("results") or []:
            props = page.get("properties") or {}
            if not _same(_plain(props.get(company_name)), company):
                continue
            if role_text is not None and not _same(_plain(props.get(role_name)), role_text):
                continue
            return page
        if not response.get("has_more"):
            return None
        cursor = response.get("next_cursor")
        if not cursor:
            return None


def _find_titled(
    client: Client,
    source_id: str,
    properties: dict[str, Any],
    field_name: str,
    value: str,
) -> dict[str, Any] | None:
    name, prop_type = _field(properties, field_name)
    query_filter = _equals_filter(name, prop_type, value)
    cursor = None
    while True:
        kwargs: dict[str, Any] = {"data_source_id": source_id, "page_size": 100}
        if query_filter is not None:
            kwargs["filter"] = query_filter
        if cursor:
            kwargs["start_cursor"] = cursor
        response = client.data_sources.query(**kwargs)
        for page in response.get("results") or []:
            props = page.get("properties") or {}
            if _same(_plain(props.get(name)), value):
                return page
        if not response.get("has_more"):
            return None
        cursor = response.get("next_cursor")
        if not cursor:
            return None


def find_application(company: str, role: str | None = None) -> str | None:
    page = _find_page(_client(), company, role)
    if page is None:
        return None
    return page["id"]


def _properties(
    properties: dict[str, Any],
    *,
    company: str | None,
    role: str | None,
    status: str | None,
    when: str | None,
    next_step: str | None,
    thread_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if company:
        name, prop_type = _field(properties, "Company")
        payload[name] = _notion_value(prop_type, company)
        _fill_title(properties, payload, company)
    if role:
        name, prop_type = _field(properties, "Role")
        payload[name] = _notion_value(prop_type, role)
    if status:
        name, prop_type = _field(properties, "Status")
        payload[name] = _notion_value(prop_type, status)
    if when:
        name, prop_type = _field(properties, "Last update")
        payload[name] = _notion_value(prop_type, when)
    if next_step:
        name, prop_type = _field(properties, "Next step")
        payload[name] = _notion_value(prop_type, next_step)
    if thread_id:
        name, prop_type = _field(properties, "Thread ID")
        payload[name] = _notion_value(prop_type, thread_id)
    return payload


def _note(email: Email, result: JobEmailResult) -> str:
    date = _iso_date(email.date) or " ".join(email.date.split())
    subject = " ".join(email.subject.split())
    step = result.next_step or ""
    return f"{date} · {result.category} · {subject} · {step}"[:_NOTE_LIMIT]


def _append_note(client: Client, page_id: str, text: str) -> None:
    client.blocks.children.append(
        block_id=page_id,
        children=[
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": text}}]
                },
            }
        ],
    )


def upsert_application(result: JobEmailResult, email: Email) -> str | None:
    if is_processed(email.id):
        return None
    company = (result.company or "").strip()
    if not company:
        return None
    client = _client()
    source_id, properties = _source(client)
    existing = _find_page(client, company, result.role)
    when = _iso_date(email.date)
    role = result.role.strip() if result.role and result.role.strip() else None
    if existing is None:
        status = _choose_status(None, result.status)
        created = client.pages.create(
            parent={"type": "data_source_id", "data_source_id": source_id},
            properties=_properties(
                properties,
                company=company,
                role=role,
                status=status,
                when=when,
                next_step=result.next_step,
                thread_id=email.thread_id,
            ),
        )
        page_id = created["id"]
    else:
        page_id = existing["id"]
        status_name, _status_type = _field(properties, "Status")
        current = _plain((existing.get("properties") or {}).get(status_name))
        status = _choose_status(current or None, result.status)
        payload = _properties(
            properties,
            company=None,
            role=role,
            status=status,
            when=when,
            next_step=result.next_step,
            thread_id=email.thread_id,
        )
        if payload:
            client.pages.update(page_id=page_id, properties=payload)
    _append_note(client, page_id, _note(email, result))
    _mark_processed(email.id)
    return page_id


def upsert_summary(day: str, paragraph: str) -> str:
    client = _client()
    source_id, properties = _summary_source(client)
    date_name, date_type = _field(properties, "Date")
    summary_name, summary_type = _field(properties, "Summary")
    payload = {
        date_name: _notion_value(date_type, day),
        summary_name: _notion_value(summary_type, paragraph),
    }
    existing = _find_titled(client, source_id, properties, "Date", day)
    if existing is None:
        created = client.pages.create(
            parent={"type": "data_source_id", "data_source_id": source_id},
            properties=payload,
        )
        return created["id"]
    page_id = existing["id"]
    client.pages.update(page_id=page_id, properties=payload)
    return page_id

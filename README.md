# Job email reader

Reads recent Gmail messages, classifies the job-related ones, and records them in Notion. A second command writes a one-day summary from the emails just classified.

Copy `.env.example` to `.env` and fill in the keys. Gmail also needs a Google OAuth client file named `credentials.json` in the project directory. The first Gmail call opens a browser and then saves `token.json`.

## Notion

Set `NOTION_PAGE_ID` yourself. An internal Notion integration cannot create a page at the workspace root: Notion rejects that call because the integration is not owned by a single user. If `NOTION_PAGE_ID` is empty, `classify` stops with that reason and does not call Notion.

Create a page, share it with the integration, and put the page id from its link in `.env`. Leave off the `?v=` view id.

```env
NOTION_PAGE_ID=a1b2c3d4e5f6789012345678abcdef01
```

After that, the rest of the Notion setup is automatic. The first `classify` creates two databases on that page, **Job applications** and **Daily summaries**, adds any missing columns, and writes `NOTION_DB_ID` and `NOTION_SUMMARY_DB_ID` back to `.env`. Later runs reuse those ids.

## What a daily run does

`LOOKBACK_DAYS` defaults to `1`. `MAX_EMAILS` defaults to `50`. `--days` overrides the lookback for that one command.

`classify` fetches mail, classifies each new message, appends kept emails to `out/emails.jsonl`, and updates the company database. Messages already listed in `out/processed.json` are skipped.

`summary` reads that cache, writes `out/summaries/<date>.md`, and upserts that paragraph into the **Daily summaries** database (one row per date). It deletes `out/emails.jsonl` only after both saves succeed. Run it after `classify`. If the model call or the Notion write fails, the cache is left in place.

```bash
python -m job_email_reader.cli classify --model groq
python -m job_email_reader.cli summary --model groq
```

## Commands

`fetch` only prints matching mail. It does not call a model or Notion.

```bash
python -m job_email_reader.cli fetch
python -m job_email_reader.cli fetch --days 7
```

`classify` takes the same `--days` override. `--model` chooses `gemini` or `groq` for that command. You can also set it once in front of the command, which sets `LLM_PROVIDER` for the process.

```bash
python -m job_email_reader.cli classify
python -m job_email_reader.cli classify --days 7
python -m job_email_reader.cli classify --model groq
python -m job_email_reader.cli classify --days 7 --model gemini
python -m job_email_reader.cli --model groq classify
```

`summary` uses the cached emails from the last `classify`. It accepts `--model` the same way and does not take `--days`.

```bash
python -m job_email_reader.cli summary
python -m job_email_reader.cli summary --model groq
python -m job_email_reader.cli --model gemini summary
```

With the package installed, the same commands are available as `job-email-reader` instead of `python -m job_email_reader.cli`.

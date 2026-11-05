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

## GitHub cron

GitHub Actions can run that pair every day while this computer is off. Put the workflow in `.github/workflows/daily.yml`. The cron is `45 2 * * *`, which is 02:45 UTC and 8:15 IST. The schedule can start late. **Run workflow** on the Actions tab starts it by hand.

Each run is a blank machine, so the Gmail login and `.env` are stored as secrets rather than committed. From the project directory:

```bash
gh secret set ENV_FILE < .env
gh secret set GMAIL_TOKEN < token.json
gh secret set GMAIL_CREDENTIALS < credentials.json
```

`ENV_FILE` is the whole `.env`: the API keys, `NOTION_PAGE_ID`, `LOOKBACK_DAYS`, and `MAX_EMAILS`. The workflow writes that secret to `.env` in the job directory before `classify`. A change on this computer does not reach GitHub until you run `gh secret set ENV_FILE < .env` again.

`GMAIL_TOKEN` is the contents of `token.json`, and `GMAIL_CREDENTIALS` is the contents of `credentials.json`. The workflow writes both files back before `classify`. The refresh token inside `token.json` keeps the login alive, so the browser step does not run again. When the access token expires, that run refreshes it. The next day starts again from the same secret.

The names are listed under **Settings → Secrets and variables → Actions**. GitHub does not show the saved values. Use **Update** there, or the `gh secret set` commands above, to replace one.

`out/processed.json` is the list of Gmail message ids already written to Notion. It is not committed. The workflow restores one Actions cache entry named `processed-ids` into `out/processed.json`, runs `classify`, deletes that cache entry, and saves the updated file under the same name. `summary` runs after the save. The file stays small: it is only those ids, and a year of mail is still a few hundred kilobytes.

The cache starts empty. The first GitHub run does not see the `out/processed.json` already on your computer, so mail from the last day can get a second Notion note. Later runs skip the ids saved in the cache. GitHub drops a cache that nobody reads for 7 days. A daily run keeps it.

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

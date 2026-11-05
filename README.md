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

## Scheduled run

GitHub Actions runs that pair while this computer is off. The workflow is `.github/workflows/daily.yml`.

GitHub's own `schedule` cron is not reliable. It queues the run and can start it late, and under load it can skip the day without recording a failure. The morning job is started with `workflow_dispatch` instead. [cron-job.org](https://cron-job.org) sends that request at 8:15 Asia/Kolkata. A `204` response means GitHub accepted it. The Actions run, not the cron test, writes to Notion.

```text
POST https://api.github.com/repos/kuriand259/job-email-reader/actions/workflows/daily.yml/dispatches
```

```text
Accept: application/vnd.github+json
Authorization: Bearer <token>
User-Agent: job-email-reader
Content-Type: application/json
```

```json
{"ref":"master"}
```

The token is a fine-grained personal access token for **kuriand259**, limited to this repository, with **Actions** set to **Read and write**. It stays in cron-job.org. The same trigger from this computer is `gh workflow run daily.yml --ref master`, and **Run workflow** on the Actions tab does it by hand. The file still has a `45 2 * * *` schedule, 8:15 IST, as a backup.

Each run is a blank machine, so the Gmail login and `.env` are stored as secrets rather than committed. From the project directory:

```bash
gh secret set ENV_FILE < .env
gh secret set GMAIL_TOKEN < token.json
gh secret set GMAIL_CREDENTIALS < credentials.json
```

`ENV_FILE` is the whole `.env`: the API keys, `NOTION_PAGE_ID`, `LOOKBACK_DAYS`, and `MAX_EMAILS`. The workflow writes that secret to `.env` in the job directory before `classify`. A change on this computer does not reach GitHub until you run `gh secret set ENV_FILE < .env` again.

`GMAIL_TOKEN` is the contents of `token.json`, and `GMAIL_CREDENTIALS` is the contents of `credentials.json`. The workflow writes both files back before `classify`. When the access token expires, that run refreshes it from the refresh token in the secret. The next day starts again from the same secret. `credentials.json` does not need a refresh.

The Cloud app stays in **Testing**. Publishing it to production requires a public homepage and a privacy policy on a domain you own, and this project has neither. In Testing, Google expires the refresh token 7 days after the browser sign-in. The daily job then fails. Sign in again from the project directory and replace the secret:

```bash
rm -f token.json
python -m job_email_reader.cli fetch
gh secret set GMAIL_TOKEN < token.json
```

Remove `token.json` first. A dead refresh token makes the client try to refresh and exit, so the browser does not open while that file is still there. A Google password change, or removing the app on the Google Account security page, ends the token before the week is up.

IMAP with a Gmail app password would skip this weekly sign-in. That path was not used. The password would live in the `ENV_FILE` secret, and it can read, send, and permanently delete mail. The OAuth token has `gmail.readonly` and `gmail.compose`. Compose can create drafts and send, and it cannot permanently delete mail. Google also hides app passwords unless 2-Step Verification is on, and they are unavailable with Advanced Protection or with 2-Step Verification that uses only security keys.

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

## Follow-up agent

The Gmail login asks for `gmail.readonly` and `gmail.compose`. If `token.json` does not include `gmail.compose`, the next Gmail call deletes it and opens a browser. Sign in once from this computer and update `GMAIL_TOKEN` before the next Actions run. A runner cannot open that browser, so the job stops until the secret is replaced.

`draft-replies` reads the classified job emails and may create a Gmail draft. It uses the cache from `classify` when that file is still there. `summary` deletes the cache, so a later run, including the Actions step, reads `out/results.json` from that `classify` instead. Application acknowledgements and rejections are skipped before the model runs. A thread listed in `out/drafted.json` is skipped.

The model can call four tools:

- `get_thread` reads the Gmail thread. Each message has from, to, date, subject, a body of at most 3000 characters, and the Message-ID header.
- `lookup_tracker_row` reads the Notion row for the company.
- `create_gmail_draft` creates an unsent reply with `Re:`, `In-Reply-To`, `References`, and the Gmail thread id. It calls `users.drafts.create` only.
- `finish` records `draft` or `skip` and a one-sentence reason.

The loop loads `prompts/followup_agent.md`, then calls the model until `finish` or `--max-steps` (default 6). Each tool call is printed and appended to `out/agent_traces/<date>.jsonl`. A tool failure comes back as `{"error": "..."}` and the loop keeps going. Gemini and Groq both go through `chat_with_tools`.

Drafts stay under 120 words, use plain English, and end with `Regards, Kurian`. Missing facts are written as `[AVAILABILITY]` or `[CONFIRM]`. The prompt answers interview invites, recruiter questions, assessment scheduling, and requests for availability or documents. It skips auto-acknowledgements, rejections, newsletters, and no-reply mail.

Nothing in this path sends mail. `--dry-run` logs `create_gmail_draft` and returns `{"dry_run": true}` without calling Gmail. The daily workflow runs `draft-replies --model groq` only when the **Run workflow** input `draft_replies` is checked, or the repository variable `DRAFT_REPLIES` is `true`. The morning schedule does neither unless that variable is set.

```bash
python -m job_email_reader.cli draft-replies --days 7 --model gemini --dry-run --max-steps 6 --limit 10
python -m job_email_reader.cli draft-test --thread-id THREAD_ID
python -m job_email_reader.cli tool-smoke --model groq
```

A dry run appends lines like these:

```json
{"tool": "get_thread", "arguments": {"thread_id": "18c2"}, "result": {"messages": [{"from": "recruiter@acme.com", "subject": "Interview", "message_id": "<m@acme.com>"}]}}
{"tool": "lookup_tracker_row", "arguments": {"company": "Acme"}, "result": {"found": true, "status": "Interviewing", "role": "Engineer"}}
{"tool": "create_gmail_draft", "arguments": {"thread_id": "18c2", "to": "recruiter@acme.com"}, "result": {"dry_run": true}}
{"tool": "finish", "arguments": {"decision": "draft", "reason": "They asked for a time."}, "result": {"decision": "draft", "reason": "They asked for a time."}}
```

The printed line is the company, the decision, the reason, and whether a draft was created: `Acme / draft / They asked for a time. / draft created: false`.

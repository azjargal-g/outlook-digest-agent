# Outlook Digest Agent

A daily AI digest of your Outlook mail. Two small agents work together:

- a **worker agent** (Claude Haiku) reads each new email and writes a 2–3 sentence summary;
- a **manager agent** (Claude Sonnet) reads those summaries plus your plain-English preferences and picks
  what actually deserves your attention: **act on** (deadlines, requests) and **worth a look**.

You get the result as a Windows notification and as an email to yourself, once a day.

It reads **Outlook on the web through a real browser** (Playwright), using your own signed-in session.
No Microsoft Graph app registration or admin consent is needed, which matters if your organization
blocks third-party mail apps.

```
Outlook on the web ──(Playwright, your session)──▶ worker agent ──▶ summaries ──▶ manager agent ──▶ digest
   folders you choose        exclude list,             Haiku, one        SQLite        Sonnet + your       toast +
                             redaction                 call per email                  preferences.md      email to self
```

## Safety model

- **The models have no tools.** Each agent is a single API request with no tools attached. The models can
  only return text (worker) or schema-validated JSON (manager). They can't click, send, move, or delete anything.
- **Email content is treated as data.** Prompts tell the models never to follow instructions found in emails.
  The manager sees only summaries, never raw mail.
- **Privacy filters run before the model.** Emails whose sender, subject, or preview match your `exclude` list
  are never opened. SSN- and card-number patterns are redacted from the rest.
- **Sending is plain code, and only to you.** The digest email's recipient is always the signed-in address.
  Before Send is clicked, code checks that there is exactly one recipient and that it resolves to you.
- **Everything else stays local.** The SQLite database, reports, logs, and browser session live in git-ignored folders.

> **Check your organization's policy** before sending work or school email to an AI service. If your
> organization offers an approved Anthropic-compatible gateway, point `LLM_BASE_URL` at it.

## Requirements

- Windows 10/11. Scheduling and notifications use Task Scheduler and Windows toasts. The agents themselves
  are cross-platform (see [Other platforms](#other-platforms)).
- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- An Outlook-on-the-web mailbox. This was developed with a Microsoft 365 work/school account;
  personal outlook.com accounts are untested.
- An [Anthropic API key](https://console.anthropic.com), or a key for an Anthropic-compatible gateway

## Setup

```powershell
git clone https://github.com/<you>/outlook-digest-agent
cd outlook-digest-agent
uv sync
uv run playwright install chromium

Copy-Item .env.example .env                                     # add LLM_API_KEY
Copy-Item config\email_worker.example.toml config\email_worker.toml
Copy-Item config\preferences.example.md config\preferences.md   # describe what you care about
```

1. **Sign in once.** A browser window opens; sign in, including MFA. The session is saved in `.cache/owa_profile`.
   ```powershell
   uv run tests/outlook_browser/extract_emails.py login
   ```
2. **Pick folders.** List your folder names, then put the ones you want into `folders` in `config/email_worker.toml`.
   ```powershell
   uv run tests/outlook_browser/extract_emails.py folders
   ```
3. **Write your preferences** in `config/preferences.md`. This is the only thing steering the manager, so be concrete.
4. **Dry run.** This shows which emails a run would process. It opens nothing and makes no model calls.
   ```powershell
   uv run python -m swarm.agents.email_worker --batch --dry-run
   ```
5. **First real run.** Summarizes, builds the digest, notifies you, and emails it to you.
   ```powershell
   uv run python -m swarm.pipelines.daily_digest
   ```
6. **Schedule it.**
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_schedule.ps1
   ```

**Tip:** in Outlook on the web, turn on Settings → Mail → Message handling → *Don't automatically mark items as read*.
Otherwise every email the worker opens is marked read.

## Configuration

| File | What it holds |
|---|---|
| `.env` | `LLM_API_KEY`; optional `LLM_BASE_URL`, `LLM_AUTH`; model IDs (`SUMMARIZE_MODEL`, `MANAGER_MODEL`) |
| `config/email_worker.toml` | folders, `exclude` privacy list, first-run lookback, `[schedule] daily_at`, `[digest]` delivery toggles |
| `config/preferences.md` | your interests in plain English: *always show*, *worth a look*, *leave out* |

All three are git-ignored. The repo ships only the `*.example` versions.

## Commands

| Command | Does |
|---|---|
| `uv run python -m swarm.pipelines.daily_digest` | full run: worker → manager → notification + email |
| `... daily_digest --skip-batch` | build or resend a digest from summaries already collected |
| `uv run python -m swarm.agents.email_worker --batch [--dry-run]` | worker only, for all configured folders |
| `... email_worker --batch --since "2026-01-31 08:00"` | backfill from a given time |
| `... email_worker --folder "Newsletters" -n 1` | summarize the newest email in one folder |
| `uv run tests/outlook_browser/extract_emails.py login\|folders\|list\|read` | browser connector on its own |

Outputs: `reports/digest-*.html` (the digest), `reports/YYYY-MM-DD.json` (all summaries),
`data/swarm.db` (run history, summaries, digests), `logs/*.log`.

## How scheduling works

`scripts/install_schedule.ps1` registers the Task Scheduler task `\PersonalAgent\EmailWorkerDaily`. It runs the
project's `pythonw.exe` directly, so no console window appears. The task has three triggers:

- daily at `daily_at`, repeating hourly until 23:00;
- at logon;
- on wake from sleep.

The logon and wake triggers start 2 minutes late so Wi-Fi can reconnect. Every start goes through a
**once-a-day guard**: it does work only if it's at or after `daily_at` and no run has succeeded since then.
Otherwise it exits within seconds. Opening your laptop at noon therefore produces the digest a couple of
minutes later. A run that fails or is interrupted gets retried the next hour.

Each run covers mail **since the last successful run**, so a skipped day loses nothing. Summaries that failed
are retried, and digests that failed to send are re-sent on the next run.

Nothing runs between triggers. A run takes a few minutes: a headless browser plus a model call per email.

## Troubleshooting

- **"Outlook login expired" notification**: run the `login` command again. How long a session lasts is up to your organization.
- **A folder is missing or the layout changed**: run `extract_emails.py list --folder X --debug`. This saves a
  screenshot and HTML to `.cache/` (they contain mail, so delete them afterwards). Selectors live at the top of
  `src/swarm/connectors/outlook_web.py`.
- **Did the task run?** Check `logs/daily_digest.log`. Every trigger logs `Scheduled trigger fired`, and every
  folder logs `newest row …, N new since cutoff`. Task Scheduler's *Last Run Result* `0x0` means success.
- **Timestamps:** rows are dated from Outlook's tooltip in US format (`Tue 10/20/2026 9:37 AM`). Other display
  locales need `STAMP`/`STAMP_FORMAT` in `outlook_web.py` adjusted.

## Limitations

- Outlook's web page isn't a stable API. Microsoft UI changes can break the selectors. They rely on accessibility
  roles and labels, which change less often than CSS classes.
- Outlook renders a folder from its cache first, then inserts new mail. The connector waits for the list to
  settle; on very slow connections, raise `settle_list`'s timeout.
- Only email text is read. Attached emails (e.g. mailing lists that wrap the original) are opened; file
  attachments and images are ignored.

## Other platforms

The worker, manager, and email delivery are plain Python and Playwright. On macOS or Linux, schedule
`python -m swarm.pipelines.daily_digest --scheduled` with cron or a systemd timer (`Persistent=true`).
Windows toasts are skipped with a warning in the log.

## Project layout

```
src/swarm/
  connectors/outlook_web.py   browser connector: folders, scrolling, reading, attached emails, send-to-self
  skills/summarize.py         worker skill: 2-3 sentence summary (+ redaction)
  agents/email_worker.py      worker agent: batch over folders since last successful run
  agents/inbox_manager.py     manager agent: structured digest from summaries + preferences
  pipelines/daily_digest.py   worker → manager → delivery, scheduled-run guard
  delivery.py                 HTML digest, Windows toast, email rendering
  llm.py                      Anthropic / compatible-gateway client (no tools)
  store.py                    SQLite: runs, emails, digests
scripts/install_schedule.ps1  Task Scheduler registration
tests/outlook_browser/        connector CLI (login, folders, list, read)
```

The `swarm` package is meant to grow into a general agent graph: workers with plug-in skills reporting to
managers. Outlook is the first connector.

## License

[MIT](LICENSE)

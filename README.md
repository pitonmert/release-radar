# Release Radar

release-radar monitors software release feeds, summarizes new releases with Gemini, and sends the summaries to Telegram. It runs as a long-running Docker service that scans every 6 hours.

## Features

- Fetches release updates from configured RSS/Atom sources
- Supports VS Code release notes via GitHub markdown and standard GitHub Atom feeds
- Tracks the entire ChatGPT & Codex changelog, including CLI, desktop, mobile/Remote, and general announcements
- Summarizes release notes with Gemini (output in Turkish)
- Automatically caches summaries, then sends their first page directly to Telegram
- Uses Previous/Next buttons on the same message for multi-page summaries
- Splits long summaries into numbered pages while preserving text and valid HTML
- Runs continuously in Docker, scanning on a fixed interval (default: every 6 hours)
- Stores processed entry IDs in a local SQLite database (`data/release_radar.db`) to avoid duplicate notifications
- Retries failed network requests and API calls with exponential backoff

## Requirements

- Docker and Docker Compose (recommended deployment)
- Python 3.10 or newer (for local development or running the migration script without Docker)
- Telegram bot token and chat ID
- Gemini API key

## Configuration

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Fill in the required values:

```env
TELEGRAM_BOT_TOKEN="your_bot_token"
TELEGRAM_CHAT_ID="your_chat_id"
GEMINI_API_KEY="your_gemini_api_key"
```

Configure sources in `config.json`:

```json
{
  "VS Code": {
    "rss": "https://code.visualstudio.com/feed.xml",
    "type": "vscode_github"
  },
  "Claude Code": {
    "rss": "https://github.com/anthropics/claude-code/releases.atom",
    "type": "github_releases"
  },
  "ChatGPT & Codex": {
    "rss": "https://learn.chatgpt.com/docs/changelog/rss.xml",
    "type": "rss"
  }
}
```

Supported source types:

- `vscode_github` — fetches full release notes from the VS Code docs GitHub repository
- `github_releases` — works with any GitHub repository's Atom feed (`/releases.atom`)
- `rss` — reads full feed content (`content:encoded` when present), falling back to the summary/description. HTML is converted to text; Markdown text is accepted. No category, Insiders, or prerelease filters are applied. Entries are processed oldest first using publication dates (update dates as a fallback); if any date is missing, reverse feed order is used instead.

`config.json` is bind-mounted read-only into the container, so you can edit the source list without rebuilding — changes take effect on the next scan cycle (or immediately after `docker compose restart`).

Upgrading from a version without `rss` support requires rebuilding the image with `docker compose up -d --build`; changing only the configuration is not enough. The existing database is preserved.

### Optional environment variables

- `SCAN_INTERVAL_SECONDS` — seconds between scans (default: `21600`, i.e. 6 hours)
- `GEMINI_MODEL` — automatic summary model (default: `gemini-3.5-flash-lite`)
- `DB_PATH` — path to the SQLite database file (default: `data/release_radar.db`; the provided `docker-compose.yml` sets this to `/app/data/release_radar.db` to match the bind mount)

## Usage

```bash
docker compose up -d --build
```

This runs an immediate scan on startup, then rescans every `SCAN_INTERVAL_SECONDS` (6 hours by default) until stopped. `restart: unless-stopped` means the service survives Docker Desktop or host restarts.

On the first run for a given source, release-radar syncs all existing entries with valid IDs into the SQLite database without sending notifications, including feeds with more than 50 entries. Subsequent runs process only unseen entries. An entry is marked seen after successful Telegram delivery; failed summaries or deliveries are retried on a later scan. Edits to an already-seen entry with the same ID do not trigger another notification.

Useful commands:

```bash
docker compose logs -f     # follow logs
docker compose down        # stop the service
```

### Local development (without Docker)

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m release_radar              # scanner + interactive Telegram listener
pytest                      # run the test suite
ruff check .                # lint
```

### Manually test the latest Claude Code release

From the repository root, with dependencies installed and `.env` configured:

```bash
.venv/bin/python scripts/test_latest_release.py
```

The test default model is `gemini-3.5-flash-lite`. To select a specific model for this test only:

```bash
.venv/bin/python scripts/test_latest_release.py --model gemini-3.8-flash
```

The requested model is logged. This option does not change the production model or persist
any configuration, and does not automatically fall back to another model on failure.

This sends a **real notification** to the configured Telegram chat and uses Gemini quota.
It fetches only Claude Code, freezes the feed, and selects the newest eligible entry by
publication time (update time as fallback). If dates are incomplete, it uses the first
eligible entry in GitHub's newest-first feed and logs a warning. Insiders entries are excluded.

The first scan silently records the feed in a temporary SQLite database. The script removes
the selected entry, then runs the normal summary/delivery pipeline for that entry alone.
Other sources and announcements arriving during the test are not processed. A long summary
may produce several Telegram messages. Running the script again sends the release again.

The temporary database is removed on success, failure, or Ctrl+C. Exit status is 0 on success,
1 on failure, or 130 on Ctrl+C; there is no recurring scan or next-cycle retry. Forced process
termination or a machine crash can leave temporary files behind. The normal local database,
`DB_PATH` setting, and server data are not changed. No server sync is needed.

The script and its automated tests belong in the repository, but are not copied into the
production Docker image or run by the service. Automated tests mock external services and
never send real messages.

### Paginated summaries and cache

The service automatically generates each new summary and commits it to SQLite **before**
sending its first page directly. There is no notification card or extra step to open the content.
Previous/Next buttons edit the same message using cached pages, without Gemini calls or extra
messages. The official release link remains. Single-page summaries have only the official link,
with no counter or navigation buttons. There is no main menu, history list, or `/start`/`/menu`
command handler. Legacy seen IDs are preserved but not backfilled with summaries.
The first scan of a new source remains silent; later edits to an already-seen GUID are ignored.

Only the configured **private chat** can use pagination callbacks. Each callback is bound to
the release originally sent in that message; old menu actions are rejected. A separate long-polling
loop handles buttons while the scan thread fetches and summarizes releases. No public port,
domain, webhook, Redis, or extra service is needed. The database stores source snapshots,
model, summary JSON, rendered pages, notification IDs, and the update offset. Old page buttons work
after a restart as long as that database and bot identity remain available. Rebuild the image
to include the `release_radar/` package; restart after changing the set of sources.

Failed summary generation is retried next scan without sending a notification. Failed delivery
reuses the cached summary, even if the release disappears from the live feed. Pending records
are durable; there is no automatic retention deletion. SQLite upgrades add tables without
deleting `seen_entries`; back up the DB before deployment. Each worker uses its own connection,
and API requests do not hold write transactions. Ambiguous send responses or crashes between
Telegram delivery and DB commit can still produce duplicate notifications.

The service and interactive tests require a single polling consumer per bot. A local lock blocks
duplicates on one host; Telegram conflict errors stop a competing listener across hosts.
Webhook configurations are rejected, never silently removed. Do not run a local interactive
test against a token already used by another interactive service; use a separate test bot.

### Interactive replay test

```bash
.venv/bin/python scripts/test_latest_release.py \
  --version v2.1.277 --model gemini-3.5-flash-lite \
  --interactive --listen-seconds 600
```

This selects exactly the named Claude Code release, prepares one cached summary in a temporary
DB, sends its first summary page, then listens for 10 minutes **after delivery** so you can
try Previous/Next on that message. No other source is summarized.
Without `--version` the latest eligible release is selected; without `--interactive` the previous
one-shot multi-message test remains available. Model overrides never persist to production.

On normal expiry or Ctrl+C, the script replaces its own test messages with a test-ended notice
and removes callback buttons, preserving official links on notifications. The temporary DB is
then removed. Cleanup API failures are reported; buttons may remain visible but no longer work.
An abrupt kill or machine failure can bypass cleanup. This test sends real messages and uses
Gemini quota, but never changes the normal local DB or the server DB. Pending Telegram updates
are not bulk-dropped; old commands predating the test are ignored. Do not use the same bot for
unrelated applications that consume Telegram updates.

### Telegram message format

Each release is sent with a bold source name and version on the same line, such as
`Claude Code v2.1.278`. Multi-part messages append the counter beside the title:
`Claude Code v2.1.277 · 1/4`. Single messages have no counter.
Non-version announcement titles retain their own line; publication dates appear below the header.
The publication date appears only when provided and parseable; an update date is not presented
as a publication date. Releases without version numbers keep their announcement title.

Gemini returns validated JSON with critical items, new features, important fixes, and other
changes. Short announcements are translated faithfully rather than compressed; the prompt asks
to retain commands, affected platforms, conditions, exceptions, pricing, warnings, and opt-out
instructions. An overview is optional and requested only when it helps navigate longer notes.
When absent, the message starts directly with the relevant sections. All sections remain
directly visible regardless of length; there are no expandable quotations. Empty sections
are omitted.

Inline commands and environment settings belong in the same item as their purpose and scope,
not in a detached code section. The prompt requests short sentences within long items.
Single-backtick inline code and `[descriptive label](source URL)` links are converted to safe
Telegram HTML; arbitrary HTML is escaped. Model-generated link destinations must appear in the
supplied source. The official-release button remains separate. Inline tokens are kept together
across message boundaries where they fit; oversized tokens may remain literal text.
Optional code blocks are reserved by the prompt for genuine
multi-line source examples, with context in the relevant item. Copied examples must match the
supplied source exactly, preserving indentation and line breaks. Empty or code-only JSON is
rejected. Malformed summaries are not sent or marked as processed; they retry next scan.

These instructions reduce information loss but do not guarantee semantic completeness.
The prompt uses release-neutral rules rather than product-specific examples, preserves each
action's subject and condition, and forbids inventing causal connections between separate facts.
Link labels are derived from the source topic rather than a predefined example label.
Automated regression tests check the prompt rules and preservation/rendering of a curated
Claude Code example using mocked model output; they do not measure real model quality.

The application creates and escapes Telegram HTML itself. Messages include a
“Resmî sürüm notlarını aç” URL button; VS Code links open its public release page while
the summary still uses the GitHub Markdown content. Missing or invalid HTTP(S) links result
in no button, rather than an invented destination. Link previews are disabled.

Long summaries are split at section/item boundaries where possible, with numbered pages and
repeated source/title headers. Oversized individual lines and code blocks are split without
discarding text. Every part has independently valid HTML and fits the 4096-character limit,
including all content and headers; emoji are counted conservatively using UTF-16 units.
This preserves the generated summary, not the full upstream notes: the existing 15,000-character
input limit and summarization still apply. A header too long to leave room for content causes
the entry to be skipped without marking it processed.

In the paginated service, a release is marked notified after its first page is delivered; navigation
only edits the message. In the legacy one-shot test, all parts must be delivered successfully
before marking the entry seen, and partial delivery can be repeated. Opening URL buttons does
not need a listener; callback navigation requires the service or interactive test to be running.

Tests use fake Gemini/Telegram responses and temporary databases. A Docker build alone does
not start the scanner. Deploying these code changes requires rebuilding the image; starting
the rebuilt service performs the usual immediate scan.

## Automation

release-radar is a long-running service, not a scheduled job — it manages its own internal scan loop (see `SCAN_INTERVAL_SECONDS` above). GitHub Actions is used only for CI (linting, tests, and a Docker build check on every push/PR) and no longer runs the scanner or commits any state back to the repository.

## Runtime Files

- `data/release_radar.db` — SQLite database storing seen IDs, release snapshots, cached summaries, notification records, and menu/update state. Bind-mounted from the host (`./data`) so it persists across container rebuilds; it's gitignored, so each machine keeps its own database. WAL sidecars may exist while it is open; use SQLite's backup facility rather than copying a live database file alone.
- Logs go to stdout — view them with `docker compose logs -f` (or directly in the terminal when running `python -m release_radar` locally).

## Notes

Summaries and Telegram message labels are in Turkish; logs remain in English.

The ChatGPT & Codex feed covers published changelog announcements, not every desktop build. Separate Marketplace and OpenAI product-release feeds are not configured.

## Project layout

```text
release_radar/
├── __init__.py
├── __main__.py       # python -m release_radar entry point
├── config.py         # Environment settings and source configuration
├── feeds.py          # RSS/Atom fetching and release content extraction
├── summarizer.py     # Gemini prompt, generation, and output validation
├── database.py       # SQLite state and summary cache
├── telegram.py       # Telegram HTTP client and direct delivery
├── messages.py       # Message formatting and page splitting
├── pagination.py     # Persistent Previous/Next callbacks
├── publisher.py      # Cache summaries, send, and retry pending releases
└── service.py        # Coordinate scans, listener, and shutdown
```

- `release_radar/` — application package: scanning, storage, message formatting, and Telegram pagination. Run from the repository root with `python -m release_radar`.
- `scripts/` — standalone release replay test using temporary state.
- `tests/` — automated tests using temporary databases.
- `config.json` and `.env` — runtime configuration in the repository root.
- `data/` — gitignored runtime state, created only when needed; the replay test uses its own temporary directory.

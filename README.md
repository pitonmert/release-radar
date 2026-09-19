# Release Radar

release-radar monitors software release feeds, summarizes new releases with Gemini, and sends the summaries to Telegram. It runs as a long-running Docker service that scans every 6 hours.

## Features

- Fetches release updates from configured RSS/Atom sources
- Supports VS Code release notes via GitHub markdown and standard GitHub Atom feeds
- Tracks the entire ChatGPT & Codex changelog, including CLI, desktop, mobile/Remote, and general announcements
- Summarizes release notes with Gemini (output in Turkish)
- Sends formatted Telegram summaries with expandable details, code blocks, and release-note buttons
- Splits long summaries into numbered messages while preserving text and valid HTML
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
python main.py              # runs the same scan-and-sleep loop locally
pytest                      # run the test suite
ruff check .                # lint
```

### Telegram message format

Each release or announcement is sent with a bold source name and the original feed title.
The publication date appears only when provided and parseable; an update date is not presented
as a publication date. Releases without version numbers keep their announcement title.

Gemini returns a validated JSON summary: a short overview, critical items, new features,
important fixes, other changes, and optional source code examples. The overview and critical
items remain visible. Other sections use Telegram's expandable quotations, and empty sections
are omitted. Code examples must match the supplied source text and retain their indentation
and line breaks. Malformed summaries are not sent or marked as processed; they are retried
on the next scan.

The application creates and escapes Telegram HTML itself. Messages include a
“Resmî sürüm notlarını aç” URL button; VS Code links open its public release page while
the summary still uses the GitHub Markdown content. Missing or invalid HTTP(S) links result
in no button, rather than an invented destination. Link previews are disabled.

Long summaries are split at section/item boundaries where possible, with numbered parts and
repeated source/title headers. Oversized individual lines and code blocks are split without
discarding text. Every part has independently valid HTML and fits the 4096-character limit,
including expandable content and headers; emoji are counted conservatively using UTF-16 units.
This preserves the generated summary, not the full upstream notes: the existing 15,000-character
input limit and summarization still apply. A header too long to leave room for content causes
the entry to be skipped without marking it processed.

All parts must be delivered successfully before an entry is marked processed. If a later part
fails, earlier parts may be sent again on the next scan; delivery is not exactly-once.
Expanding quotations and opening URL buttons require no callback listener or webhook.

Tests use fake Gemini/Telegram responses and temporary databases. A Docker build alone does
not start the scanner. Deploying these code changes requires rebuilding the image; starting
the rebuilt service performs the usual immediate scan.

## Automation

release-radar is a long-running service, not a scheduled job — it manages its own internal scan loop (see `SCAN_INTERVAL_SECONDS` above). GitHub Actions is used only for CI (linting, tests, and a Docker build check on every push/PR) and no longer runs the scanner or commits any state back to the repository.

## Runtime Files

- `data/release_radar.db` — SQLite database storing processed entry IDs between scans. Bind-mounted from the host (`./data`) so it persists across container rebuilds; it's gitignored, so each machine keeps its own database.
- Logs go to stdout — view them with `docker compose logs -f` (or directly in the terminal when running `python main.py` locally).

## Migrating from the legacy state.json version

Earlier versions of release-radar tracked state in a git-committed `state.json` file. If you have an existing `state.json` with real history, migrate it into SQLite before the first `docker compose up`:

```bash
python scripts/migrate_state_to_sqlite.py
sqlite3 data/release_radar.db "select source, count(*) from seen_entries group by source;"
git rm --cached state.json
docker compose up -d --build
```

Skipping this step will not cause duplicate Telegram notifications (release-radar's first-run behavior for a source with no history is to silently sync a baseline, not to notify) — but each source's "seen" baseline would be rebuilt from whatever the live feed currently returns (a narrow recent window) instead of your full accumulated history.

## Notes

Summaries and Telegram message labels are in Turkish; logs remain in English.

The ChatGPT & Codex feed covers published changelog announcements, not every desktop build. Separate Marketplace and OpenAI product-release feeds are not configured.

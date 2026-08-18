# Release Radar

release-radar monitors software release feeds, summarizes new releases with Gemini, and sends the summaries to Telegram. It runs as a long-running Docker service that scans every 6 hours.

## Features

- Fetches release updates from configured RSS/Atom sources
- Supports VS Code release notes via GitHub markdown and standard GitHub Atom feeds
- Summarizes release notes with Gemini (output in Turkish)
- Sends summaries to Telegram with chunking support for long messages
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
  }
}
```

Supported source types:

- `vscode_github` — fetches full release notes from the VS Code docs GitHub repository
- `github_releases` — works with any GitHub repository's Atom feed (`/releases.atom`)

`config.json` is bind-mounted read-only into the container, so you can edit the source list without rebuilding — changes take effect on the next scan cycle (or immediately after `docker compose restart`).

### Optional environment variables

- `SCAN_INTERVAL_SECONDS` — seconds between scans (default: `21600`, i.e. 6 hours)
- `DB_PATH` — path to the SQLite database file (default: `data/release_radar.db`; the provided `docker-compose.yml` sets this to `/app/data/release_radar.db` to match the bind mount)

## Usage

```bash
docker compose up -d --build
```

This runs an immediate scan on startup, then rescans every `SCAN_INTERVAL_SECONDS` (6 hours by default) until stopped. `restart: unless-stopped` means the service survives Docker Desktop or host restarts.

On the first run for a given source, release-radar syncs existing entries into the SQLite database without sending notifications. Subsequent runs process only new entries.

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

The Gemini prompt requests summaries in Turkish. Static logs and Telegram message labels are in English.

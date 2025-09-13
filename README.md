# MouseTrace (macOS)

MouseTrace captures mouse events and **app/window switches** on macOS and stores them in a
clean, queryable **SQLite** database.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Permissions (required)

System Settings → Privacy & Security:

* **Accessibility** → enable your Terminal/Python
* **Input Monitoring** → enable your Terminal/Python
* **Screen Recording** → enable your Terminal/Python (for window titles)

You may need to restart Terminal after toggling.

## Quick start

```bash
# 1) Create/upgrade the database
mousetrace init-db --db ~/mousetrace.sqlite

# 2) Run the collector
mousetrace run --db ~/mousetrace.sqlite --poll-hz 10 --move-hz 30

# 3) Peek at some stats
mousetrace analyze --db ~/mousetrace.sqlite --summary
```

## Database

* SQLite with WAL mode, STRICT tables, foreign keys, indexes, and helper **views**.
* Tables: `sessions`, `applications`, `pointer_events`, `switches`, `key_events`.
* Foreign-key edges guarantee referential integrity.

See `mousetrace/schema.sql` for full DDL and views.

## CLI

* `mousetrace init-db` — create/upgrade the DB.
* `mousetrace run` — start the capture service.
* `mousetrace analyze` — quick, built-in summaries.
* `mousetrace serve` — run the FastAPI insights API.

## Launch at login (optional)

Copy `contrib/com.example.mousetrace.plist` to `~/Library/LaunchAgents/` and run:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.mousetrace.plist
```

## License

MIT

## FastAPI Insights API

The project ships an API that exposes summary endpoints and an OpenAI-powered agent to answer questions about your telemetry database.

### Configuration (.env)

Create a `.env` file in the project root with your OpenAI key:

```
OPENAI_API_KEY=sk-...
# optional default model
# OPENAI_MODEL=gpt-4o-mini
```

The server reads from `.env` automatically (via `python-dotenv`). You can also override per request with the `X-OpenAI-Key` header or the `api_key` field in the request body.

### Run the server

```bash
source .venv/bin/activate
pip install -e .
mousetrace init-db --db ~/mousetrace.sqlite

# Start the API (defaults: 127.0.0.1:8000)
mousetrace serve --db ~/mousetrace.sqlite --host 127.0.0.1 --port 8000 --reload

Reload note: when you pass `--reload`, the CLI switches to Uvicorn's import-string mode under the hood. It sets `MOUSETRACE_DB_PATH` so Uvicorn can recreate the app on changes. If you run Uvicorn yourself, do:

```bash
MOUSETRACE_DB_PATH=~/mousetrace.sqlite uvicorn mousetrace.api:app_factory --factory --reload
```
```

### Endpoints

- `GET /health` — liveness check.
- `GET /schema` — returns the SQLite DDL shipped with the package.
- `GET /summary` — quick stats: clicks, moves, switches, keypresses, KPM, best 1‑minute KPM and window, top apps.
- `POST /insights` — ask a natural-language question; the agent will call safe tools against your DB.

Example:

```bash
curl -H 'Content-Type: application/json' \
     -d '{"question":"Top 5 apps by clicks and moves"}' \
     http://127.0.0.1:8000/insights

# Or pass a key explicitly
curl -H 'Content-Type: application/json' \
     -H "X-OpenAI-Key: $OPENAI_API_KEY" \
     -d '{"question":"Which app had the most switches yesterday?"}' \
     http://127.0.0.1:8000/insights
```

### Security notes

- The agent has access only to read-only tools; arbitrary writes are blocked.
- SQL tool only allows `SELECT`/CTEs and rejects mutating statements.

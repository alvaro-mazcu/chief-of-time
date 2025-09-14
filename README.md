# Chief of Time

Chief of Time captures mouse/keyboard/window telemetry to SQLite, augments it with periodic
screenshots + OCR + LLM summaries, and exposes an Agent + API for live notifications, daily
summaries, daily plans, audio→plan ingestion, and an optional presence (avatar) integration.

## Which technologies do we implement?
- An scheduled OCR system for tracking your screen
- A listener on your mouse, keyboard and clicks
- An agent to determine is you are focused or not
- An agent for understanding how did you perform along the day, scoring your respective sessions
- Whisper, for obtaining the daily planning from an audio file
- Lovable, for the frontend (https://github.com/alvaro-mazcu/chief-of-time-frontend)

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
* `mousetrace notify` — periodic macOS notifications about recent productivity.
* `mousetrace sight` — periodic screenshots + OCR + LLM summaries stored in DB.
* `mousetrace recreate-db --yes` — destructive reset + re-init.
* `mousetrace seed-health` — add a sample sleep + activity record.

## FRONTEND

https://github.com/alvaro-mazcu/chief-of-time-frontend

## Launch at login (optional)

Copy `contrib/com.example.mousetrace.plist` to `~/Library/LaunchAgents/` and run:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.mousetrace.plist
```

## License

MIT

## API (FastAPI)

The API exposes summary endpoints, a structured daily summary (JSON), daily plans, audio upload and
transcription into plans (Whisper + LLM), and optional presence (LiveKit + Beyond Presence).

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

### Endpoints (overview)

- `GET /health` — liveness check.
- `GET /schema` — returns the SQLite DDL shipped with the package.
- `GET /summary` — quick stats: clicks, moves, switches, keypresses, KPM, best 1‑minute KPM and window, top apps.
- `POST /insights` — free-form Q&A; the agent calls safe tools against your DB.
- `GET /daily-summary?hours=24` — returns `{ answer, used_tools }`; `answer` is a JSON string with keys:
  `general`, `productivity`, `focus`, `activity`, `sleep`, `key_moments`, `recommendations` — each `{ content, score }`.
- `GET /assessments?limit=50` — recent 2‑minute verdicts.
- `GET /screenshots?limit=50` and `GET /screenshots/{id}` — summaries and OCR text.
- `GET /sleep` — latest sleep `{ score, hours }`.
- `GET /activity?hours=24` — activity `{ score, minutes }` over window.
- `POST /daily-plan` — store a daily plan (accepts `{ plan, plan_date? }` or raw array/object).
- `GET /daily-plan?plan_date=YYYY-MM-DD` — returns the plan or latest.
- `POST /upload-audio` — multipart `file` upload (webm/ogg/mpeg), returns storage path.
- `POST /audio-available` — `{ path, plan_date? }` → transcribe with Whisper and store plan.
- Presence (optional):
  - `GET /presence/token?room=<room>&identity=<user>` → LiveKit browser token
  - `POST /presence/speak` `{ room, text, voice?, avatar_id? }` → avatar says text

Examples:

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

- Read-only toolset for agent queries; SQL is limited to SELECT/CTEs.
- Schema auto-initializes on API start; `init-db` is safe to run anytime.

### Extra: Common run commands
- Initialize DB: `python -m mousetrace init-db --db mouse_trace.sqlite3`
- Run collector: `python -m mousetrace run --db mouse_trace.sqlite3 --poll-hz 10 --move-hz 30`
- Run notifier: `python -m mousetrace notify --db mouse_trace.sqlite3 --interval 120`
- Run screenshots: `python -m mousetrace sight --db mouse_trace.sqlite3 --interval 300 --out-dir ~/Pictures/mousetrace`
- Start API: `python -m mousetrace serve --db mouse_trace.sqlite3 --reload`
- Upload audio: `curl -F "file=@voice.webm" http://127.0.0.1:8000/upload-audio`
- Process audio to plan: `curl -X POST http://127.0.0.1:8000/audio-available -H 'Content-Type: application/json' -d '{"path":"uploads/audio/voice-note-....webm"}'`
- Get daily plan: `curl http://127.0.0.1:8000/daily-plan`

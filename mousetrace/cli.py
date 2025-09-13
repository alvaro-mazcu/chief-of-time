from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import summary
from .config import CaptureConfig
from .database.db import Database
from .capture import run_collector
from .api import create_app

import uvicorn
import os


def cmd_init_db(args: argparse.Namespace) -> None:
    db = Database(Path(args.db))
    db.init_schema()
    print(f"Initialized schema at {db.path}")
    db.close()


def cmd_run(args: argparse.Namespace) -> None:
    cfg = CaptureConfig(
        db_path=Path(args.db),
        poll_hz=args.poll_hz,
        move_hz=args.move_hz,
        record_moves=not args.no_moves,
        record_keys=not args.no_keys,
        log_level=args.log_level,
    )
    run_collector(cfg)


def cmd_analyze(args: argparse.Namespace) -> None:
    info = summary(Path(args.db))
    if args.summary:
        print("Clicks:\t", info["clicks"])
        print("Moves:\t", info["moves"])
        print("Switches:", info["switches"])
        print("Keypresses:", info["keypresses"])
        print("KPM (overall):", info["kpm_overall"])
        print("KPM (last 60m):", info["kpm_last_60m"])
        print("Best KPM (1m window):", info["best_kpm"]) 
        if info.get("best_kpm_window"):
            w = info["best_kpm_window"]
            print(f"  best window start: {w['start_ts']}, end: {w['end_ts']}, keypresses: {w['keypresses']}")
        print("Top apps (by clicks):")
        for name, n in info["top_apps"]:
            print(f"  {name:30s} {n:6d}")


def main() -> None:
    p = argparse.ArgumentParser(prog="mousetrace", description="MouseTrace — macOS pointer & window telemetry")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="Create or upgrade the database schema")
    p_init.add_argument("--db", required=True, help="Path to SQLite database file")
    p_init.set_defaults(func=cmd_init_db)

    p_run = sub.add_parser("run", help="Run the collector")
    p_run.add_argument("--db", required=True, help="Path to SQLite database file")
    p_run.add_argument("--poll-hz", type=int, default=10, help="App/window polling frequency (Hz)")
    p_run.add_argument("--move-hz", type=int, default=30, help="Mouse move sampling frequency (Hz)")
    p_run.add_argument("--no-moves", action="store_true", help="Do not record mouse move events")
    p_run.add_argument("--no-keys", action="store_true", help="Do not record keyboard events")
    p_run.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, ...)")
    p_run.set_defaults(func=cmd_run)

    p_an = sub.add_parser("analyze", help="Quick summaries from the database")
    p_an.add_argument("--db", required=True, help="Path to SQLite database file")
    p_an.add_argument("--summary", action="store_true", help="Show high-level summary")
    p_an.set_defaults(func=cmd_analyze)

    p_srv = sub.add_parser("serve", help="Run the FastAPI insights server")
    p_srv.add_argument("--db", required=True, help="Path to SQLite database file")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8000)
    p_srv.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")

    def cmd_serve(args: argparse.Namespace) -> None:
        db_path = str(Path(args.db).expanduser())
        if args.reload:
            # Use import string + factory so uvicorn can reload workers properly
            os.environ["MOUSETRACE_DB_PATH"] = db_path
            uvicorn.run("mousetrace.api:app_factory", host=args.host, port=args.port, reload=True, factory=True)
        else:
            app = create_app(Path(db_path))
            uvicorn.run(app, host=args.host, port=args.port, reload=False)

    p_srv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

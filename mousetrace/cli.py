from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import summary
from .config import CaptureConfig
from .database.db import Database
from .capture import run_collector
from .api import create_app
from .notify.service import run_notifier, NotifierConfig
from .sight.service import run_sight, SightConfig

import uvicorn
import os
import time


def cmd_init_db(args: argparse.Namespace) -> None:
    db = Database(Path(args.db))
    db.init_schema()
    print(f"Initialized schema at {db.path}")
    db.close()


def cmd_recreate_db(args: argparse.Namespace) -> None:
    db_path = Path(args.db).expanduser()
    if not args.yes:
        print("Refusing to recreate DB without --yes (destructive).")
        return
    # Remove DB and SQLite sidecar files if present
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    db.init_schema()
    print(f"Recreated schema at {db.path}")
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

    p_recreate = sub.add_parser("recreate-db", help="Delete the DB and create a fresh schema (destructive)")
    p_recreate.add_argument("--db", required=True, help="Path to SQLite database file")
    p_recreate.add_argument("--yes", action="store_true", help="Confirm destructive action")
    p_recreate.set_defaults(func=cmd_recreate_db)

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

    # Notify subcommand: periodic productivity notifications
    p_notify = sub.add_parser("notify", help="Send periodic macOS notifications with recent productivity verdicts")
    p_notify.add_argument("--db", required=True, help="Path to SQLite database file")
    p_notify.add_argument("--interval", type=int, default=120, help="Notification interval in seconds (min 30)")
    p_notify.add_argument("--icon", default=None, help="Path to icon image for notifications")
    p_notify.add_argument("--model", default=None, help="Override OpenAI model for the Agent")
    p_notify.add_argument("--with-collector", action="store_true", help="Also start the event collector in the background")

    def cmd_notify(args: argparse.Namespace) -> None:
        db_path = Path(args.db).expanduser()
        schema_path = Path(__file__).resolve().parent / "database" / "schema.sql"
        # Ensure schema exists so Agent tools don't fail on first run
        db = Database(db_path)
        db.init_schema()
        db.close()
        # Optionally start the collector in the background so the DB is populated
        if args.with_collector:
            import threading
            cap_cfg = CaptureConfig(
                db_path=db_path,
            )
            threading.Thread(target=run_collector, args=(cap_cfg,), daemon=True).start()

        cfg = NotifierConfig(
            db_path=db_path,
            schema_path=schema_path,
            interval_sec=args.interval,
            icon_path=args.icon,
            model=args.model,
        )
        run_notifier(cfg)

    p_notify.set_defaults(func=cmd_notify)

    # Sight: periodic screenshots -> OCR -> LLM summary
    p_sight = sub.add_parser("sight", help="Capture screenshots periodically, run OCR + LLM summary, store in DB")
    p_sight.add_argument("--db", required=True, help="Path to SQLite database file")
    p_sight.add_argument("--out-dir", default=str(Path.home() / "Pictures" / "mousetrace"), help="Directory to store screenshots")
    p_sight.add_argument("--interval", type=int, default=300, help="Seconds between screenshots (default 300)")
    p_sight.add_argument("--model", default=None, help="Override OpenAI model for summarization")

    def cmd_sight(args: argparse.Namespace) -> None:
        db_path = Path(args.db).expanduser()
        out_dir = Path(args.out_dir).expanduser()
        # Ensure schema exists
        db = Database(db_path)
        db.init_schema()
        db.close()
        cfg = SightConfig(db_path=db_path, out_dir=out_dir, interval_sec=args.interval, model=args.model)
        run_sight(cfg)

    p_sight.set_defaults(func=cmd_sight)

    # Seed wellness demo data
    p_seed = sub.add_parser("seed-health", help="Insert demo sleep and activity records into the DB")
    p_seed.add_argument("--db", required=True, help="Path to SQLite database file")

    def cmd_seed(args: argparse.Namespace) -> None:
        db_path = Path(args.db).expanduser()
        db = Database(db_path)
        try:
            db.init_schema()
            # Insert: Sleep 8h30m with score 0.95
            sleep_duration = 8 * 3600 + 30 * 60  # 30600 seconds
            db.insert_sleep_log(ts=time.time(), duration_sec=float(sleep_duration), score=0.95)

            # Insert: Activity 1h10m weight lifting, high intensity, 302 kcal
            act_duration = 1 * 3600 + 10 * 60  # 4200 seconds
            db.insert_activity_log(ts=time.time(), kind="weight_lifting", duration_sec=float(act_duration), intensity="high", kcal=302.0)
            print("Seeded: sleep(8h30m, score=0.95), activity(1h10m weight_lifting, high, 302 kcal)")
        finally:
            db.close()

    p_seed.set_defaults(func=cmd_seed)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil


@dataclass(slots=True)
class DBEvent:
    # Generic container for events the writer will persist
    kind: str  # 'pointer', 'switch', 'key', or 'app'
    payload: tuple


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    # --- schema management ---
    def init_schema(self) -> None:
        import importlib.resources as res

        with res.files("mousetrace.database").joinpath("schema.sql").open("r", encoding="utf-8") as f:
            sql = f.read()
        self._conn.executescript(sql)

    # --- sessions ---
    def open_session(self) -> int:
        username = psutil.Process().username()
        hostname = os.uname().nodename
        os_version = os.popen("sw_vers -productVersion").read().strip() or "macOS"
        ts = time.time()
        cur = self._conn.execute(
            "INSERT INTO sessions(started_at, hostname, username, os_version) VALUES (?,?,?,?)",
            (ts, hostname, username, os_version),
        )
        return int(cur.lastrowid)

    def close_session(self, session_id: int) -> None:
        self._conn.execute("UPDATE sessions SET ended_at=? WHERE id=? AND ended_at IS NULL", (time.time(), session_id))

    # --- applications ---
    def upsert_application(self, bundle_id: str, app_name: str) -> None:
        # Synchronous upsert; prefer using the writer queue from multi-threaded contexts.
        self._conn.execute(
            """
            INSERT INTO applications(bundle_id, app_name, first_seen_ts)
            VALUES (?,?,?)
            ON CONFLICT(bundle_id) DO UPDATE SET app_name=excluded.app_name
            """,
            (bundle_id, app_name, time.time()),
        )

    # --- writer queue ---
    def writer(self) -> "DBWriter":
        return DBWriter(self._conn)

    # --- assessments ---
    def insert_assessment(self, start_ts: float, end_ts: float, verdict: str, score: Optional[float], reason: Optional[str] = None) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO productivity_assessments(start_ts, end_ts, verdict, score, reason, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (start_ts, end_ts, verdict, score, reason, time.time()),
        )
        self._conn.commit()

    def insert_screenshot(self, ts: float, path: str, ocr_text: Optional[str], summary: Optional[str], verdict: Optional[str]) -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO screenshots(ts, path, ocr_text, summary, verdict, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (ts, path, ocr_text, summary, verdict, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # --- wellness data ---
    def insert_sleep_log(self, ts: float, duration_sec: float, score: Optional[float]) -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO sleep_logs(ts, duration_sec, score, created_at)
            VALUES (?,?,?,?)
            """,
            (ts, duration_sec, score, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def insert_activity_log(self, ts: float, kind: str, duration_sec: float, intensity: Optional[str], kcal: Optional[float]) -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO activity_logs(ts, kind, duration_sec, intensity, kcal, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (ts, kind, duration_sec, intensity, kcal, time.time()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    # --- daily plans ---
    def insert_daily_plan(self, plan_date: str, plan_json: str) -> int:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO daily_plans(plan_date, plan_json, created_at)
            VALUES (?,?,?)
            ON CONFLICT(plan_date) DO UPDATE SET plan_json=excluded.plan_json, created_at=excluded.created_at
            """,
            (plan_date, plan_json, time.time()),
        )
        self._conn.commit()
        # Return row id of the upserted/inserted row
        row = self._conn.execute("SELECT id FROM daily_plans WHERE plan_date=?", (plan_date,)).fetchone()
        return int(row[0])

    def get_daily_plan(self, plan_date: Optional[str] = None) -> Optional[dict]:
        if plan_date:
            row = self._conn.execute(
                "SELECT id, plan_date, plan_json, created_at FROM daily_plans WHERE plan_date=?",
                (plan_date,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id, plan_date, plan_json, created_at FROM daily_plans ORDER BY plan_date DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row[0]),
            "plan_date": str(row[1]),
            "plan_json": str(row[2]),
            "created_at": float(row[3]),
        }


class DBWriter:
    """Threaded writer that batches commits for low overhead."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.q: "queue.Queue[DBEvent]" = queue.Queue(maxsize=10000)
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def put(self, ev: DBEvent) -> None:
        self.q.put(ev)

    def _run(self) -> None:
        cur = self.conn.cursor()
        last_commit = time.time()
        while self._running or not self.q.empty():
            try:
                ev = self.q.get(timeout=0.5)
            except queue.Empty:
                ev = None

            if ev is not None:
                if ev.kind == "pointer":
                    cur.execute(
                        """
                        INSERT INTO pointer_events
                        (ts, kind, x, y, extra, bundle_id, pid, window_num, session_id)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        ev.payload,
                    )

                elif ev.kind == "switch":
                    cur.execute(
                        """
                        INSERT INTO switches
                        (ts, kind, from_bundle, from_pid, from_window_num, from_window_title,
                         to_bundle, to_pid, to_window_num, to_window_title, session_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        ev.payload,
                    )

                elif ev.kind == "key":
                    cur.execute(
                        """
                        INSERT INTO key_events
                        (ts, kind, key, modifiers, bundle_id, pid, window_num, session_id)
                        VALUES (?,?,?,?,?,?,?,?)
                        """,
                        ev.payload,
                    )
                elif ev.kind == "app":
                    # payload: (bundle_id, app_name)
                    cur.execute(
                        """
                        INSERT INTO applications(bundle_id, app_name, first_seen_ts)
                        VALUES (?,?,?)
                        ON CONFLICT(bundle_id) DO UPDATE SET app_name=excluded.app_name
                        """,
                        (ev.payload[0], ev.payload[1], time.time()),
                    )

            # commit about once a second
            if time.time() - last_commit >= 1.0:
                self.conn.commit()
                last_commit = time.time()

        self.conn.commit()

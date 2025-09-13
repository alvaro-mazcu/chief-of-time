from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
import time
from typing import Optional

from AppKit import NSWorkspace  # type: ignore
import Quartz  # type: ignore
from pynput import mouse  # type: ignore
from pynput import keyboard  # type: ignore

from ..config import CaptureConfig
from ..database.db import DBEvent, Database


# ---- macOS helpers ----

def frontmost_app() -> tuple[str, str, Optional[int]]:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return "", "", None
    return (
        app.localizedName() or "",
        app.bundleIdentifier() or "",
        int(app.processIdentifier()) if app.processIdentifier() else None,
    )


def topmost_window() -> Optional[dict]:
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    wins = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    for w in wins:
        if w.get("kCGWindowLayer", 1) != 0:
            continue
        return {
            "owner_name": w.get("kCGWindowOwnerName", "") or "",
            "owner_pid": int(w.get("kCGWindowOwnerPID", 0) or 0),
            "title": w.get("kCGWindowName", "") or "",
            "window_num": int(w.get("kCGWindowNumber", 0) or 0),
        }
    return None


class Collector:
    def __init__(self, cfg: CaptureConfig) -> None:
        self.cfg = cfg
        self.cfg.validate()
        self.log = logging.getLogger("mousetrace")
        self.db = Database(cfg.db_path)
        self.writer = self.db.writer()
        self.session_id = self.db.open_session()
        self.writer.start()

        # state for switch detection
        self._last_app: dict = {"name": None, "bundle": None, "pid": None}
        self._last_win: dict = {"pid": None, "window_num": None, "title": None}

        # mouse throttling
        self._last_move_ts = 0.0
        self._move_interval = 1.0 / float(self.cfg.move_hz)

        # lifecycle
        self._running = True
        atexit.register(self._shutdown)

    # --- capture loops ---
    def start(self) -> None:
        self.log.info("Collector starting; session_id=%s", self.session_id)
        # focus watcher
        threading.Thread(target=self._focus_loop, daemon=True).start()

        # mouse listener
        listener = mouse.Listener(
            on_move=self._on_move if self.cfg.record_moves else None,
            on_click=self._on_click,
            on_scroll=self._on_scroll,
        )
        listener.start()

        # keyboard listener (optional)
        if self.cfg.record_keys:
            self.kb_listener = keyboard.Listener(
                on_press=self._on_key_press,
                on_release=self._on_key_release,
            )
            self.kb_listener.start()
        else:
            self.kb_listener = None

        self._install_signals(listener)
        listener.join()

    def _focus_loop(self) -> None:
        interval = 1.0 / float(self.cfg.poll_hz)
        while self._running:
            try:
                ts = time.time()
                app_name, bundle_id, pid = frontmost_app()
                win = topmost_window()

                # enqueue application upsert (stable FK target)
                if bundle_id:
                    self.writer.put(DBEvent("app", (bundle_id, app_name or bundle_id)))

                # detect app switch
                if (app_name and bundle_id) and (
                    app_name != self._last_app.get("name")
                    or bundle_id != self._last_app.get("bundle")
                ):
                    self.writer.put(
                        DBEvent(
                            "switch",
                            (
                                ts,
                                "app",
                                self._last_app.get("bundle"),
                                self._last_app.get("pid"),
                                None,
                                None,
                                bundle_id,
                                pid,
                                (win or {}).get("window_num"),
                                (win or {}).get("title"),
                                self.session_id,
                            ),
                        )
                    )
                    self._last_app = {"name": app_name, "bundle": bundle_id, "pid": pid}

                # detect window switch
                if win:
                    if (
                        win.get("owner_pid") != self._last_win.get("pid")
                        or win.get("window_num") != self._last_win.get("window_num")
                    ):
                        self.writer.put(
                            DBEvent(
                                "switch",
                                (
                                    ts,
                                    "window",
                                    self._last_app.get("bundle"),
                                    self._last_app.get("pid"),
                                    self._last_win.get("window_num"),
                                    self._last_win.get("title"),
                                    bundle_id or self._last_app.get("bundle"),
                                    pid if pid is not None else self._last_app.get("pid"),
                                    win.get("window_num"),
                                    win.get("title"),
                                    self.session_id,
                                ),
                            )
                        )
                        self._last_win = {
                            "pid": win.get("owner_pid"),
                            "window_num": win.get("window_num"),
                            "title": win.get("title"),
                        }
            except Exception:
                # swallow transient errors
                pass
            time.sleep(interval)

    # --- mouse handlers ---
    def _on_move(self, x: float, y: float) -> None:
        now = time.time()
        if now - self._last_move_ts < self._move_interval:
            return
        self._last_move_ts = now
        app_name, bundle_id, pid = frontmost_app()
        win = topmost_window()
        if bundle_id:
            self.writer.put(DBEvent("app", (bundle_id, app_name or bundle_id)))
        self.writer.put(
            DBEvent(
                "pointer",
                (
                    now,
                    "move",
                    x,
                    y,
                    None,
                    bundle_id or "",
                    pid,
                    (win or {}).get("window_num"),
                    self.session_id,
                ),
            )
        )

    def _on_click(self, x: float, y: float, button, pressed: bool) -> None:
        ts = time.time()
        app_name, bundle_id, pid = frontmost_app()
        win = topmost_window()
        if bundle_id:
            self.writer.put(DBEvent("app", (bundle_id, app_name or bundle_id)))
        self.writer.put(
            DBEvent(
                "pointer",
                (
                    ts,
                    "click_down" if pressed else "click_up",
                    x,
                    y,
                    str(button),
                    bundle_id or "",
                    pid,
                    (win or {}).get("window_num"),
                    self.session_id,
                ),
            )
        )

    def _on_scroll(self, x: float, y: float, dx: float, dy: float) -> None:
        ts = time.time()
        app_name, bundle_id, pid = frontmost_app()
        win = topmost_window()
        if bundle_id:
            self.writer.put(DBEvent("app", (bundle_id, app_name or bundle_id)))
        self.writer.put(
            DBEvent(
                "pointer",
                (
                    ts,
                    "scroll",
                    x,
                    y,
                    f"{dx},{dy}",
                    bundle_id or "",
                    pid,
                    (win or {}).get("window_num"),
                    self.session_id,
                ),
            )
        )

    # --- lifecycle ---
    def _install_signals(self, listener) -> None:
        def handler(*_):
            self._running = False
            listener.stop()
            self._shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self.writer.stop()
            self.db.close_session(self.session_id)
        finally:
            self.db.close()

    # --- keyboard handlers ---
    def _on_key_press(self, key) -> None:
        ts = time.time()
        app_name, bundle_id, pid = frontmost_app()
        win = topmost_window()
        if bundle_id:
            self.writer.put(DBEvent("app", (bundle_id, app_name or bundle_id)))
        key_text = self._key_to_text(key)
        self.writer.put(
            DBEvent(
                "key",
                (
                    ts,
                    "key_down",
                    key_text,
                    None,
                    bundle_id or "",
                    pid,
                    (win or {}).get("window_num"),
                    self.session_id,
                ),
            )
        )

    def _on_key_release(self, key) -> None:
        ts = time.time()
        app_name, bundle_id, pid = frontmost_app()
        win = topmost_window()
        if bundle_id:
            self.db.upsert_application(bundle_id, app_name or bundle_id)
        key_text = self._key_to_text(key)
        self.writer.put(
            DBEvent(
                "key",
                (
                    ts,
                    "key_up",
                    key_text,
                    None,
                    bundle_id or "",
                    pid,
                    (win or {}).get("window_num"),
                    self.session_id,
                ),
            )
        )

    @staticmethod
    def _key_to_text(key) -> str:
        try:
            if hasattr(key, 'char') and key.char is not None:
                return key.char
            return str(key)
        except Exception:
            return "<unknown>"


def run_collector(cfg: CaptureConfig) -> None:
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    Collector(cfg).start()

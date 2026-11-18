"""Follow ComfyUI runs from its /queue and /history routes and time them.

Pure Python with no Nuke dependency, so it can be tested on its own.
"""
from collections import namedtuple
import time

from . import client

HISTORY_TRIES = 5  # polls to wait for a finished job to appear in /history

View = namedtuple("View", "state status detail last_run session label")


def clock(seconds):
    """Elapsed time as mm:ss, or h:mm:ss from one hour."""
    seconds = max(int(seconds), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def history_result(entry):
    """(seconds or None, outcome) for a finished /history entry.

    The seconds come from ComfyUI's own execution_start and finish timestamps.
    """
    status = entry.get("status") or {}
    messages = status.get("messages")
    stamps = {}
    for message in messages if isinstance(messages, (list, tuple)) else ():
        if isinstance(message, (list, tuple)) and len(message) == 2 and isinstance(message[1], dict):
            stamp = message[1].get("timestamp")
            if isinstance(stamp, (int, float)):
                stamps.setdefault(message[0], stamp)
    start = stamps.get("execution_start")
    end = next((stamps[name] for name in ("execution_success", "execution_error", "execution_interrupted")
                if name in stamps), None)
    seconds = (end - start) / 1000 if start is not None and end is not None and end >= start else None
    if "execution_interrupted" in stamps:
        outcome = "stopped"
    elif status.get("status_str") == "error" or "execution_error" in stamps:
        outcome = "failed"
    elif status.get("status_str") == "success" or "execution_success" in stamps or status.get("completed"):
        outcome = "ok"
    else:
        outcome = "ended"
    return seconds, outcome


class RunTracker:
    """Call poll() every few seconds and view() whenever the display should refresh."""

    def __init__(self, now=time.monotonic):
        self.now = now
        self.online = None      # None until the first answer or failure
        self.waiting = 0
        self.current = None     # run being timed: id, started, partial
        self.finished = []      # runs that left the queue, waiting for /history
        self.last = None        # (seconds, outcome, exact) of the latest finished run
        self.runs = 0
        self.total = 0.0
        self._first_poll = True

    def poll(self, fetch):
        """Update from the server. fetch(route) returns parsed JSON or raises client.TransientError."""
        try:
            queue = fetch("/queue") or {}
        except client.TransientError:
            self.online = False  # A running timer keeps counting through a short outage.
            self._first_poll = False
            return
        self.online = True
        running = [item for item in queue.get("queue_running") or ()
                   if isinstance(item, (list, tuple)) and len(item) > 1]
        self.waiting = len(queue.get("queue_pending") or ())
        running_id = running[0][1] if running else None
        if self.current is not None and self.current["id"] != running_id:
            self.current["ended"] = self.now()
            self.current["tries"] = 0
            self.finished.append(self.current)
            self.current = None
        if running_id is not None and self.current is None:
            # Already running when we first looked: its true start is unknown.
            self.current = {"id": running_id, "started": self.now(), "partial": self._first_poll}
        self._first_poll = False
        self._resolve(fetch)

    def _resolve(self, fetch):
        for run in list(self.finished):
            try:
                entry = (fetch(f"/history/{run['id']}") or {}).get(run["id"])
            except client.TransientError:
                entry = None
            if entry:
                seconds, outcome = history_result(entry)
                exact = seconds is not None
                if not exact:
                    seconds = run["ended"] - run["started"]
            elif run["tries"] < HISTORY_TRIES:
                run["tries"] += 1
                continue
            else:
                seconds, outcome, exact = run["ended"] - run["started"], "ended", False
            self.finished.remove(run)
            self.last = (seconds, outcome, exact)
            self.runs += 1
            self.total += seconds

    def view(self):
        last = "No runs seen yet"
        if self.last is not None:
            seconds, outcome, exact = self.last
            last = f"{clock(seconds)} · {outcome}" + ("" if exact else " (timed here)")
        session = (f"{self.runs} run{'s' if self.runs != 1 else ''} · {clock(self.total)} total"
                   if self.runs else "No runs yet")
        waiting = f"{self.waiting} waiting" if self.waiting else ""
        if self.current is not None:
            elapsed = ("≥" if self.current["partial"] else "") + clock(self.now() - self.current["started"])
            status = f"Running · {elapsed}" + ("" if self.online is not False else " · connection lost")
            detail = " · ".join(part for part in (str(self.current["id"])[:8], waiting) if part)
            return View("running", status, detail, last, session, elapsed)
        if self.online is False:
            return View("offline", "ComfyUI offline · retrying", "", last, session, "offline")
        if self.online is None:
            return View("starting", "Connecting to ComfyUI", "", last, session, "…")
        return View("idle", "Idle · ComfyUI online", waiting, last, session, "idle")

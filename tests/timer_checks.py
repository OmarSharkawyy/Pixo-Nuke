"""Checks for the run timer: a fake clock, a real HTTP fixture server, and a fake Nuke."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import runpy
import socket
import sys
import threading
import time
import types

ROOT = Path(__file__).resolve().parents[1]
STARTED_MS = 1_700_000_000_000


def entry(seconds, status="success", finish="execution_success"):
    """A /history entry for a job that ran for `seconds`."""
    return {"status": {"status_str": status, "completed": status == "success", "messages": [
        ["execution_start", {"prompt_id": "x", "timestamp": STARTED_MS}],
        [finish, {"prompt_id": "x", "timestamp": STARTED_MS + int(seconds * 1000)}]]}}


class Server:
    """Scripted ComfyUI state shared by the fake fetch and the HTTP fixture."""

    def __init__(self):
        self.running, self.pending, self.history = [], [], {}
        self.down = False
        self.http_error = None

    def queue(self):
        return {"queue_running": [[0, job, {}, {}, []] for job in self.running],
                "queue_pending": [[1, job, {}, {}, []] for job in self.pending]}

    def fetch(self, route):
        from pixo import client
        if self.down:
            raise client.TransientError("down")
        if route == "/queue":
            return self.queue()
        job = route.rsplit("/", 1)[1]
        return {job: self.history[job]} if job in self.history else {}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tracker_test(tracker_module):
    from pixo import client
    clock_of = tracker_module.clock
    assert [clock_of(value) for value in (0, 5, 65, 3599, 3600, 3725, -3)] == [
        "00:00", "00:05", "01:05", "59:59", "1:00:00", "1:02:05", "00:00"]
    assert tracker_module.history_result(entry(7.5)) == (7.5, "ok")
    assert tracker_module.history_result(entry(3, "error", "execution_error")) == (3.0, "failed")
    assert tracker_module.history_result(entry(3, "error", "execution_interrupted")) == (3.0, "stopped")
    for broken in ({}, {"status": {"messages": None}}, {"status": {"messages": [["execution_start", {}]]}},
                   {"status": {"messages": [["execution_success", {"timestamp": 1}],
                                            ["execution_start", {"timestamp": 9}]]}}):
        assert tracker_module.history_result(broken)[0] is None, broken
    assert tracker_module.history_result({"status": {"status_str": "success"}}) == (None, "ok")

    def new():
        return Server(), Clock()

    # Idle, then a run with a queue behind it, then the finish reported by ComfyUI itself.
    server, clock = new()
    tracker = tracker_module.RunTracker(clock)
    assert tracker.view().state == "starting"
    tracker.poll(server.fetch)
    view = tracker.view()
    assert (view.state, view.status, view.label) == ("idle", "Idle · ComfyUI online", "idle"), view
    assert view.last_run == "No runs seen yet" and view.session == "No runs yet"
    server.running, server.pending = ["job-aaaaaaaa-1"], ["b", "c"]
    tracker.poll(server.fetch)
    clock.now += 90
    view = tracker.view()
    assert (view.state, view.status, view.detail, view.label) == (
        "running", "Running · 01:30", "job-aaaa · 2 waiting", "01:30"), view
    server.running, server.pending = [], []
    server.history["job-aaaaaaaa-1"] = entry(83.5)
    tracker.poll(server.fetch)
    view = tracker.view()
    assert view.state == "idle" and view.last_run == "01:23 · ok" and view.session == "1 run · 01:23 total", view

    # Already running when watching began: shown as a lower bound, then replaced by the exact time.
    server, clock = new()
    server.running = ["late"]
    tracker = tracker_module.RunTracker(clock)
    tracker.poll(server.fetch)
    clock.now += 5
    assert tracker.view().status == "Running · ≥00:05", tracker.view()
    server.running = []
    server.history["late"] = entry(600)
    tracker.poll(server.fetch)
    assert tracker.view().last_run == "10:00 · ok"

    # A short outage keeps the timer counting; an outage while idle says offline.
    server, clock = new()
    tracker = tracker_module.RunTracker(clock)
    tracker.poll(server.fetch)
    server.running = ["job"]
    tracker.poll(server.fetch)
    clock.now += 30
    server.down = True
    tracker.poll(server.fetch)
    assert tracker.view().status == "Running · 00:30 · connection lost", tracker.view()
    clock.now += 30
    server.down = False
    tracker.poll(server.fetch)
    assert tracker.view().status == "Running · 01:00", tracker.view()
    server.running = []
    server.history["job"] = entry(58)
    tracker.poll(server.fetch)
    server.down = True
    tracker.poll(server.fetch)
    view = tracker.view()
    assert (view.state, view.label, view.last_run) == ("offline", "offline", "00:58 · ok"), view

    # History is slow to appear: wait for it, but only a few polls.
    server, clock = new()
    tracker = tracker_module.RunTracker(clock)
    tracker.poll(server.fetch)
    server.running = ["slow"]
    tracker.poll(server.fetch)
    clock.now += 42
    server.running = []
    for _ in range(2):
        tracker.poll(server.fetch)
        assert tracker.last is None
    server.history["slow"] = entry(41)
    tracker.poll(server.fetch)
    assert tracker.view().last_run == "00:41 · ok"
    server, clock = new()
    tracker = tracker_module.RunTracker(clock)
    tracker.poll(server.fetch)
    server.running = ["gone"]
    tracker.poll(server.fetch)
    clock.now += 42
    server.running = []
    polls = 0
    while tracker.last is None:
        tracker.poll(server.fetch)
        polls += 1
        assert polls <= tracker_module.HISTORY_TRIES + 1, "never gave up waiting for /history"
    assert polls == tracker_module.HISTORY_TRIES + 1
    assert tracker.view().last_run == "00:42 · ended (timed here)", tracker.view()

    # Failed and stopped runs, and one job replacing another between two polls.
    server, clock = new()
    tracker = tracker_module.RunTracker(clock)
    tracker.poll(server.fetch)
    server.running = ["first"]
    tracker.poll(server.fetch)
    server.running = ["second"]
    server.history["first"] = entry(3, "error", "execution_error")
    tracker.poll(server.fetch)
    assert tracker.last == (3.0, "failed", True) and tracker.current["id"] == "second"
    server.running = []
    server.history["second"] = entry(9, "error", "execution_interrupted")
    tracker.poll(server.fetch)
    assert tracker.last == (9.0, "stopped", True) and tracker.runs == 2 and tracker.total == 12.0
    assert tracker.view().session == "2 runs · 00:12 total"

    # A reply that is not queue-shaped must not crash the tracker.
    tracker = tracker_module.RunTracker(Clock())
    tracker.poll(lambda route: None)
    tracker.poll(lambda route: {"queue_running": ["junk", [1]], "queue_pending": None})
    assert tracker.view().state == "idle"
    assert issubclass(client.TransientError, RuntimeError)


def http_test(client):
    release = threading.Event()

    class Reply(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/stalled":
                self.send_response(200)
                self.send_header("Content-Length", "50")
                self.end_headers()
                self.wfile.flush()
                release.wait(5)
                return
            body = b"not json" if self.path == "/garbage" else b'{"ok": true}'
            self.send_response({"/busy": 503, "/missing": 404}.get(self.path, 200))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with ThreadingHTTPServer(("127.0.0.1", 0), Reply) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host = f"http://127.0.0.1:{server.server_port}"
        try:
            assert client.get_json(host, "/queue") == {"ok": True}
            for base, route, transient in ((host, "/busy", True), (host, "/missing", False),
                                           (host, "/stalled", True), (host, "/garbage", True),
                                           (f"http://127.0.0.1:{closed_port}", "/queue", True)):
                try:
                    client.get_json(base, route, timeout=1)
                except client.TransientError:
                    assert transient, route
                except RuntimeError:
                    assert not transient, route
                else:
                    raise AssertionError(f"{route} did not fail")
        finally:
            release.set()
            server.shutdown()
    assert client.valid_host(" http://localhost:8188/ ") == "http://localhost:8188"
    assert client.valid_host("") == client.DEFAULT_URL
    for bad in ("localhost:8188", "ftp://x", "http://user:pw@host", "http://"):
        try:
            client.valid_host(bad)
        except ValueError as error:
            assert "valid ComfyUI URL" in str(error)
        else:
            raise AssertionError(bad)


class Knob:
    def __init__(self, value=""):
        self.data = value

    def value(self):
        return self.data

    def setValue(self, value):
        self.data = value


class Node:
    def __init__(self, name, **knobs):
        self.name = name
        self.knobs = {key: Knob(value) for key, value in knobs.items()}

    def knob(self, name):
        return self.knobs.get(name)

    __getitem__ = lambda self, name: self.knobs[name]
    fullName = lambda self: self.name


def wait_for(condition, what, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"Timed out waiting for {what}.")


def nuke_test(nuke, timer_ui, client):
    """Start, watch, and stop the gizmo against a real HTTP server, as Nuke would."""
    state = Server()

    class ComfyUI(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if state.http_error:
                self.send_response(state.http_error)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(state.queue() if self.path == "/queue" else state.fetch(self.path)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    timer_ui.TICK_SECONDS, timer_ui.TICKS_PER_POLL = 0.03, 2
    script = types.SimpleNamespace(flag=False, sets=0)
    script.modified = lambda: script.flag
    script.setModified = lambda value: (setattr(script, "flag", value), setattr(script, "sets", script.sets + 1))
    nuke.root = lambda: script
    messages = []
    nuke.message = messages.append
    graph = {}

    def make(name="PixoTimer1", url=None):
        node = Node(name, server=url, status="Not watching", detail="", last_run="", session="",
                    watch_token="", label="Pixo Timer")
        graph[name] = node
        return node

    nuke.toNode = graph.get
    with ThreadingHTTPServer(("127.0.0.1", 0), ComfyUI) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            node = make(url=url)
            timer_ui.start(node)
            timer_ui.start(node)
            assert messages == ["This timer is already watching ComfyUI."], messages
            wait_for(lambda: node["status"].value().startswith("Idle"), "idle status")
            state.running = ["job-1111111-x"]
            wait_for(lambda: node["status"].value().startswith("Running"), "running status")
            wait_for(lambda: node["detail"].value() == "job-1111", "job id")
            assert re.fullmatch(r"\d\d:\d\d", node["label"].value()), node["label"].value()
            state.running = []
            state.history["job-1111111-x"] = entry(2.5)
            wait_for(lambda: node["last_run"].value() == "00:02 · ok", "last run")
            assert node["session"].value() == "1 run · 00:02 total"
            assert node["status"].value().startswith("Idle")
            assert script.flag is False and script.sets > 0, "The timer marked the script as modified."
            script.flag = True  # A script with real unsaved changes must stay modified.
            timer_ui.show(node, "x", "", None, None, "x")
            assert script.flag is True
            script.flag = False
            timer_ui.stop(node)
            assert not timer_ui.WATCHERS and node["status"].value() == "Stopped"
            assert node["label"].value() == timer_ui.NODE_LABEL
            wait_for(lambda: not any(t.name == "PixoTimer" for t in threading.enumerate()), "thread to end")
            timer_ui.stop(node)
            assert messages[-1] == "This timer is not watching ComfyUI."

            # Deleting the node, or another script reusing its name, ends the watcher by itself.
            node = make("PixoTimer2", url)
            timer_ui.start(node)
            wait_for(lambda: node["status"].value().startswith("Idle"), "idle status")
            del graph["PixoTimer2"]
            wait_for(lambda: not timer_ui.WATCHERS, "watcher release after delete")
            node = make("PixoTimer3", url)
            timer_ui.start(node)
            wait_for(lambda: node["status"].value().startswith("Idle"), "idle status")
            graph["PixoTimer3"] = other = Node("PixoTimer3", status="", detail="", last_run="",
                                                      session="", watch_token="another", label="")
            wait_for(lambda: not timer_ui.WATCHERS, "watcher release after rename")
            assert other["status"].value() == "", "A node from another script was modified."

            # A URL that is not a ComfyUI server (HTTP 404) stops with a message instead of retrying forever.
            state.http_error = 404
            node = make("PixoTimer4", url)
            count = len(messages)
            timer_ui.start(node)
            wait_for(lambda: len(messages) > count, "error message")
            assert "HTTP 404" in messages[-1] and node["status"].value().startswith("Stopped: ")
            assert not timer_ui.WATCHERS
            state.http_error = None

            # A bad URL never starts a watcher.
            node = make("PixoTimer5", "ftp://nope")
            timer_ui.start(node)
            assert "valid ComfyUI URL" in messages[-1] and not timer_ui.WATCHERS

            # An unreachable server shows as offline and keeps retrying.
            node = make("PixoTimer6", "http://127.0.0.1:9")
            timer_ui.start(node)
            wait_for(lambda: node["status"].value().startswith("ComfyUI offline"), "offline status")
            timer_ui.stop(node)
        finally:
            server.shutdown()
            for name, (_, stopper) in list(timer_ui.WATCHERS.items()):
                stopper.set()
            timer_ui.WATCHERS.clear()

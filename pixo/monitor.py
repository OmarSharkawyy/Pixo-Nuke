"""Collect system readings on a background thread. No Nuke or Qt dependency."""
from collections import deque, namedtuple
import threading
from urllib.parse import urlparse

from . import client
from .sysinfo import LocalSampler, server_metrics

METRICS = ("cpu", "ram", "gpu", "vram", "temp")
HISTORY_LENGTH = 60  # samples kept for the sparklines
STATES = {}          # node full name -> MonitorState

Snapshot = namedtuple("Snapshot", "running note latest history")


class MonitorState:
    """Latest readings and short history, shared between the sampler thread and the panel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.note = ""
        self.latest = {}
        self.history = {name: deque(maxlen=HISTORY_LENGTH) for name in METRICS}

    def set_running(self, running, note=""):
        with self.lock:
            self.running, self.note = running, note

    def update(self, metrics, note):
        with self.lock:
            self.latest, self.note = dict(metrics), note
            for name in METRICS:
                self.history[name].append(metrics[name].percent if name in metrics else None)

    def snapshot(self):
        with self.lock:
            return Snapshot(self.running, self.note, dict(self.latest),
                            {name: list(values) for name, values in self.history.items()})


def is_local(host):
    return urlparse(host).hostname in ("127.0.0.1", "localhost", "::1")


def resolve_source(mode, host):
    """'local' reads this computer, 'server' reads what ComfyUI reports."""
    if mode == "auto":
        return "local" if is_local(host) else "server"
    return mode


def sample_once(source, local, fetch, gpu_index=0):
    """One round of readings and a short note about where they came from."""
    if source == "server":
        try:
            found = server_metrics(fetch("/system_stats"), gpu_index)
        except client.TransientError:
            return {}, "ComfyUI is not reachable"
        return found, "ComfyUI server: RAM and VRAM only" if found else "ComfyUI did not report RAM or VRAM"
    found = local.sample()
    note = "This computer"
    if "vram" not in found:
        # No readable local GPU (not NVIDIA, or nvidia-smi is missing): use what ComfyUI reports.
        try:
            reported = server_metrics(fetch("/system_stats"), gpu_index)
        except client.TransientError:
            reported = {}
        if "vram" in reported:
            found["vram"] = reported["vram"]
            note = "This computer; VRAM from ComfyUI"
    return found, note


def run(state, host, mode, gpu_index, stop, interval=1.0, fetch=None, local=None, on_sample=None):
    """Sample until `stop` is set, updating `state` (and calling on_sample) each time."""
    local = local if local is not None else LocalSampler(gpu_index)
    fetch = fetch if fetch is not None else (lambda route: client.get_json(host, route))
    source = resolve_source(mode, host)
    state.set_running(True)
    try:
        while not stop.is_set():
            try:
                metrics, note = sample_once(source, local, fetch, gpu_index)
            except Exception as error:  # One bad reading must not end the monitor.
                metrics, note = {}, f"Read error: {error}"
            state.update(metrics, note)
            if on_sample is not None:
                on_sample(metrics, note)
            if stop.wait(interval):
                break
    finally:
        state.set_running(False)

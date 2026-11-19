"""Estimate how long a running ComfyUI job has left, for any workflow and whoever queued it.

Two sources are used, and neither needs the job to have been started from Nuke:

- ComfyUI's console log (/internal/logs/raw). Every sampler prints a progress bar there, such as
  "2/3 [03:00<01:30, 90.28s/it]", which gives the current step and the time left in that sampler.
  (ComfyUI sends its websocket progress only to the client that queued the job, so the log is the
  one place where any watcher can see it.)
- How long the same workflow took before: ComfyUI's /history, and runs watched here, which also
  remember when each sampler started and how long it took. "The same workflow" means the same nodes,
  models and size settings (width, height, frames, steps, ...); prompts and seeds are ignored.

Pure Python with no Nuke dependency, so it can be tested on its own.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import tempfile
import threading
import time

from . import client

BAR = re.compile(r"(\d+)/(\d+) \[(?:(\d+):)?(\d+):(\d+)<(?:(?:(\d+):)?(\d+):(\d+)|\?)")
SIZE_INPUTS = {"width", "height", "length", "frames", "num_frames", "frame_count", "video_frames",
               "max_frames", "frame_load_cap", "batch_size", "steps", "scale_by", "upscale_by",
               "megapixels", "resolution"}
MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft", ".onnx")
KEEP_RUNS = 5           # runs remembered per workflow
KEEP_WORKFLOWS = 200    # workflows remembered in the timings file
HISTORY_ITEMS = 64      # recent /history entries read when a job starts
SPEED_LIMITS = (0.5, 2.0)
_FILE_LOCK = threading.Lock()


def default_store_path():
    """Where finished runs are remembered. PIXO_TIMINGS_FILE overrides it."""
    override = os.environ.get("PIXO_TIMINGS_FILE", "").strip()
    return Path(override) if override else Path.home() / ".nuke" / "pixo_timings.json"


def _seconds(hours, minutes, seconds):
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def parse_bar(text):
    """(step, total, elapsed, remaining or None) from the last progress bar in a log line, or None."""
    found = None
    for found in BAR.finditer(text or ""):
        pass
    if found is None:
        return None
    step, total = int(found.group(1)), int(found.group(2))
    if total <= 0 or step > total:
        return None
    elapsed = _seconds(found.group(3), found.group(4), found.group(5))
    remaining = None if found.group(7) is None else _seconds(found.group(6), found.group(7), found.group(8))
    return step, total, elapsed, remaining


def signature(prompt):
    """A short id for a workflow: its node types, model files and size settings.

    Prompts, seeds and input file names are left out, so re-running a shot with new text or a new
    take still counts as the same workflow, while a new resolution or frame count does not.
    """
    if not isinstance(prompt, dict) or not prompt:
        return None
    parts = []
    for step in prompt.values():
        if not isinstance(step, dict):
            continue
        kept = []
        for name, value in sorted((step.get("inputs") or {}).items()):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and name in SIZE_INPUTS:
                kept.append([name, value])
            elif isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES):
                kept.append([name, value])
        parts.append([str(step.get("class_type")), kept])
    if not parts:
        return None
    parts.sort(key=lambda part: json.dumps(part, sort_keys=True))
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def history_seconds(entry):
    """Seconds of a successful /history entry, from ComfyUI's own timestamps; None otherwise."""
    status = entry.get("status") or {}
    stamps = {}
    for message in status.get("messages") or ():
        if isinstance(message, (list, tuple)) and len(message) == 2 and isinstance(message[1], dict):
            stamp = message[1].get("timestamp")
            if isinstance(stamp, (int, float)):
                stamps.setdefault(message[0], stamp)
    start, end = stamps.get("execution_start"), stamps.get("execution_success")
    if start is None or end is None or end < start or status.get("status_str") == "error":
        return None
    return (end - start) / 1000


class TimingStore:
    """Runs of each workflow, kept in a small JSON file so estimates survive a Nuke restart."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_store_path()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def runs(self, sig):
        record = self.load().get(sig) or {}
        return [run for run in record.get("runs") or () if isinstance(run, dict)
                and isinstance(run.get("seconds"), (int, float))]

    def add(self, sig, run):
        with _FILE_LOCK:
            data = self.load()
            record = data.get(sig) if isinstance(data.get(sig), dict) else {}
            record["runs"] = ([*record.get("runs", []), run])[-KEEP_RUNS:]
            record["used"] = time.time()
            data[sig] = record
            if len(data) > KEEP_WORKFLOWS:
                oldest = sorted(data, key=lambda key: (data[key] or {}).get("used", 0))
                for key in oldest[:len(data) - KEEP_WORKFLOWS]:
                    data.pop(key, None)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".pixo_timings_")
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(data, stream)
                os.replace(temp, self.path)
            except OSError:
                pass  # An estimate that is not remembered is not worth an error.


class Job:
    def __init__(self, job_id, sig, started, partial):
        self.id, self.sig, self.started, self.partial = job_id, sig, started, partial
        self.bars = []          # dicts: total, step, start, seen, remaining, done
        self.cursor = None      # timestamp of the newest log line already read
        self.primed = False     # the log lines from before this job have been skipped


class Estimator:
    """Follows one job at a time for a RunTracker. Every network failure only weakens the estimate."""

    def __init__(self, store=None):
        self.store = store if store is not None else TimingStore()
        self.logs_ok = True     # False once the server shows it has no log route
        self.job = None
        self.ended = {}         # job id -> Job, waiting for its /history result
        self.past = {}          # signature -> {job id: seconds} from ComfyUI's /history

    # --- following the job -------------------------------------------------------------------
    def begin(self, fetch, job_id, prompt, partial, now):
        self.job = Job(job_id, signature(prompt), now, partial)
        self._read_history(fetch)
        self._read_logs(fetch, now, skip=True)  # older lines belong to earlier jobs

    def update(self, fetch, now):
        if self.job is not None:
            self._read_logs(fetch, now)

    def end(self, job_id):
        if self.job is not None and self.job.id == job_id:
            self.ended[job_id] = self.job
            self.job = None

    def finish(self, job_id, seconds, outcome, exact):
        job = self.ended.pop(job_id, None)
        if job is None or job.sig is None or outcome != "ok" or not exact or job.partial:
            return
        bars = [[bar["total"], round(bar["start"] - job.started, 2), round(bar["length"], 2)]
                for bar in job.bars if bar.get("done")] if self.logs_ok else None
        self.store.add(job.sig, {"id": str(job_id), "seconds": round(seconds, 2), "bars": bars})

    def _read_history(self, fetch):
        try:
            history = fetch(f"/history?max_items={HISTORY_ITEMS}") or {}
        except Exception:  # Optional: without it the estimate starts from runs watched here.
            return
        past = {}
        for job_id, entry in history.items() if isinstance(history, dict) else ():
            if not isinstance(entry, dict):
                continue
            prompt = entry.get("prompt")
            sig = signature(prompt[2]) if isinstance(prompt, (list, tuple)) and len(prompt) > 2 else None
            seconds = history_seconds(entry)
            if sig is not None and seconds is not None:
                past.setdefault(sig, {})[str(job_id)] = seconds
        self.past = past

    def _read_logs(self, fetch, now, skip=False):
        if not self.logs_ok:
            return
        try:
            reply = fetch("/internal/logs/raw") or {}
        except client.TransientError:
            return
        except Exception:  # Older ComfyUI, or a server without the log route: estimate without it.
            self.logs_ok = False
            return
        entries = reply.get("entries") if isinstance(reply, dict) else None
        if not isinstance(entries, list):
            return
        job = self.job
        skip = skip or not job.primed
        job.primed = True
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("t"), str):
                continue
            stamp = entry["t"]
            if job.cursor is not None and stamp <= job.cursor:
                continue
            job.cursor = stamp
            if not skip:
                self._take(job, parse_bar(entry.get("m")), now)

    @staticmethod
    def _take(job, bar, now):
        if bar is None:
            return
        step, total, elapsed, remaining = bar
        last = job.bars[-1] if job.bars else None
        if last is None or last["done"] or last["total"] != total or step < last["step"]:
            if last is not None and not last["done"]:
                last["done"], last["length"] = True, now - last["start"]
            last = {"total": total, "done": False}
            job.bars.append(last)
        # The newest line read is the closest to now, so it gives the best start time.
        last.update(step=step, seen=now, remaining=remaining, elapsed=elapsed, start=now - elapsed)
        if step >= total:
            last["done"], last["length"] = True, elapsed

    # --- the estimate --------------------------------------------------------------------------
    def describe(self, now):
        """(text for the Estimate field, short text for the node label), or ("", "") with nothing yet."""
        job = self.job
        if job is None:
            return "", ""
        elapsed = now - job.started
        bar = job.bars[-1] if job.bars else None
        live = None if bar is None or bar["done"] else self._bar_left(bar, now)
        runs = self.store.runs(job.sig) if job.sig else []
        known = dict(self.past.get(job.sig, {}))
        known.update({run.get("id") or f"watched-{index}": run["seconds"] for index, run in enumerate(runs)})
        totals = list(known.values())
        left, basis = None, ""
        profile = None if job.partial else self._profile(runs, job)
        if profile is not None:
            left = self._from_profile(profile, job, now, live)
            basis = f"from {len(totals)} earlier run{'s' if len(totals) != 1 else ''}"
        elif totals and not job.partial:  # a job already running when watching began has no known start
            left = statistics.median(totals) - elapsed
            if live is not None:
                left = max(left, live)
            basis = f"from {len(totals)} earlier run{'s' if len(totals) != 1 else ''}"
        step = ""
        if bar is not None:
            count = len(profile) if profile is not None else None
            sampler = f"sampler {len(job.bars)}" + (f" of {count}" if count and count >= len(job.bars) else "")
            step = f"{sampler} · step {bar['step']}/{bar['total']}"
        if left is None:
            if live is None:
                if job.partial:
                    reason = "No estimate: the job was already running when watching began"
                else:
                    reason = "No estimate yet: this workflow has not finished here before"
                return " · ".join(part for part in (reason, step) if part), ""
            text = f"~{_clock(live)} left in this sampler · {step}"
            return text, f"~{_clock(live)} sampler"
        if left <= 0:
            return " · ".join(part for part in ("Running longer than before", step, basis) if part), "over"
        ends = time.strftime("%H:%M", time.localtime(time.time() + left))
        text = " · ".join(part for part in (f"~{_clock(left)} left (about {ends})", step, basis) if part)
        return text, f"~{_clock(left)} left"

    @staticmethod
    def _bar_left(bar, now):
        since = now - bar["seen"]
        if bar.get("remaining") is not None:
            return max(bar["remaining"] - since, 0.0)
        if bar["step"] > 0 and bar.get("elapsed"):
            return max(bar["elapsed"] / bar["step"] * (bar["total"] - bar["step"]) - since, 0.0)
        return None

    @staticmethod
    def _profile(runs, job):
        """The bars of the median remembered run, when its samplers match this job's so far."""
        timed = [run for run in runs if isinstance(run.get("bars"), list) and run["bars"]]
        if not timed:
            return None
        timed.sort(key=lambda run: run["seconds"])
        run = timed[len(timed) // 2]
        bars = [bar for bar in run["bars"] if isinstance(bar, list) and len(bar) == 3]
        if len(job.bars) > len(bars) or any(seen["total"] != bars[index][0]
                                            for index, seen in enumerate(job.bars)):
            return None
        return [(total, start, length, run["seconds"]) for total, start, length in bars]

    @staticmethod
    def _from_profile(profile, job, now, live):
        total_seconds = profile[0][3]
        if not job.bars:
            return total_seconds - (now - job.started)
        index = len(job.bars) - 1
        _, start, length, _ = profile[index]
        bar = job.bars[-1]
        after = total_seconds - (start + length)  # later samplers, decoding and saving
        speed = 1.0
        if bar.get("elapsed") and bar.get("step") and length:
            speed = (bar["elapsed"] / bar["step"]) / (length / profile[index][0])
            speed = min(max(speed, SPEED_LIMITS[0]), SPEED_LIMITS[1])
        if not bar["done"]:
            return (live if live is not None else length * speed) + after * speed
        return after * speed - (now - (bar["start"] + bar["length"]))


def _clock(seconds):
    seconds = max(int(round(seconds)), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

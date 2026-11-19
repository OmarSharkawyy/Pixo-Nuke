"""Estimate how long a running ComfyUI job has left, for any workflow and whoever queued it.

Two sources are used, and neither needs the job to have been started from Nuke:

- ComfyUI's console log (/internal/logs/raw). Every sampler prints a progress bar there, such as
  "2/3 [03:00<01:30, 90.28s/it]", which gives the current step and the time left in that sampler.
  (ComfyUI sends its websocket progress only to the client that queued the job, so the log is the
  one place where any watcher can see it.)
- How long the same workflow took before: ComfyUI's /history, and runs watched here, which also
  remember when each sampler started and how long it took.

"The same workflow" means the same nodes and models; prompts, seeds and input files are ignored.
Runs at the same size give the closest estimate. Runs at another size are scaled: first by the
pixel count, then, as soon as steps are running, by how fast those steps actually go, which also
covers clip length and GPU load.

Pure Python with no Nuke dependency, so it can be tested on its own.
"""
import hashlib
import json
import math
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
KEEP_RUNS = 5           # runs remembered per workflow and size
KEEP_WORKFLOWS = 200    # workflow sizes remembered in the timings file
HISTORY_ITEMS = 64      # recent /history entries read when a job starts
SAME_SIZE_SPEED = (0.5, 2.0)    # how far live step speed may move an estimate from a same-size run
OTHER_SIZE_SPEED = (0.05, 20.0)
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


def signature(prompt, sizes=True):
    """A short id for a workflow: its node types and model files, plus its size settings if sizes.

    Prompts, seeds and input file names are always left out, so re-running a shot with new text or
    a new take counts as the same workflow. With sizes=False, a new resolution does too.
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
            if sizes and isinstance(value, (int, float)) and name in SIZE_INPUTS:
                kept.append([name, value])
            elif isinstance(value, str) and value.lower().endswith(MODEL_SUFFIXES):
                kept.append([name, value])
        parts.append([str(step.get("class_type")), kept])
    if not parts:
        return None
    parts.sort(key=lambda part: json.dumps(part, sort_keys=True))
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def work(prompt):
    """Pixels per frame times batch: the size that the time of a run grows with, or None."""
    if not isinstance(prompt, dict):
        return None
    pixels, batch = 0, 1
    for step in prompt.values():
        inputs = (step.get("inputs") or {}) if isinstance(step, dict) else {}
        width, height = inputs.get("width"), inputs.get("height")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
               for value in (width, height)):
            pixels = max(pixels, width * height)
        count = inputs.get("batch_size")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            batch = max(batch, count)
    return pixels * batch if pixels else None


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


def _plural(count, word):
    return f"{count} {word}{'s' if count != 1 else ''}"


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

    @staticmethod
    def _valid(run):
        return isinstance(run, dict) and isinstance(run.get("seconds"), (int, float))

    def runs(self, sig):
        record = self.load().get(sig) or {}
        return [run for run in record.get("runs") or () if self._valid(run)]

    def runs_of_shape(self, shape):
        """Runs of the same workflow at any size."""
        found = []
        for record in self.load().values():
            if isinstance(record, dict):
                found += [run for run in record.get("runs") or () if self._valid(run) and run.get("shape") == shape]
        return found

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
    def __init__(self, job_id, prompt, started, partial):
        self.id, self.started, self.partial = job_id, started, partial
        self.sig, self.shape, self.work = signature(prompt), signature(prompt, sizes=False), work(prompt)
        self.bars = []          # dicts: total, step, start, seen, remaining, elapsed, done, length
        self.cursor = None      # timestamp of the newest log line already read
        self.primed = False     # the log lines from before this job have been skipped


class Estimator:
    """Follows one job at a time for a RunTracker. Every network failure only weakens the estimate."""

    def __init__(self, store=None):
        self.store = store if store is not None else TimingStore()
        self.logs_ok = True     # False once the server shows it has no log route
        self.job = None
        self.ended = {}         # job id -> Job, waiting for its /history result
        self.past = []          # finished runs from ComfyUI's /history: id, sig, shape, work, seconds

    # --- following the job -------------------------------------------------------------------
    def begin(self, fetch, job_id, prompt, partial, now):
        self.job = Job(job_id, prompt, now, partial)
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
        self.store.add(job.sig, {"id": str(job_id), "seconds": round(seconds, 2), "bars": bars,
                                 "shape": job.shape, "work": job.work})

    def _read_history(self, fetch):
        try:
            history = fetch(f"/history?max_items={HISTORY_ITEMS}") or {}
        except Exception:  # Optional: without it the estimate starts from runs watched here.
            return
        past = []
        for job_id, entry in history.items() if isinstance(history, dict) else ():
            if not isinstance(entry, dict):
                continue
            prompt = entry.get("prompt")
            prompt = prompt[2] if isinstance(prompt, (list, tuple)) and len(prompt) > 2 else None
            seconds = history_seconds(entry)
            if prompt is not None and seconds is not None:
                past.append({"id": str(job_id), "sig": signature(prompt), "shape": signature(prompt, sizes=False),
                             "work": work(prompt), "seconds": seconds})
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
        same = self._totals(job.sig, "sig")
        other = {key: run for key, run in self._totals(job.shape, "shape").items() if key not in same}
        left, basis, profile = None, "", None
        if not job.partial:  # a job already running when watching began has no known start
            stored = self.store.runs(job.sig) if job.sig else []
            profile, exact = self._profile(stored, job), True
            if profile is None and job.shape:
                profile, exact = self._profile(self.store.runs_of_shape(job.shape), job), False
            if profile is not None:
                left = self._from_profile(profile, job, now, live, exact)
                basis = (f"from {_plural(len(same), 'earlier run')}" if exact
                         else f"scaled from {_plural(len(other) or 1, 'run')} at another size")
            elif same:
                left = statistics.median(run["seconds"] for run in same.values()) - elapsed
                basis = f"from {_plural(len(same), 'earlier run')}"
            elif other and job.work:
                # Part of a run does not grow with size (loading, decoding), so only the runs
                # closest in size are scaled: those within twice or half the pixels, else the nearest.
                sized = sorted((run for run in other.values() if run.get("work")),
                               key=lambda run: abs(math.log(run["work"] / job.work)))
                near = ([run for run in sized if abs(math.log(run["work"] / job.work)) <= math.log(2)]
                        or [run for run in sized if sized and run["work"] == sized[0]["work"]])
                scaled = [run["seconds"] * job.work / run["work"] for run in near]
                if scaled:
                    left = statistics.median(scaled) - elapsed
                    basis = f"rough: scaled from {_plural(len(scaled), 'run')} at another size"
            if left is not None and live is not None:
                left = max(left, live)
        step = ""
        if bar is not None:
            count = len(profile["bars"]) if profile is not None else None
            sampler = f"sampler {len(job.bars)}" + (f" of {count}" if count and count >= len(job.bars) else "")
            step = f"{sampler} · step {bar['step']}/{bar['total']}"
        if left is None:
            if live is None:
                if job.partial:
                    reason = "No estimate: the job was already running when watching began"
                elif bar is not None:
                    reason = "Starting this sampler"
                else:
                    reason = "No estimate yet: first run of this workflow"
                return " · ".join(part for part in (reason, step) if part), ""
            text = f"~{_clock(live)} left in this sampler · {step}"
            return text, f"~{_clock(live)} sampler"
        if left <= 0:
            return " · ".join(part for part in ("Running longer than before", step, basis) if part), "over"
        ends = time.strftime("%H:%M", time.localtime(time.time() + left))
        text = " · ".join(part for part in (f"~{_clock(left)} left (about {ends})", step, basis) if part)
        return text, f"~{_clock(left)} left"

    def _totals(self, key, field):
        """Finished runs matching this job by signature or shape, from history and runs watched here."""
        if not key:
            return {}
        found = {run["id"]: run for run in self.past if run.get(field) == key}
        stored = self.store.runs(key) if field == "sig" else self.store.runs_of_shape(key)
        for index, run in enumerate(stored):
            found[run.get("id") or f"watched-{index}"] = run
        return found

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
        """The samplers of the median remembered run, when they match this job's samplers so far."""
        timed = [run for run in runs if isinstance(run.get("bars"), list) and run["bars"]]
        if not timed:
            return None
        timed.sort(key=lambda run: run["seconds"])
        run = timed[len(timed) // 2]
        bars = [bar for bar in run["bars"] if isinstance(bar, list) and len(bar) == 3]
        if not bars or len(job.bars) > len(bars) or any(seen["total"] != bars[index][0]
                                                        for index, seen in enumerate(job.bars)):
            return None
        return {"bars": bars, "seconds": run["seconds"], "work": run.get("work")}

    @staticmethod
    def _from_profile(profile, job, now, live, exact):
        bars, total_seconds = profile["bars"], profile["seconds"]
        size = 1.0 if exact or not (job.work and profile.get("work")) else job.work / profile["work"]
        # How fast this job runs compared with the remembered one: from the newest sampler with a
        # finished step, so a sampler that is just starting still has a speed.
        speed = None
        for index in range(len(job.bars) - 1, -1, -1):
            seen = job.bars[index]
            step_time = (seen["length"] / seen["total"] if seen["done"]
                         else seen["elapsed"] / seen["step"] if seen.get("step") and seen.get("elapsed") else None)
            known_total, _, known_length = bars[index]
            if step_time and known_length:
                speed = step_time / (known_length / known_total)
                break
        if speed is None:
            speed = size
        low, high = SAME_SIZE_SPEED if exact else OTHER_SIZE_SPEED
        speed = min(max(speed, low), high)
        if not job.bars:
            return total_seconds * speed - (now - job.started)
        index = len(job.bars) - 1
        _, start, length = bars[index]
        bar = job.bars[-1]
        after = total_seconds - (start + length)  # later samplers, decoding and saving
        if not bar["done"]:
            if live is None:
                live = length * speed * (1 - bar.get("step", 0) / bar["total"])
            return live + after * speed
        return after * speed - (now - (bar["start"] + bar["length"]))


def _clock(seconds):
    seconds = max(int(round(seconds)), 0)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

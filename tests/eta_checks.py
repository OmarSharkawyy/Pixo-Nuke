"""Checks for the time-left estimate: log parsing, workflow matching, learning, and the estimate itself."""
import json
from pathlib import Path
import tempfile

STARTED_MS = 1_700_000_000_000
BAR_LINE = "\r 67%|██████      | {step}/{total} [{elapsed}<{left}, 90.28s/it]"


def workflow(width=768, text="a skater", seed=1, model="ltx-2.5.safetensors"):
    return {
        "1": {"class_type": "CheckpointLoader", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "EmptyLatent", "inputs": {"width": width, "height": 448, "length": 49,
                                                      "batch_size": 1, "tiled": True}},
        "3": {"class_type": "TextEncode", "inputs": {"text": text, "clip": ["1", 1]}},
        "4": {"class_type": "Sampler", "inputs": {"seed": seed, "steps": 8, "model": ["1", 0]}},
        "5": {"class_type": "LoadVideo", "inputs": {"file": f"{seed}.mp4"}},
    }


def history_entry(prompt, seconds, status="success"):
    finish = "execution_success" if status == "success" else "execution_error"
    return {"prompt": [0, "id", prompt, {}, []],
            "status": {"status_str": status, "messages": [
                ["execution_start", {"timestamp": STARTED_MS}],
                [finish, {"timestamp": STARTED_MS + int(seconds * 1000)}]]}}


class Comfy:
    """A fake ComfyUI: queue, history, and a console log that grows as the test goes."""

    def __init__(self):
        self.running, self.history, self.lines, self.second = [], {}, [], 0
        self.logs_route = True

    def log(self, text):
        self.second += 1
        self.lines.append({"t": f"2026-11-19T05:{self.second // 60:02d}:{self.second % 60:02d}.000001",
                           "m": text})

    def bar(self, step, total, elapsed, left):
        self.log(BAR_LINE.format(step=step, total=total, elapsed=elapsed, left=left))

    def fetch(self, route):
        from pixo import client
        if route == "/queue":
            return {"queue_running": [[0, job, prompt, {}, []] for job, prompt in self.running],
                    "queue_pending": []}
        if route.startswith("/history?"):
            return self.history
        if route == "/internal/logs/raw":
            if not self.logs_route:
                raise RuntimeError("ComfyUI answered HTTP 404 on /internal/logs/raw.")
            return {"entries": list(self.lines[-300:]), "size": {"cols": 120, "rows": 30}}
        job = route.rsplit("/", 1)[1]
        if job in self.history:
            return {job: self.history[job]}
        raise client.TransientError("not there yet")


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def left_seconds(text):
    """The mm:ss after '~' in an estimate, in seconds."""
    minutes, seconds = text.split("~", 1)[1].split(" ", 1)[0].split(":")[-2:]
    return int(minutes) * 60 + int(seconds)


def eta_test(eta, tracker_module):
    # Progress bars as ComfyUI's samplers print them.
    assert eta.parse_bar(BAR_LINE.format(step=2, total=3, elapsed="03:00", left="01:30")) == (2, 3, 180, 90)
    assert eta.parse_bar(" 0%|          | 0/8 [00:00<?, ?it/s]") == (0, 8, 0, None)
    assert eta.parse_bar("50%|#####     | 4/8 [1:02:03<1:00:00, 9.1s/it]") == (4, 8, 3723, 3600)
    assert eta.parse_bar("\r 10%| 1/10 [00:01<00:09]\r 20%| 2/10 [00:02<00:08]") == (2, 10, 2, 8)
    for text in ("", None, "Requested to load LTXAV", "5/3 [00:01<00:02]", "0/0 [00:00<00:00]"):
        assert eta.parse_bar(text) is None, text

    # The same workflow means the same nodes, models and sizes; prompts, seeds and inputs are ignored.
    base = eta.signature(workflow())
    assert base and base == eta.signature(workflow(text="new prompt", seed=99))
    assert base != eta.signature(workflow(width=1920))
    assert base != eta.signature(workflow(model="other.safetensors"))
    for broken in (None, {}, [], {"1": "junk"}):
        assert eta.signature(broken) is None, broken
    assert eta.history_seconds(history_entry(workflow(), 12.5)) == 12.5
    assert eta.history_seconds(history_entry(workflow(), 12.5, "error")) is None

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sub" / "timings.json"
        store = eta.TimingStore(path)
        assert store.runs("x") == []
        for index in range(eta.KEEP_RUNS + 3):
            store.add("x", {"id": str(index), "seconds": index})
        assert [run["seconds"] for run in store.runs("x")] == list(range(3, eta.KEEP_RUNS + 3))
        path.write_text("not json", encoding="utf-8")
        assert store.runs("x") == []
        for index in range(eta.KEEP_WORKFLOWS + 5):
            store.add(f"w{index}", {"seconds": 1})
        assert len(store.load()) == eta.KEEP_WORKFLOWS and "w0" not in store.load()
        path.unlink()

        comfy, clock = Comfy(), Clock()
        prompt = workflow()

        def run_job(job_id, *, stage1=8, stage2=3, step_seconds=10, tail=20, check=None, prompt=prompt):
            """One job: loading, a sampler of stage1 steps, a sampler of stage2 steps, then decoding."""
            comfy.running = [(job_id, prompt)]
            tracker.poll(comfy.fetch)
            clock.now += 30                               # loading models
            comfy.log("Requested to load LTXAV")
            for total in (stage1, stage2):
                comfy.bar(0, total, "00:00", "?")
                for step in range(1, total + 1):
                    clock.now += step_seconds
                    done = step * step_seconds
                    rest = (total - step) * step_seconds
                    comfy.bar(step, total, f"{done // 60:02d}:{done % 60:02d}", f"{rest // 60:02d}:{rest % 60:02d}")
                    tracker.poll(comfy.fetch)
                    if check:
                        check(total, step)
            clock.now += tail
            tracker.poll(comfy.fetch)
            comfy.running = []
            seconds = 30 + (stage1 + stage2) * step_seconds + tail
            comfy.history[job_id] = history_entry(prompt, seconds)
            tracker.poll(comfy.fetch)
            return seconds

        # Old lines in the log belong to earlier jobs and must not count.
        comfy.bar(3, 3, "00:30", "00:00")
        tracker = tracker_module.RunTracker(clock, eta.Estimator(store))
        tracker.poll(comfy.fetch)
        seen = []

        def first_check(total, step):
            view = tracker.view()
            seen.append(view.estimate)
            if total == 8 and step == 4:
                # Never ran before: only the sampler that is running can be estimated.
                assert "left in this sampler" in view.estimate and left_seconds(view.estimate) == 40, view
                assert "sampler 1 · step 4/8" in view.estimate, view
                assert view.label.endswith("~00:40 sampler"), view

        first = run_job("job-1", check=first_check)
        runs = store.runs(eta.signature(prompt))
        assert len(runs) == 1 and runs[0]["seconds"] == first, runs
        assert [bar[0] for bar in runs[0]["bars"]] == [8, 3], runs
        assert tracker.view().estimate == "", "The estimate must clear when the job ends."

        # The second time, the whole job is estimated from the first run plus the live steps.
        checked = []

        def second_check(total, step):
            view = tracker.view()
            if total == 8 and step == 4:
                expected = first - (30 + 4 * 10)
                assert abs(left_seconds(view.estimate) - expected) <= 3, (view.estimate, expected)
                assert "sampler 1 of 2 · step 4/8" in view.estimate and "from 1 earlier run" in view.estimate, view
                assert view.label.endswith("left"), view
                checked.append(1)
            if total == 3 and step == 1:
                expected = 2 * 10 + 20
                assert abs(left_seconds(view.estimate) - expected) <= 3, (view.estimate, expected)
                assert "sampler 2 of 2" in view.estimate, view
                checked.append(2)

        run_job("job-2", check=second_check)
        assert checked == [1, 2], checked

        # A slower machine: the remaining time scales with the live speed of the steps.
        def slow_check(total, step):
            if total == 8 and step == 4:
                view = tracker.view()
                expected = (8 - 4) * 20 + (first - (30 + 8 * 10)) * 2
                assert abs(left_seconds(view.estimate) - expected) <= 5, (view.estimate, expected)
                checked.append(3)

        run_job("job-3", step_seconds=20, check=slow_check)
        assert checked[-1] == 3

        # Another size of the same workflow: before any step, a rough estimate scaled by pixels;
        # once steps run, the remembered samplers scaled by their real speed.
        wide = workflow(width=1536)
        assert eta.signature(wide) != eta.signature(prompt)
        assert eta.signature(wide, sizes=False) == eta.signature(prompt, sizes=False)
        assert eta.work(wide) == 2 * eta.work(prompt)

        def wide_check(total, step):
            view = tracker.view()
            if total == 8 and step == 2:
                # 6 steps of 25 s left here, then the rest of the run at 2.5 times the remembered speed.
                expected = 6 * 25 + (first - (30 + 8 * 10)) * 2.5
                assert abs(left_seconds(view.estimate) - expected) <= 6, (view.estimate, expected)
                assert "scaled from" in view.estimate and "at another size" in view.estimate, view
                checked.append(4)
            if total == 3 and step == 0:
                assert False, "step 0 lines are only seen between polls"

        comfy.running = [("job-4", wide)]
        tracker.poll(comfy.fetch)
        view = tracker.view()
        assert "left" in view.estimate and "another size" in view.estimate, view
        comfy.running = []
        tracker.poll(comfy.fetch)
        run_job("job-4b", step_seconds=25, check=wide_check, prompt=wide)
        assert checked[-1] == 4

        # A sampler that has only just started still gets a whole-job estimate from the last one's speed.
        tracker.poll(comfy.fetch)
        comfy.running = [("job-5", prompt)]
        tracker.poll(comfy.fetch)
        clock.now += 30
        for step in range(1, 9):
            clock.now += 10
            comfy.bar(step, 8, f"00:{step * 10:02d}" if step < 6 else f"01:{step * 10 - 60:02d}",
                      f"00:{(8 - step) * 10:02d}" if (8 - step) * 10 < 60 else f"01:{(8 - step) * 10 - 60:02d}")
        comfy.bar(0, 3, "00:00", "?")
        tracker.poll(comfy.fetch)
        view = tracker.view()
        assert "sampler 2 of 2 · step 0/3" in view.estimate and "left (about" in view.estimate, view
        assert abs(left_seconds(view.estimate) - 50) <= 3, view.estimate
        comfy.running = []
        tracker.poll(comfy.fetch)

        # Without the log route, earlier runs in ComfyUI's /history still give a whole-job estimate.
        path.unlink()
        comfy2, clock2 = Comfy(), Clock()
        comfy2.logs_route = False
        comfy2.history = {"old-a": history_entry(prompt, 300), "old-b": history_entry(prompt, 340),
                          "old-c": history_entry(prompt, 9999, "error")}
        estimator = eta.Estimator(eta.TimingStore(path))
        tracker2 = tracker_module.RunTracker(clock2, estimator)
        tracker2.poll(comfy2.fetch)
        comfy2.running = [("job-x", prompt)]
        tracker2.poll(comfy2.fetch)
        clock2.now += 100
        view = tracker2.view()
        assert estimator.logs_ok is False
        assert abs(left_seconds(view.estimate) - 220) <= 2 and "from 2 earlier runs" in view.estimate, view

        # Another size with only ComfyUI's history: a rough estimate scaled by the pixel count.
        comfy2.running = []
        tracker2.poll(comfy2.fetch)
        comfy2.running = [("job-y", workflow(width=1920))]
        tracker2.poll(comfy2.fetch)
        view = tracker2.view()
        assert abs(left_seconds(view.estimate) - 800) <= 2 and view.estimate.endswith(
            "rough: scaled from 2 runs at another size"), view

        # A different workflow altogether has nothing to go on.
        comfy2.running = []
        tracker2.poll(comfy2.fetch)
        other = workflow(model="other.safetensors")
        comfy2.running = [("job-o", other)]
        tracker2.poll(comfy2.fetch)
        assert tracker2.view().estimate.startswith("No estimate yet: first run of this workflow"), tracker2.view()

        # Already running when watching began: its start is unknown, so no whole-job estimate.
        tracker3 = tracker_module.RunTracker(Clock(), eta.Estimator(eta.TimingStore(path)))
        comfy2.running = [("late", prompt)]
        tracker3.poll(comfy2.fetch)
        assert tracker3.view().estimate.startswith("No estimate: the job was already running"), tracker3.view()
        assert not path.exists() or eta.signature(prompt) not in json.loads(path.read_text())

        # Running past every earlier run says so instead of showing a negative time.
        comfy2.running = []
        tracker2.poll(comfy2.fetch)
        comfy2.running = [("job-z", prompt)]
        tracker2.poll(comfy2.fetch)
        tracker2.now = lambda: clock2.now + 1000
        assert tracker2.view().estimate.startswith("Running longer than before"), tracker2.view()

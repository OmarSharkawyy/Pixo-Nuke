"""Checks for the system monitor: parsers, sampling, the meter panel, and the Nuke controls."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import types

from timer_checks import Knob, Node, wait_for

PROC_STAT = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8\n"
MEMINFO = "MemTotal:       16384000 kB\nMemFree:          100000 kB\nMemAvailable:    4096000 kB\nBuffers: 5 kB\n"
STATS = {"system": {"os": "posix", "ram_total": 32 * 1024 ** 3, "ram_free": 8 * 1024 ** 3},
         "devices": [{"vram_total": 24 * 1024 ** 3, "vram_free": 6 * 1024 ** 3},
                     {"vram_total": 8 * 1024 ** 3, "vram_free": 8 * 1024 ** 3}]}


def close(value, expected, tolerance=0.01):
    assert abs(value - expected) <= tolerance, (value, expected)


def sysinfo_test(sysinfo):
    # /proc parsing: idle = idle + iowait, total = the first eight fields.
    assert sysinfo.parse_proc_stat(PROC_STAT) == (850, 1000)
    assert sysinfo.parse_proc_stat("cpu  1 2 3 4") == (4, 10)
    for broken in ("", "cpu", "cpu a b c d e"):
        assert sysinfo.parse_proc_stat(broken) is None, broken
    assert sysinfo.parse_meminfo(MEMINFO) == ((16384000 - 4096000) * 1024, 16384000 * 1024)
    assert sysinfo.parse_meminfo("MemTotal: 100 kB\nMemFree: 40 kB\n") == (60 * 1024, 100 * 1024)
    assert sysinfo.parse_meminfo("nonsense") is None

    # NVIDIA output: normal, unsupported fields, several GPUs, empty.
    found = sysinfo.parse_nvidia_smi("17, 9000, 12000, 42\n")
    close(found["gpu"].percent, 17)
    close(found["vram"].percent, 75)
    assert found["vram"].detail == "8.8 / 11.7 GB", found["vram"].detail
    close(found["temp"].percent, 42)
    partial = sysinfo.parse_nvidia_smi("[N/A], [N/A], [N/A], 55")
    assert set(partial) == {"temp"}
    two = "10, 1000, 2000, 30\n90, 3000, 4000, 80\n"
    close(sysinfo.parse_nvidia_smi(two, 1)["gpu"].percent, 90)
    close(sysinfo.parse_nvidia_smi(two, 7)["gpu"].percent, 90)   # out of range uses the last GPU
    close(sysinfo.parse_nvidia_smi(two, -1)["gpu"].percent, 10)
    assert sysinfo.parse_nvidia_smi("") == {} and sysinfo.parse_nvidia_smi("\n  \n") == {}
    assert "vram" not in sysinfo.parse_nvidia_smi("5, 100, 0, 30")   # total of zero must not divide

    # ComfyUI /system_stats.
    found = sysinfo.server_metrics(STATS)
    close(found["ram"].percent, 75)
    close(found["vram"].percent, 75)
    close(sysinfo.server_metrics(STATS, 1)["vram"].percent, 0)
    assert sysinfo.server_metrics(STATS, 9)["vram"].percent == 0   # clamps to the last device
    for broken in (None, {}, {"system": None, "devices": "x"}, {"system": {"ram_total": 0, "ram_free": 0}},
                   {"devices": [{"vram_total": "a", "vram_free": 1}]}, {"devices": [3]}):
        assert sysinfo.server_metrics(broken) == {}, broken


def cpu_test(sysinfo):
    saved = sysinfo.psutil, sysinfo.cpu_times, sysinfo.memory
    try:
        sysinfo.psutil = None
        readings = iter([(0, 0), (50, 100), (50, 100), (150, 200), None])
        sysinfo.cpu_times = lambda: next(readings)
        meter = sysinfo.CpuMeter()
        assert meter.read() is None                    # the first call has nothing to compare with
        close(meter.read(), 50)                        # 50 idle of 100 total
        assert meter.read() is None                    # no time passed
        close(meter.read(), 0)                         # all 100 ticks were idle
        assert meter.read() is None                    # unreadable
        sysinfo.cpu_times = lambda: (200, 100)         # counters that go backwards stay in range
        meter.previous = (0, 1000)
        assert meter.read() is None

        sysinfo.psutil = types.SimpleNamespace(
            cpu_percent=lambda interval=None: 33.0,
            virtual_memory=lambda: types.SimpleNamespace(total=1000, available=250))
        assert sysinfo.CpuMeter().read() == 33.0
        assert sysinfo.memory() == (750, 1000)
    finally:
        sysinfo.psutil, sysinfo.cpu_times, sysinfo.memory = saved


def nvidia_test(sysinfo):
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as folder:
        def script(name, body):
            path = Path(folder, name)
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            return str(path)

        good = script("good", 'echo "17, 9000, 12000, 42"')
        bad = script("bad", "exit 9")
        slow = script("slow", "sleep 5")
        assert sysinfo.read_nvidia(good)["gpu"].percent == 17
        assert sysinfo.read_nvidia(bad) is None
        assert sysinfo.read_nvidia(slow, timeout=0.3) is None
        assert sysinfo.read_nvidia(str(Path(folder, "missing"))) is None

        # A failing nvidia-smi is left alone for a while instead of being started every second.
        calls, clock = [], [100.0]
        original = sysinfo.read_nvidia
        sysinfo.read_nvidia = lambda path, index=0, timeout=3: (calls.append(path), None)[1]
        try:
            sampler = sysinfo.LocalSampler(now=lambda: clock[0])
            sampler.nvidia = bad
            for _ in range(10):
                assert sampler._gpu() == {}
            assert len(calls) == sysinfo.NVIDIA_FAILURES, calls
            clock[0] += sysinfo.NVIDIA_PAUSE + 1
            sampler._gpu()
            assert len(calls) == sysinfo.NVIDIA_FAILURES + 1
            # One success resets the failure count.
            sysinfo.read_nvidia = lambda path, index=0, timeout=3: {"gpu": sysinfo.Metric(5.0, "x")}
            clock[0] += sysinfo.NVIDIA_PAUSE + 1
            assert "gpu" in sampler._gpu() and sampler.failures == 0
            sampler.nvidia = None
            assert sampler._gpu() == {}
        finally:
            sysinfo.read_nvidia = original

    # A whole local sample on this machine returns plain Metric objects and never raises.
    sampler = sysinfo.LocalSampler()
    sampler.nvidia = None
    sampler.sample()
    found = sampler.sample()
    for metric in found.values():
        assert 0 <= metric.percent <= 100 and isinstance(metric.detail, str)


def monitor_test(monitor, sysinfo, client):
    Metric = sysinfo.Metric
    state = monitor.MonitorState()
    assert state.snapshot().running is False and state.snapshot().latest == {}
    state.update({"cpu": Metric(10.0, "a")}, "note")
    state.update({"ram": Metric(20.0, "b")}, "note2")
    shot = state.snapshot()
    assert shot.history["cpu"] == [10.0, None] and shot.history["ram"] == [None, 20.0]
    assert shot.latest == {"ram": Metric(20.0, "b")} and shot.note == "note2"
    shot.history["cpu"].append(99)   # the snapshot is a copy
    assert state.snapshot().history["cpu"] == [10.0, None]
    for _ in range(monitor.HISTORY_LENGTH + 20):
        state.update({"cpu": Metric(1.0, "")}, "")
    assert len(state.snapshot().history["cpu"]) == monitor.HISTORY_LENGTH

    assert monitor.resolve_source("auto", "http://127.0.0.1:8188") == "local"
    assert monitor.resolve_source("auto", "http://localhost:8188") == "local"
    assert monitor.resolve_source("auto", "http://render-box:8188") == "server"
    assert monitor.resolve_source("local", "http://render-box:8188") == "local"
    assert monitor.resolve_source("server", "http://127.0.0.1:8188") == "server"

    class Local:
        def __init__(self, found):
            self.found = found

        def sample(self):
            return dict(self.found)

    def fetch(route):
        assert route == "/system_stats"
        return STATS

    def down(route):
        raise client.TransientError("down")

    both = {"cpu": Metric(5.0, "c"), "ram": Metric(6.0, "r"), "gpu": Metric(7.0, "g"), "vram": Metric(8.0, "v")}
    found, note = monitor.sample_once("local", Local(both), fetch)
    assert found == both and note == "This computer"
    # Without a local GPU, VRAM comes from ComfyUI; everything else stays local.
    found, note = monitor.sample_once("local", Local({"cpu": Metric(5.0, "c")}), fetch)
    assert set(found) == {"cpu", "vram"} and "VRAM from ComfyUI" in note
    found, note = monitor.sample_once("local", Local({"cpu": Metric(5.0, "c")}), down)
    assert set(found) == {"cpu"} and note == "This computer"
    # A server only reports RAM and VRAM, and may be unreachable.
    found, note = monitor.sample_once("server", Local(both), fetch)
    assert set(found) == {"ram", "vram"} and "RAM and VRAM only" in note
    assert monitor.sample_once("server", Local(both), down) == ({}, "ComfyUI is not reachable")
    assert monitor.sample_once("server", Local(both), lambda route: {})[1] == "ComfyUI did not report RAM or VRAM"

    # The loop samples, survives a failing reading, reports to on_sample, and stops promptly.
    stop, seen, count = threading.Event(), [], [0]

    class Flaky:
        def sample(self):
            count[0] += 1
            if count[0] == 2:
                raise ValueError("boom")
            return {"cpu": Metric(float(count[0]), "")}

    def on_sample(metrics, note):
        seen.append((dict(metrics), note))
        if len(seen) == 4:
            stop.set()

    loop_state = monitor.MonitorState()
    thread = threading.Thread(target=monitor.run, args=(loop_state, "http://127.0.0.1:1", "local", 0, stop),
                              kwargs={"interval": 0.01, "fetch": down, "local": Flaky(), "on_sample": on_sample})
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and len(seen) == 4
    assert seen[1] == ({}, "Read error: boom"), seen[1]
    assert seen[2][0]["cpu"].percent == 3.0
    assert loop_state.snapshot().running is False
    assert loop_state.snapshot().history["cpu"] == [1.0, None, 3.0, 4.0]


def widget_test(widget):
    for name, value, expected in (("cpu", 59.9, widget.OK), ("cpu", 60, widget.WARM), ("cpu", 84.9, widget.WARM),
                                  ("cpu", 85, widget.HOT), ("temp", 64, widget.OK), ("temp", 65, widget.WARM),
                                  ("temp", 80, widget.HOT)):
        assert widget.level_color(name, value) == expected, (name, value)
    assert widget.format_value("cpu", 31.4) == "31%" and widget.format_value("temp", 42.2) == "42°C"
    assert widget.format_value("gpu", None) == "n/a"
    assert [widget.fill_fraction(v) for v in (None, -5, 50, 250)] == [0.0, 0.0, 0.5, 1.0]

    for width in (300, 360, 600, 1200):
        previous_bottom = widget.HEADER
        for row in range(len(widget.ROWS)):
            rects = widget.geometry(width, row)
            label, bar, spark, value = (rects[key] for key in ("label", "bar", "spark", "value"))
            assert label[0] + label[2] <= bar[0] and bar[0] + bar[2] <= spark[0] and spark[0] + spark[2] <= value[0]
            assert value[0] + value[2] <= width and bar[2] >= 20
            assert label[1] == previous_bottom
            previous_bottom += widget.ROW
    assert widget.panel_height() > widget.HEADER + widget.ROW * len(widget.ROWS)


def render(widget, state, width=420):
    """Paint the panel offscreen and return the image."""
    app = widget.QtWidgets.QApplication.instance() or widget.QtWidgets.QApplication([])
    panel = widget.MeterPanel(state=state)
    panel.resize(width, widget.panel_height())
    palette = panel.palette()
    palette.setColor(panel.backgroundRole(), widget.QtGui.QColor("#262626"))
    palette.setColor(widget.QtGui.QPalette.WindowText, widget.QtGui.QColor("#dddddd"))
    panel.setPalette(palette)
    panel.setAutoFillBackground(True)
    image = widget.QtGui.QImage(panel.size(), widget.QtGui.QImage.Format_ARGB32)
    image.fill(widget.QtGui.QColor("#262626"))
    painter = widget.QtGui.QPainter(image)
    panel.render(painter, widget.QtCore.QPoint(0, 0))
    painter.end()
    return app, panel, image


def count_color(image, rect, color):
    wanted = widget_color(color)
    hits = 0
    for x in range(int(rect[0]), int(rect[0] + rect[2])):
        for y in range(int(rect[1]), int(rect[1] + rect[3])):
            pixel = image.pixelColor(x, y)
            if abs(pixel.red() - wanted[0]) < 18 and abs(pixel.green() - wanted[1]) < 18 \
                    and abs(pixel.blue() - wanted[2]) < 18:
                hits += 1
    return hits


def widget_color(text):
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def render_test(widget, monitor, sysinfo):
    Metric = sysinfo.Metric
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    state = monitor.MonitorState()
    app, panel, image = render(widget, state)
    # Not monitoring: no colored meter fill anywhere.
    for color in (widget.OK, widget.WARM, widget.HOT):
        assert count_color(image, (0, 0, image.width(), image.height()), color) == 0, color

    def value_ink(image, row):
        x, y, w, h = widget.geometry(image.width(), row)["value"]
        background = image.pixelColor(0, 0)
        return sum(image.pixelColor(i, j) != background
                   for i in range(int(x), int(x + w)) for j in range(int(y), int(y + h)))
    idle_ink = value_ink(image, 0)
    state.set_running(True, "This computer")   # running with no readings yet shows n/a
    no_reading_ink = value_ink(render(widget, state)[2], 0)
    assert 0 < idle_ink < no_reading_ink, (idle_ink, no_reading_ink)
    for _ in range(5):
        state.update({"cpu": Metric(95.0, "c"), "ram": Metric(70.0, "r"), "gpu": Metric(10.0, "g"),
                      "vram": Metric(10.0, "v")}, "This computer")
    app, panel, image = render(widget, state)
    for name, color, level in (("cpu", widget.HOT, 95), ("ram", widget.WARM, 70), ("gpu", widget.OK, 10)):
        row = [key for key, _ in widget.ROWS].index(name)
        bar = widget.geometry(image.width(), row)["bar"]
        expected = bar[2] * level / 100 * widget.BAR_H   # filled area in pixels
        hits = count_color(image, bar, color)
        assert 0.6 * expected <= hits <= 1.1 * expected, (name, hits, expected)
    # The temperature row has no reading, so its bar stays empty.
    row = [key for key, _ in widget.ROWS].index("temp")
    bar = widget.geometry(image.width(), row)["bar"]
    for color in (widget.OK, widget.WARM, widget.HOT):
        assert count_color(image, bar, color) == 0
    # A sparkline is drawn next to a meter that has history.
    def inked(row):
        x, y, w, h = widget.geometry(image.width(), row)["spark"]
        background = image.pixelColor(0, 0)
        return sum(image.pixelColor(i, j) != background
                   for i in range(int(x), int(x + w)) for j in range(int(y), int(y + h)))
    assert inked(0) >= 5, ("cpu sparkline missing", inked(0))
    assert inked([key for key, _ in widget.ROWS].index("temp")) == 0, "empty history must draw nothing"

    panel.refresh()
    assert "CPU: c" in panel.toolTip() and "TEMP" not in panel.toolTip()
    # Showing and hiding the panel starts and stops its refresh timer.
    panel.show()
    assert panel.timer.isActive()
    panel.hide()
    assert not panel.timer.isActive()
    panel.close()

    # No state for a node yet: it paints without error.
    widget.MeterPanel("NoSuchNode").grab()


def monitor_ui_test(nuke, monitor_ui, monitor):
    stats = {"value": STATS}

    class ComfyUI(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps(stats["value"]).encode()
            self.send_response(200 if self.path == "/system_stats" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    assert [monitor_ui.source_mode(t) for t in ("Auto", "Local", "Server", "This computer", "ComfyUI server", "", None)] == \
        ["auto", "local", "server", "local", "server", "auto", "auto"]
    assert monitor_ui.short_label({}) == monitor_ui.NODE_LABEL
    assert monitor_ui.short_label({"ram": types.SimpleNamespace(percent=74.6),
                                   "cpu": types.SimpleNamespace(percent=31.2)}) == "CPU 31 · RAM 75"

    monitor_ui.SAMPLE_SECONDS = 0.03
    messages = []
    nuke.message = messages.append
    script = types.SimpleNamespace(flag=False)
    script.modified = lambda: script.flag
    script.setModified = lambda value: setattr(script, "flag", value)
    nuke.root = lambda: script
    graph = {}
    nuke.toNode = graph.get

    class DirtyKnob(Knob):
        """Like Nuke, changing a value marks the script as having unsaved changes."""

        def setValue(self, value):
            script.flag = True
            super().setValue(value)

    def make(name, url, source="Server", gpu="0"):
        node = Node(name, server=url, source=source, gpu_index=gpu, status="Not monitoring", watch_token="",
                    label="Pixo Pulse")
        node.knobs = {key: DirtyKnob(knob.data) for key, knob in node.knobs.items()}
        graph[name] = node
        return node

    with ThreadingHTTPServer(("127.0.0.1", 0), ComfyUI) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            node = make("PixoPulse1", url)
            monitor_ui.start(node)
            monitor_ui.start(node)
            assert messages == ["This monitor is already running."], messages
            wait_for(lambda: node["label"].value() == "RAM 75", "label from server reading")
            assert node["status"].value().startswith("Monitoring")
            assert node["watch_token"].value()
            shot = monitor.STATES["PixoPulse1"].snapshot()
            assert shot.running and set(shot.latest) == {"ram", "vram"}
            assert script.flag is False, "The monitor marked the script as modified."
            script.flag = True   # real unsaved changes stay unsaved
            monitor_ui.show(node, "x", "y")
            assert script.flag is True
            script.flag = False

            stats["value"] = {"system": {"ram_total": 100, "ram_free": 50}}
            wait_for(lambda: node["label"].value() == "RAM 50", "label update")
            monitor_ui.stop(node)
            assert not monitor_ui.WATCHERS and node["status"].value() == "Stopped"
            assert node["label"].value() == monitor_ui.NODE_LABEL
            wait_for(lambda: not any(t.name == "PixoPulse" for t in threading.enumerate()), "thread to end")
            assert monitor.STATES["PixoPulse1"].snapshot().running is False
            monitor_ui.stop(node)
            assert messages[-1] == "This monitor is not running."

            # A bad URL or GPU number never starts a thread.
            monitor_ui.start(make("PixoPulse2", "ftp://nope"))
            assert "valid ComfyUI URL" in messages[-1] and not monitor_ui.WATCHERS
            monitor_ui.start(make("PixoPulse3", url, gpu="two"))
            assert not monitor_ui.WATCHERS

            # Deleting the node, or another script reusing its name, releases the watcher.
            node = make("PixoPulse4", url)
            monitor_ui.start(node)
            wait_for(lambda: node["status"].value().startswith("Monitoring"), "monitoring status")
            del graph["PixoPulse4"]
            wait_for(lambda: not monitor_ui.WATCHERS, "release after delete")
            node = make("PixoPulse5", url)
            monitor_ui.start(node)
            wait_for(lambda: node["status"].value().startswith("Monitoring"), "monitoring status")
            graph["PixoPulse5"] = other = Node("PixoPulse5", status="", watch_token="another",
                                                        label="")
            wait_for(lambda: not monitor_ui.WATCHERS, "release after rename")
            assert other["status"].value() == "" and other["label"].value() == ""
            wait_for(lambda: not any(t.name == "PixoPulse" for t in threading.enumerate()), "thread to end")

            # An unreachable server shows a note and keeps running until stopped.
            node = make("PixoPulse6", "http://127.0.0.1:9")
            monitor_ui.start(node)
            wait_for(lambda: "not reachable" in node["status"].value(), "unreachable note")
            monitor_ui.stop(node)
        finally:
            server.shutdown()
            for _, (_, stopper) in list(monitor_ui.WATCHERS.items()):
                stopper.set()
            monitor_ui.WATCHERS.clear()


def parse_gizmo(path):
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("Gizmo {") and text.rstrip().endswith("end_group")
    assert text.count("{") == text.count("}"), "unbalanced braces"
    knobs = re.findall(r"addUserKnob \{(\d+) (\w+)", text)
    names = [name for _, name in knobs]
    assert len(names) == len(set(names)), "duplicate knob names"
    return text, dict((name, int(kind)) for kind, name in knobs)


def package_test(root, nuke, timer_ui, monitor_ui):
    import runpy
    timer_text, timer_knobs = parse_gizmo(root / "PixoTimer.gizmo")
    monitor_text, monitor_knobs = parse_gizmo(root / "PixoPulse.gizmo")
    builtin = {"label"}
    for module, knobs in ((timer_ui, timer_knobs), (monitor_ui, monitor_knobs)):
        used = set(re.findall(r'node\["(\w+)"\]', Path(module.__file__).read_text(encoding="utf-8")))
        missing = used - set(knobs) - builtin
        assert not missing, (module.__name__, missing)
    for text, knobs in ((timer_text, timer_knobs), (monitor_text, monitor_knobs)):
        assert "watch_token" in knobs and knobs["start_watch"] == 22 and knobs["stop_watch"] == 22
        assert re.search(r"addUserKnob \{\d+ watch_token[^}]*\+INVISIBLE", text)
        assert text.count("name Output1") == 1
    assert monitor_knobs["meters"] == 52 and monitor_knobs["source"] == 4 and monitor_knobs["gpu_index"] == 3
    # The enum has the three items monitor_ui.source_mode recognises.
    items = re.search(r'addUserKnob \{4 source .*? M \{(.*?)\}\}', monitor_text).group(1)
    assert [monitor_ui.source_mode(item) for item in re.findall(r'"[^"]*"|\w+', items)] == ["auto", "local", "server"]
    assert "PixoTimer" == timer_ui.NODE_CLASS and "PixoPulse" == monitor_ui.NODE_CLASS
    assert re.search(r"^ name PixoTimer$", timer_text, re.M)
    assert re.search(r"^ name PixoPulse$", monitor_text, re.M)

    # Every Python snippet in a button, and the custom knob, imports and runs against the fake Nuke.
    snippets = re.findall(r' T "((?:[^"\\]|\\.)*)"', timer_text + monitor_text)
    assert len(snippets) >= 5, snippets
    for snippet in snippets:
        if snippet.startswith(("from pixo", "__import__")):
            compile(snippet, "<gizmo>", "eval" if snippet.startswith("__import__") else "exec")
    nuke.thisNode = lambda: Node("PixoPulse1")
    custom = next(s for s in snippets if s.startswith("__import__"))
    assert "pixo.knob" in custom
    knob_object = eval(custom)
    assert callable(knob_object.makeUI) and knob_object.updateValue() is None
    try:
        import pixo.widget  # noqa: F401  (needs PySide, which only Nuke may have)
    except ImportError:
        assert knob_object.panel is None and "No module named" in knob_object.error
    else:
        assert knob_object.error is None and knob_object.panel.node.fullName() == "PixoPulse1"
        assert type(knob_object.makeUI()).__name__ == "MeterPanel"
    # A broken meter panel, or no current node, must never stop the node from being created.
    saved = sys.modules.get("pixo.widget")
    sys.modules["pixo.widget"] = None
    nuke.thisNode = lambda: (_ for _ in ()).throw(RuntimeError("no node"))
    try:
        broken = eval(custom)
        assert broken.panel is None and broken.error and broken.node is None
        assert broken.updateValue() is None
    finally:
        nuke.thisNode = lambda: Node("PixoPulse1")
        if saved is not None:
            sys.modules["pixo.widget"] = saved
        else:
            sys.modules.pop("pixo.widget", None)

    # The menu is built once, as its own Pixo menu in the Nodes toolbar and the top menu bar,
    # and leaves other menus such as ComfyUI alone.
    class Menu:
        def __init__(self):
            self.items = {}
            self.commands = {}

        def findItem(self, name):
            return self.items.get(name)

        def addMenu(self, name, icon=None):
            self.items[name] = Menu()
            return self.items[name]

        def addCommand(self, name, command):
            self.commands[name] = command

    bars = {"Nodes": Menu(), "Nuke": Menu()}
    existing = bars["Nodes"].addMenu("ComfyUI")
    nuke.menu = lambda name: bars[name]
    created = []
    nuke.createNode = created.append
    for name in [m for m in sys.modules if m == "pixo" or m.startswith("pixo.")]:
        del sys.modules[name]       # the menu must not need Pixo to be importable at startup
    for _ in range(2):
        runpy.run_path(str(root / "menu.py"))
    assert not any(m == "pixo" or m.startswith("pixo.") for m in sys.modules)
    assert bars["Nodes"].items["ComfyUI"] is existing and not existing.items
    for bar in bars.values():
        assert "Pixo" in bar.items
        commands = bar.items["Pixo"].commands
        assert list(commands) == ["Timer", "Pulse"], commands
        created = []
        nuke.createNode = created.append
        for command in commands.values():
            exec(command, {"nuke": nuke})
        assert created == ["PixoTimer", "PixoPulse"], created

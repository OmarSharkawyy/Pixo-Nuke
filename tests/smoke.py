"""Run every Pixo check without Nuke, ComfyUI, or a GPU:  python3 -I tests/smoke.py"""
import os
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def fake_nuke():
    """Just enough of Nuke for the modules to import; each test fills in what it needs."""
    nuke = types.ModuleType("nuke")
    nuke.executeInMainThread = lambda function, args=(), kwargs=None: function(*args, **(kwargs or {}))
    nuke.message = lambda text: None
    nuke.createNode = lambda name: None
    nuke.toNode = lambda name: None
    nuke.thisNode = lambda: None
    nuke.root = lambda: types.SimpleNamespace(modified=lambda: False, setModified=lambda value: None)
    sys.modules["nuke"] = nuke
    return nuke


def main():
    import tempfile
    # Keep test runs out of the real timings file in ~/.nuke.
    os.environ["PIXO_TIMINGS_FILE"] = str(Path(tempfile.mkdtemp(prefix="pixo-tests-")) / "timings.json")
    import eta_checks
    import timer_checks as timer
    import monitor_checks as monitor_tests

    nuke = fake_nuke()
    from pixo import client, eta, monitor, monitor_ui, sysinfo, timer_ui, tracker
    try:
        from pixo import widget
    except ImportError:
        widget = None

    steps = [("timer tracking", lambda: timer.tracker_test(tracker)),
             ("time left estimate", lambda: eta_checks.eta_test(eta, tracker)),
             ("http client", lambda: timer.http_test(client)),
             ("timer node", lambda: timer.nuke_test(nuke, timer_ui, client)),
             ("system readings", lambda: monitor_tests.sysinfo_test(sysinfo)),
             ("cpu meter", lambda: monitor_tests.cpu_test(sysinfo)),
             ("nvidia-smi", lambda: monitor_tests.nvidia_test(sysinfo)),
             ("monitor sampling", lambda: monitor_tests.monitor_test(monitor, sysinfo, client)),
             ("monitor node", lambda: monitor_tests.monitor_ui_test(nuke, monitor_ui, monitor)),
             ("gizmos and menu", lambda: monitor_tests.package_test(ROOT, nuke, timer_ui, monitor_ui))]
    if widget is not None:
        steps[7:7] = [("meter layout", lambda: monitor_tests.widget_test(widget)),
                      ("meter drawing", lambda: monitor_tests.render_test(widget, monitor, sysinfo))]
    else:
        print("SKIP   meter panel: PySide6 or PySide2 is not installed")
    for name, step in steps:
        step()
        print(f"ok     {name}")
    print("All Pixo checks passed.")


if __name__ == "__main__":
    main()

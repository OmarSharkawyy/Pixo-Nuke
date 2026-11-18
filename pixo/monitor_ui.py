"""Nuke controls for the Pixo Pulse gizmo."""
import threading
import uuid

import nuke

from . import client, monitor
from .common import unmodified

NODE_CLASS = "PixoPulse"
NODE_LABEL = "Pixo Pulse"
SAMPLE_SECONDS = 1.0
WATCHERS = {}       # node full name -> (token, stop event)
SHOWN = {}          # node full name -> (status, label) currently displayed


def create():
    return nuke.createNode(NODE_CLASS)


def source_mode(text):
    text = str(text).casefold()
    return "server" if "server" in text else "local" if "local" in text or "this" in text else "auto"


def start(node):
    key = node.fullName()
    if key in WATCHERS:
        nuke.message("This monitor is already running.")
        return
    try:
        host = client.valid_host(node["server"].value())
        gpu_index = max(int(node["gpu_index"].value()), 0)
    except (ValueError, TypeError) as error:
        nuke.message(str(error))
        return
    mode = source_mode(node["source"].value())
    token, stop = uuid.uuid4().hex, threading.Event()
    state = monitor.STATES.setdefault(key, monitor.MonitorState())
    WATCHERS[key] = (token, stop)
    try:
        with unmodified():
            node["watch_token"].setValue(token)
            node["status"].setValue("Starting")
        threading.Thread(target=watch, args=((key, token), state, host, mode, gpu_index, stop),
                         daemon=True, name="PixoPulse").start()
    except Exception:
        WATCHERS.pop(key, None)
        raise


def stop(node):
    key = node.fullName()
    active = WATCHERS.pop(key, None)
    if active is None:
        nuke.message("This monitor is not running.")
        return
    active[1].set()
    SHOWN.pop(key, None)
    show(node, "Stopped", NODE_LABEL)


def show(node, status, label):
    with unmodified():
        node["status"].setValue(status)
        node["label"].setValue(label)


def short_label(metrics):
    parts = [f"{name.upper()} {metrics[name].percent:.0f}" for name in ("cpu", "gpu", "ram") if name in metrics]
    return " · ".join(parts) or NODE_LABEL


def watch(context, state, host, mode, gpu_index, stopped):
    def on_sample(metrics, note):
        # Nuke is only touched on its main thread; this also notices a deleted node.
        nuke.executeInMainThread(apply_label, args=(context, "Monitoring · " + note, short_label(metrics)))

    monitor.run(state, host, mode, gpu_index, stopped, SAMPLE_SECONDS, on_sample=on_sample)


def owned_node(context):
    """The gizmo this monitor belongs to, or None after releasing it if the node is gone."""
    key, token = context
    active = WATCHERS.get(key)
    if active is None or active[0] != token:
        return None
    node = nuke.toNode(key)
    if node is None or node.knob("watch_token") is None or node["watch_token"].value() != token:
        active[1].set()  # The node was deleted, or another script's node took its name.
        WATCHERS.pop(key, None)
        SHOWN.pop(key, None)
        return None
    return node


def apply_label(context, status, label):
    node = owned_node(context)
    if node is not None and SHOWN.get(context[0]) != (status, label):
        SHOWN[context[0]] = (status, label)
        show(node, status, label)

"""Nuke controls for the Pixo Timer gizmo."""
import threading
import uuid

import nuke

from . import client, eta
from .common import unmodified
from .tracker import RunTracker

NODE_CLASS = "PixoTimer"
NODE_LABEL = "Pixo Timer"
TICK_SECONDS = 1.0   # how often the timer display refreshes
TICKS_PER_POLL = 2   # server is asked every TICK_SECONDS * TICKS_PER_POLL
WATCHERS = {}        # node full name -> (token, stop event)
SHOWN = {}           # node full name -> the view currently displayed


def create():
    return nuke.createNode(NODE_CLASS)


def start(node):
    key = node.fullName()
    if key in WATCHERS:
        nuke.message("This timer is already watching ComfyUI.")
        return
    try:
        host = client.valid_host(node["server"].value())
    except ValueError as error:
        nuke.message(str(error))
        return
    token = uuid.uuid4().hex
    stop = threading.Event()
    WATCHERS[key] = (token, stop)
    try:
        with unmodified():
            node["watch_token"].setValue(token)
            node["status"].setValue("Connecting to ComfyUI")
        threading.Thread(target=watch, args=((key, token), host, stop),
                         daemon=True, name="PixoTimer").start()
    except Exception:
        WATCHERS.pop(key, None)
        raise


def stop(node):
    active = WATCHERS.pop(node.fullName(), None)
    if active is None:
        nuke.message("This timer is not watching ComfyUI.")
        return
    active[1].set()
    SHOWN.pop(node.fullName(), None)
    show(node, "Stopped", "", None, None, NODE_LABEL)


def show(node, status, detail, last_run, session, label, estimate=""):
    with unmodified():
        node["status"].setValue(status)
        node["detail"].setValue(detail)
        if node.knob("estimate") is not None:  # nodes saved before the Estimate field have none
            node["estimate"].setValue(estimate)
        if last_run is not None:
            node["last_run"].setValue(last_run)
        if session is not None:
            node["session"].setValue(session)
        node["label"].setValue(label)


def watch(context, host, stopped):
    tracker = RunTracker(estimator=eta.Estimator())

    def fetch(route):
        return client.get_json(host, route)

    while not stopped.is_set():
        try:
            tracker.poll(fetch)
        except Exception as error:  # A real error such as HTTP 404 means this is not a usable ComfyUI URL.
            nuke.executeInMainThread(fail, args=(context, str(error)))
            return
        for _ in range(TICKS_PER_POLL):
            # Every tick, so a deleted node is noticed even while the display is not changing.
            nuke.executeInMainThread(apply_view, args=(context, tracker.view()))
            if stopped.wait(TICK_SECONDS):
                return


def owned_node(context):
    """The gizmo this watcher belongs to, or None after releasing it if the node is gone."""
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


def apply_view(context, view):
    node = owned_node(context)
    if node is not None and SHOWN.get(context[0]) != view:
        SHOWN[context[0]] = view
        show(node, view.status, view.detail, view.last_run, view.session, view.label, view.estimate)


def fail(context, message):
    node = owned_node(context)
    if node is not None:
        WATCHERS.pop(context[0], None)
        SHOWN.pop(context[0], None)
        show(node, "Stopped: " + message.splitlines()[0], "", None, None, NODE_LABEL)
        nuke.message(message)

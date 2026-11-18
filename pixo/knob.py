"""The custom knob behind the Pulse meters.

Nuke builds this when the node is created, so nothing here may raise: if the meter panel
cannot load, the panel shows the reason instead and the node still works.
"""
import traceback

import nuke


class Meters:
    def __init__(self):
        self.panel = None
        self.error = None
        try:
            self.node = nuke.thisNode()
        except Exception:
            self.node = None
        try:
            from . import widget
            self.panel = widget.MonitorWidget(self.node)
        except Exception:
            self.error = traceback.format_exc()
            print("Pixo Pulse: the meter panel could not load.\n" + self.error)

    def makeUI(self):
        if self.panel is not None:
            return self.panel.makeUI()
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets
        label = QtWidgets.QLabel("Pixo Pulse: the meter panel could not load.\n\n" + (self.error or ""))
        label.setWordWrap(True)
        return label

    def updateValue(self):
        if self.panel is not None:
            self.panel.updateValue()

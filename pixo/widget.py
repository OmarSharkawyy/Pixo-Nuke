"""The Pixo meter panel: live meters with a short history line for each reading.

Embedded in the gizmo as a Python custom knob; nothing here runs unless that panel is open.
"""
import nuke

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets

from . import monitor

ROWS = (("cpu", "CPU"), ("ram", "RAM"), ("gpu", "GPU"), ("vram", "VRAM"), ("temp", "TEMP"))
HEADER, ROW, PAD, GAP = 22, 26, 10, 10
LABEL_W, VALUE_W, SPARK_W, BAR_H = 44, 50, 84, 8
REFRESH_MS = 500
OK, WARM, HOT = "#35b9a5", "#e7a23c", "#e5604d"
LIMITS = {"temp": (65, 80)}   # (warm, hot); everything else uses 60 and 85
DEFAULT_LIMITS = (60, 85)


def level_color(name, value):
    warm, hot = LIMITS.get(name, DEFAULT_LIMITS)
    return HOT if value >= hot else WARM if value >= warm else OK


def format_value(name, value):
    return "n/a" if value is None else f"{value:.0f}°C" if name == "temp" else f"{value:.0f}%"


def fill_fraction(value):
    return 0.0 if value is None else min(max(value / 100.0, 0.0), 1.0)


def geometry(width, row):
    """Pixel rectangles (x, y, w, h) for one meter row at a given panel width."""
    y = HEADER + row * ROW
    value_x = width - PAD - VALUE_W
    spark_x = value_x - GAP - SPARK_W
    bar_x = PAD + LABEL_W + GAP
    return {"label": (PAD, y, LABEL_W, ROW),
            "bar": (bar_x, y + (ROW - BAR_H) // 2, max(spark_x - GAP - bar_x, 20), BAR_H),
            "spark": (spark_x, y + 4, SPARK_W, ROW - 8),
            "value": (value_x, y, VALUE_W, ROW)}


def panel_height():
    return HEADER + ROW * len(ROWS) + PAD // 2


class MeterPanel(QtWidgets.QWidget):
    def __init__(self, key=None, state=None, parent=None):
        super().__init__(parent)
        self.key, self.state = key, state
        self.setMinimumHeight(panel_height())
        self.setMinimumWidth(PAD * 2 + LABEL_W + GAP * 3 + 40 + SPARK_W + VALUE_W)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def snapshot(self):
        state = self.state or monitor.STATES.get(self.key)
        return state.snapshot() if state is not None else monitor.Snapshot(False, "", {}, {})

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start(REFRESH_MS)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def refresh(self):
        snapshot = self.snapshot()
        self.setToolTip("\n".join(f"{label}: {snapshot.latest[name].detail}" for name, label in ROWS
                                  if name in snapshot.latest))
        self.update()

    def paintEvent(self, event):
        snapshot = self.snapshot()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        text = self.palette().windowText().color()
        dim = QtGui.QColor(text)
        dim.setAlpha(120)
        track = QtGui.QColor(text)
        track.setAlpha(34)
        small = QtGui.QFont(self.font())
        small.setPointSizeF(max(self.font().pointSizeF() - 1, 7))
        painter.setFont(small)
        painter.setPen(dim)
        header = snapshot.note if snapshot.running else "Not monitoring. Press Start Monitoring."
        painter.drawText(QtCore.QRect(PAD, 0, self.width() - 2 * PAD, HEADER),
                         QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, header)
        for row, (name, label) in enumerate(ROWS):
            rects = {key: QtCore.QRectF(*value) for key, value in geometry(self.width(), row).items()}
            reading = snapshot.latest.get(name) if snapshot.running else None
            value = reading.percent if reading is not None else None
            color = QtGui.QColor(level_color(name, value)) if value is not None else dim
            painter.setPen(dim)
            painter.drawText(rects["label"], QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, label)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(rects["bar"], BAR_H / 2, BAR_H / 2)
            if value is not None:
                fill = QtCore.QRectF(rects["bar"])
                fill.setWidth(max(fill.width() * fill_fraction(value), BAR_H))
                painter.setBrush(color)
                painter.drawRoundedRect(fill, BAR_H / 2, BAR_H / 2)
            self.draw_history(painter, rects["spark"], snapshot.history.get(name, ()), color)
            painter.setPen(text if value is not None else dim)
            painter.drawText(rects["value"], QtCore.Qt.AlignVCenter | QtCore.Qt.AlignRight,
                             format_value(name, value) if snapshot.running else "–")
        painter.end()

    def draw_history(self, painter, rect, values, color):
        """A thin line of the recent readings; gaps where a reading was missing."""
        if not values:
            return
        step = rect.width() / (monitor.HISTORY_LENGTH - 1)
        left = rect.right() - step * (len(values) - 1)
        path, drawing = QtGui.QPainterPath(), False
        for position, value in enumerate(values):
            if value is None:
                drawing = False
                continue
            point = QtCore.QPointF(left + step * position, rect.bottom() - rect.height() * fill_fraction(value))
            if drawing:
                path.lineTo(point)
            else:
                path.moveTo(point)
                drawing = True
        line = QtGui.QColor(color)
        line.setAlpha(190)
        painter.setPen(QtGui.QPen(line, 1.3, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawPath(path)


class MonitorWidget:
    """The meter panel for one node; pixo.knob.Meters hands it to Nuke as the custom knob."""

    def __init__(self, node=None):
        self.node = node if node is not None else nuke.thisNode()
        self.panel = None

    def makeUI(self):
        self.panel = MeterPanel(self.node.fullName())
        return self.panel

    def updateValue(self):
        pass

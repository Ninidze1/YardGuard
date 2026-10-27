"""Small Qt helpers: background tasks, image conversion, style refresh."""
from __future__ import annotations

import logging

import cv2
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage

log = logging.getLogger("yardguard")


class _Signals(QObject):
    finished = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn, args):
        super().__init__()
        self.fn, self.args = fn, args
        self.signals = _Signals()  # created in the GUI thread, so results arrive there

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as exc:  # reported to the caller as the result
            log.exception("Background task failed")
            result = exc
        self.signals.finished.emit(result)


_alive: set = set()


def run_async(fn, *args, on_done=None) -> None:
    """Run fn(*args) on the thread pool; on_done(result) runs in the GUI thread."""
    task = _Task(fn, args)
    task.setAutoDelete(False)  # Python keeps the task alive until its result is delivered
    _alive.add(task)

    def finish(result, task=task):
        _alive.discard(task)
        if on_done:
            try:
                on_done(result)
            except RuntimeError:
                pass  # the widget waiting for this result was closed

    task.signals.finished.connect(finish)
    QThreadPool.globalInstance().start(task)


def to_qimage(frame_bgr) -> QImage:
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def set_prop(widget, name: str, value) -> None:
    """Set a property used by the style sheet and re-apply the style."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)

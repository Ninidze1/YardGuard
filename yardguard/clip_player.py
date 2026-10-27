"""Plays incident clips without QtMultimedia.

Windows Smart App Control blocks unsigned native DLLs, and Qt's FFmpeg multimedia plugin is one,
so clips are decoded with OpenCV (already used everywhere in the app) on a worker thread. The
GUI shows each frame in an ordinary widget, paced by a QTimer: ClipPlayer asks for the next
frame only after the previous one arrived, so a slow decode never piles up requests.
"""
from __future__ import annotations

import threading

import cv2
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from .qt_util import to_qimage

SKIP_AHEAD = 24   # a target this few frames ahead is reached by decoding forward, not by seeking


class _Signals(QObject):
    opened = Signal(int, float, int)     # generation, fps, frame count
    frame = Signal(int, int, object)     # generation, frame index, QImage
    failed = Signal(int, str)


class _Decoder(threading.Thread):
    """Owns the cv2.VideoCapture. Requests replace each other: when the user scrubs, only the
    newest frame is decoded."""

    def __init__(self, signals: _Signals) -> None:
        super().__init__(name="yg-clip-player", daemon=True)
        self.signals = signals
        self._cond = threading.Condition()
        self._open: tuple[int, str, float] | None = None   # gen, path, start (s)
        self._close = False
        self._quit = False
        self._want: tuple[int, int] | None = None        # gen, frame index
        self._cap: cv2.VideoCapture | None = None
        self._gen = 0
        self._pos = -1          # index of the last decoded frame
        self._count = 0

    def open(self, gen: int, path: str, start_s: float) -> None:
        with self._cond:
            self._open, self._want, self._close = (gen, path, start_s), None, False
            self._cond.notify()

    def request(self, gen: int, index: int) -> None:
        with self._cond:
            self._want = (gen, index)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._close, self._open, self._want = True, None, None
            self._cond.notify()

    def quit(self) -> None:
        with self._cond:
            self._quit = True
            self._cond.notify()

    def run(self) -> None:
        while True:
            with self._cond:
                while not (self._quit or self._close or self._open or self._want):
                    self._cond.wait()
                quit_, close, opening, want = self._quit, self._close, self._open, self._want
                self._close, self._open, self._want = False, None, None
            if quit_ or close or opening:
                self._release()
            if quit_:
                return
            if opening:
                self._do_open(*opening)
            elif want and want[0] == self._gen and self._cap is not None:
                self._emit(want[1])

    def _release(self) -> None:
        if self._cap is not None:
            self._cap.release()  # also lets cleanup delete the file
            self._cap = None

    def _do_open(self, gen: int, path: str, start_s: float) -> None:
        self._gen = gen
        cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            self.signals.failed.emit(gen, "The clip could not be opened.")
            return
        fps = cap.get(cv2.CAP_PROP_FPS) or 8.0
        self._count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if self._count <= 0:
            cap.release()
            self.signals.failed.emit(gen, "The clip is empty.")
            return
        self._cap, self._pos = cap, -1
        self.signals.opened.emit(gen, float(fps), self._count)
        self._emit(int(round(start_s * fps)))

    def _emit(self, index: int) -> None:
        index = max(0, min(index, self._count - 1))
        frame = self._read(index)
        if frame is None and index > 0:  # the container promised more frames than it holds
            self._count = index
            index -= 1
            frame = self._read(index)
        if frame is not None:
            self.signals.frame.emit(self._gen, index, to_qimage(frame))

    def _read(self, index: int):
        cap = self._cap
        if not (self._pos < index <= self._pos + SKIP_AHEAD):
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self._pos = index - 1
        while self._pos < index - 1:
            if not cap.grab():
                return None
            self._pos += 1
        ok, frame = cap.read()
        if not ok:
            return None
        self._pos = index
        return frame


class ClipPlayer(QObject):
    """Muted, looping clip playback. Connect frame_ready to a view; drive it with play(),
    pause(), seek() and set_speed()."""

    frame_ready = Signal(QImage)
    position_changed = Signal(int, int)    # frame index, frame count
    playing_changed = Signal(bool)
    opened = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals()
        self._signals.opened.connect(self._opened)
        self._signals.frame.connect(self._frame)
        self._signals.failed.connect(self._failed)
        self._decoder: _Decoder | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._gen = 0
        self.path = ""
        self.fps = 8.0
        self.count = 0
        self.index = 0
        self.speed = 1.0
        self._playing = False
        self._autoplay = False
        self._awaiting = False

    # --- state ---

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def loaded(self) -> bool:
        return self.count > 0

    @property
    def duration_s(self) -> float:
        return self.count / self.fps if self.fps else 0.0

    @property
    def position_s(self) -> float:
        return self.index / self.fps if self.fps else 0.0

    # --- control ---

    def open(self, path: str, start_s: float = 0.0, play: bool = True) -> None:
        """Load a clip and show the frame at start_s; plays from there when play is True."""
        self._gen += 1
        self.path, self.count, self.index = path, 0, 0
        self._awaiting = True
        self._autoplay = play
        self._set_playing(False)
        self._ensure_decoder().open(self._gen, path, max(0.0, start_s))

    def close(self) -> None:
        """Stop and release the file."""
        self._gen += 1
        self._set_playing(False)
        self.path, self.count, self.index = "", 0, 0
        self._awaiting = False
        if self._decoder is not None:
            self._decoder.close()

    def shutdown(self) -> None:
        self.close()
        if self._decoder is not None:
            self._decoder.quit()
            self._decoder.join(2)
            self._decoder = None

    def play(self) -> None:
        if self.loaded:
            self._set_playing(True)
        else:
            self._autoplay = bool(self.path)

    def pause(self) -> None:
        self._autoplay = False
        self._set_playing(False)

    def toggle(self) -> None:
        self.pause() if (self._playing or self._autoplay and not self.loaded) else self.play()

    def seek(self, seconds: float) -> None:
        self.seek_frame(int(round(seconds * self.fps)))

    def seek_frame(self, index: int) -> None:
        if not self.loaded:
            return
        index = max(0, min(int(index), self.count - 1))
        self._awaiting = True
        self._decoder.request(self._gen, index)

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.25, float(speed))
        if self._playing:
            self._timer.start(self._interval())

    # --- internals ---

    def _ensure_decoder(self) -> _Decoder:
        if self._decoder is None or not self._decoder.is_alive():
            self._decoder = _Decoder(self._signals)
            self._decoder.start()
        return self._decoder

    def _interval(self) -> int:
        return max(10, int(round(1000 / (self.fps * self.speed))))

    def _set_playing(self, on: bool) -> None:
        if on:
            self._timer.start(self._interval())
        else:
            self._timer.stop()
        if on != self._playing:
            self._playing = on
            self.playing_changed.emit(on)

    def _tick(self) -> None:
        if self._awaiting or not self.loaded:
            return  # the previous frame is still being decoded: never queue up
        nxt = self.index + 1
        self._awaiting = True
        self._decoder.request(self._gen, 0 if nxt >= self.count else nxt)   # loops

    def _opened(self, gen: int, fps: float, count: int) -> None:
        if gen != self._gen:
            return
        self.fps, self.count = fps, count
        self.opened.emit()

    def _frame(self, gen: int, index: int, img: QImage) -> None:
        if gen != self._gen:
            return
        self._awaiting = False
        self.index = index
        self.frame_ready.emit(img)
        self.position_changed.emit(index, self.count)
        if self._autoplay:
            self._autoplay = False
            self._set_playing(True)

    def _failed(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        self._awaiting = False
        self.count = 0
        self._set_playing(False)
        self.failed.emit(message)

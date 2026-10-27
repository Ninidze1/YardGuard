"""Fast batched YOLO for the monitoring pipeline ("eyes").

Backend is picked at runtime: PyTorch FP16 on an NVIDIA GPU, else PyTorch FP32 on the GPU
(if FP16 fails), else CPU. Only plain PyTorch is used: compiled exporters (ONNX/TensorRT)
ship unsigned native DLLs that Windows Smart App Control blocks, and the app must stay portable.

All frames of one call go to the GPU as one batch, letterboxed into the same landscape input
(e.g. 1280x736 for imgsz 1280), which is ~40% less work than a square 1280x1280 input.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from typing import Callable

import numpy as np

from .config import ROOT
from .video import CLASSES, ensure_model

# Keep caches next to the app, and never let ultralytics pip-install packages at runtime
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / ".cache" / "ultralytics"))
os.environ.setdefault("YOLO_AUTOINSTALL", "false")

log = logging.getLogger(__name__)

STRIDE = 32
_CLASS_IDS = list(CLASSES)

Progress = Callable[[str], None] | None


def input_shape(imgsz: int, landscape: bool = True) -> tuple[int, int]:
    """(height, width) of the network input: long side imgsz, 16:9 short side rounded up to the stride."""
    long_side = max(STRIDE, math.ceil(imgsz / STRIDE) * STRIDE)
    if not landscape:
        return long_side, long_side
    return math.ceil(long_side * 9 / 16 / STRIDE) * STRIDE, long_side


class FastDetector:
    """Batched person/vehicle detector shared by all monitored cameras; thread-safe."""

    def __init__(self, model, backend: str, model_name: str, shape: tuple[int, int], batch: int = 4) -> None:
        self._model = model
        self.backend = backend        # "pytorch-fp16" | "pytorch" | "cpu"
        self.model_name = model_name
        self.shape = shape            # (h, w) network input
        self.batch = batch            # max frames per GPU call
        self.fallback_reason: str | None = None
        self._lock = threading.Lock()

    def predict(self, frames: list[np.ndarray], conf: float) -> list[np.ndarray]:
        """One (N, 6) float32 array per frame: x1, y1, x2, y2 (frame pixels), score, class_id."""
        out: list[np.ndarray] = []
        with self._lock:
            for i in range(0, len(frames), self.batch):
                out += self._run(frames[i:i + self.batch], conf)
        return out

    def _run(self, chunk: list[np.ndarray], conf: float) -> list[np.ndarray]:
        results = self._model.predict(chunk, conf=conf, classes=_CLASS_IDS, imgsz=list(self.shape),
                                      # quantize=16 is FP16 ('half' is deprecated and warns on every call)
                                      quantize=16 if self.backend == "pytorch-fp16" else None,
                                      device="cpu" if self.backend == "cpu" else 0, verbose=False)
        return [r.boxes.data.float().cpu().numpy().astype(np.float32, copy=False).reshape(-1, 6)
                for r in results]

    def warmup(self, runs: int = 2) -> None:
        frame = np.zeros((*self.shape, 3), np.uint8)
        for _ in range(runs):
            self.predict([frame] * self.batch, 0.5)

    def __repr__(self) -> str:
        h, w = self.shape
        return f"FastDetector({self.model_name}, {self.backend}, {w}x{h}, batch {self.batch})"


def load_fast_detector(model_name: str, imgsz: int, use_tensorrt: bool = True, progress: Progress = None,
                       batch: int = 4, landscape: bool = True) -> FastDetector:
    """Best available detector. use_tensorrt is accepted for compatibility and ignored
    (TensorRT was dropped, see module docstring). progress(text) reports the slow steps.
    Raises only if the model itself cannot be loaded."""
    import torch
    from ultralytics import YOLO

    shape = input_shape(imgsz, landscape)
    if progress:
        progress(f"Loading {model_name}…")
    path = str(ensure_model(model_name))
    if not torch.cuda.is_available():
        det = FastDetector(YOLO(path), "cpu", model_name, shape, batch)
        det.fallback_reason = "No NVIDIA GPU available: detection runs on the CPU (slow)."
        return det

    reason = None
    for backend in ("pytorch-fp16", "pytorch"):
        det = FastDetector(YOLO(path), backend, model_name, shape, batch)
        try:
            det.warmup()
            det.fallback_reason = reason
            return det
        except Exception as exc:  # noqa: BLE001 - try the next, safer backend
            reason = f"{backend} failed: {str(exc)[:200]}"
            log.warning("%s; trying the next backend", reason, exc_info=True)
            torch.cuda.empty_cache()
    det = FastDetector(YOLO(path), "cpu", model_name, shape, batch)
    det.fallback_reason = reason
    return det

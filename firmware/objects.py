"""Object detection with a YOLOv8-format ONNX model, run through OpenCV's dnn module
(no PyTorch needed on the device)."""
from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import numpy as np

from .coco import COCO_CLASSES

log = logging.getLogger(__name__)

INPUT = 640
CHECK_INTERVAL = 0.4        # seconds between detections per camera
RESULT_FRESH = 2.0          # a detection counts as current for this long


class YoloDetector:
    """One model shared by all cameras; inference is serialized with a lock."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._net: cv2.dnn.Net | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return os.path.isfile(self.path)

    def detect(self, frame: np.ndarray, classes: list[str], confidence: float) -> list[str]:
        """Which of the given classes appear in the frame with at least this confidence."""
        wanted = [COCO_CLASSES.index(c) for c in classes if c in COCO_CLASSES]
        if not wanted:
            return []
        h, w = frame.shape[:2]
        scale = INPUT / max(h, w)                                  # letterbox: keep aspect
        resized = cv2.resize(frame, (round(w * scale), round(h * scale)))
        canvas = np.full((INPUT, INPUT, 3), 114, np.uint8)
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255, (INPUT, INPUT), swapRB=True)
        with self._lock:
            if self._net is None:
                self._net = cv2.dnn.readNetFromONNX(self.path)
                log.info("loaded object detection model %s", self.path)
            self._net.setInput(blob)
            out = self._net.forward()                              # (1, 4+80, 8400)
        scores = out[0][4:, :]                                     # per class, per candidate box
        best = scores[wanted].max(axis=1)
        return [COCO_CLASSES[c] for c, b in zip(wanted, best) if b >= confidence]


class ObjectWatcher:
    """Runs the detector on the newest frame in a side thread, so the capture loop
    never waits for inference. Frames are the same ones the recorder gets."""

    def __init__(self, detector: YoloDetector) -> None:
        self._detector = detector
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._job: tuple[np.ndarray, list[str], float] | None = None
        self._thread: threading.Thread | None = None
        self.objects: list[str] = []
        self._seen_at = 0.0

    def submit(self, frame: np.ndarray, classes: list[str], confidence: float) -> None:
        self._job = (frame, list(classes), confidence)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="objects", daemon=True)
            self._thread.start()
        self._wake.set()

    def current(self) -> list[str]:
        """Objects seen recently (empty if the detector fell behind or nothing is there)."""
        return self.objects if time.monotonic() - self._seen_at < RESULT_FRESH else []

    def reset(self) -> None:
        self.objects, self._job = [], None

    def close(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            job, self._job = self._job, None
            if job is None or self._stop.is_set():
                continue
            try:
                self.objects = self._detector.detect(*job)
                self._seen_at = time.monotonic()
            except Exception:
                log.exception("object detection failed")
                self.objects = []
            self._stop.wait(CHECK_INTERVAL)

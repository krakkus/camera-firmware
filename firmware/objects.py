"""Person detection with OpenCV's built-in HOG + SVM people detector (no model to download)."""
from __future__ import annotations

import logging
import threading
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)

ANALYSIS_WIDTH = 640        # frames are scaled down to this width before detecting
CHECK_INTERVAL = 0.4        # seconds between detections per camera
RESULT_FRESH = 2.0          # a detection counts as current for this long


class PersonDetector:
    """Shared by all cameras; detection is serialized with a lock."""

    def __init__(self) -> None:
        self._hog: cv2.HOGDescriptor | None = None
        self._lock = threading.Lock()

    def detect(self, frame: np.ndarray, confidence: float) -> bool:
        """Is a person in the frame? Confidence is the HOG SVM score a detection must
        reach: higher = fewer false alarms and more misses."""
        h, w = frame.shape[:2]
        if w > ANALYSIS_WIDTH:
            frame = cv2.resize(frame, (ANALYSIS_WIDTH, round(h * ANALYSIS_WIDTH / w)))
        with self._lock:
            if self._hog is None:
                self._hog = cv2.HOGDescriptor()
                self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
                log.info("person detector ready")
            _, weights = self._hog.detectMultiScale(frame, winStride=(8, 8), padding=(8, 8),
                                                    scale=1.05)
        return any(wt >= confidence for wt in np.ravel(weights))


class ObjectWatcher:
    """Runs the detector on the newest frame in a side thread, so the capture loop
    never waits for inference. Frames are the same ones the recorder gets."""

    def __init__(self, detector: PersonDetector) -> None:
        self._detector = detector
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._job: tuple[np.ndarray, float] | None = None
        self._thread: threading.Thread | None = None
        self.objects: list[str] = []
        self._seen_at = 0.0

    def submit(self, frame: np.ndarray, confidence: float) -> None:
        self._job = (frame, confidence)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="objects", daemon=True)
            self._thread.start()
        self._wake.set()

    def current(self) -> list[str]:
        """What was seen recently (empty if the detector fell behind or nothing is there)."""
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
                self.objects = ["person"] if self._detector.detect(*job) else []
                self._seen_at = time.monotonic()
            except Exception:
                log.exception("object detection failed")
                self.objects = []
            self._stop.wait(CHECK_INTERVAL)

"""
Queen bee detection with the trained YOLO model.

Used by both the live camera stream and the /detect API (mobile uploads).
"""
import os
import threading


class QueenDetector:
    """
    Loads the YOLO model once (on first use) and runs queen detection.
    A lock makes it safe to call from the camera thread and API requests at once.
    """

    def __init__(self, model_path, conf=0.17, iou=0.5):
        self.model_path = model_path
        self.conf = conf
        self.iou = iou
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is None:
            if not os.path.exists(self.model_path):
                raise FileNotFoundError(f"Queen model not found: {self.model_path}")

            # Imported here so the app (and tests) start fast without loading torch
            from ultralytics import YOLO

            print("Loading queen model:", self.model_path)
            self._model = YOLO(self.model_path)

        return self._model

    def detect(self, frame):
        """
        Returns (bbox, confidence) for the best queen detection,
        or (None, None) if no queen is found. bbox = (x1, y1, x2, y2).
        """
        with self._lock:
            model = self._get_model()
            results = model.predict(frame, conf=self.conf, iou=self.iou, verbose=False)

        r = results[0]
        best_bbox = None
        best_conf = None

        if r.boxes is not None and len(r.boxes) > 0:
            for box in r.boxes:
                cls = int(box.cls[0].item())
                score = float(box.conf[0].item())
                label = model.names.get(cls, str(cls)).lower()

                if "queen" in label and "non" not in label:
                    if best_conf is None or score > best_conf:
                        best_conf = score
                        best_bbox = tuple(box.xyxy[0].tolist())

        return best_bbox, best_conf


class QueenBoxTracker:
    """
    Keeps a stable queen marker even if YOLO misses for a few frames.
    Frame-by-frame detection can flicker under occlusion or motion blur.
    """

    def __init__(
        self,
        miss_tolerance_frames=35,
        lock_after_hits=2,
        unlock_after_misses=60,
        ema_alpha=0.45,
        min_conf_for_update=0.06,
    ):
        self.miss_tol = int(miss_tolerance_frames)
        self.lock_after = int(lock_after_hits)
        self.unlock_after = int(unlock_after_misses)
        self.alpha = float(ema_alpha)
        self.min_conf = float(min_conf_for_update)
        self.reset()

    def reset(self):
        self.frame_idx = 0
        self.bbox = None
        self.conf = 0.0
        self.last_seen = -999999
        self.locked = False
        self.hit_streak = 0

    def _ema_bbox(self, old_bbox, new_bbox):
        if old_bbox is None:
            return new_bbox

        a = self.alpha
        return tuple(a * n + (1 - a) * o for o, n in zip(old_bbox, new_bbox))

    def update(self, detection_bbox, detection_conf):
        """Returns (bbox_to_draw, status, confidence)."""
        self.frame_idx += 1

        if (
            detection_bbox is not None
            and detection_conf is not None
            and detection_conf >= self.min_conf
        ):
            self.bbox = self._ema_bbox(self.bbox, detection_bbox)
            self.conf = float(detection_conf)
            self.last_seen = self.frame_idx
            self.hit_streak += 1

            if self.hit_streak >= self.lock_after:
                self.locked = True

            return self.bbox, ("LOCKED" if self.locked else "TRACKING"), self.conf

        self.hit_streak = max(0, self.hit_streak - 1)
        frames_since_seen = self.frame_idx - self.last_seen

        if self.bbox is not None and frames_since_seen <= self.miss_tol:
            return self.bbox, ("LOCKED" if self.locked else "HOLD"), self.conf

        if self.locked and self.bbox is not None and frames_since_seen <= self.unlock_after:
            return self.bbox, "LOCKED (WEAK)", self.conf

        self.reset()
        return None, "SCANNING", 0.0

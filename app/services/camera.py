"""
Live hive camera reader.

One background thread per hive camera:
  - reads frames from the Pi stream (or a local webcam),
  - reconnects automatically if the stream drops,
  - falls back to the demo video while the live camera is unavailable,
  - runs queen detection and draws the overlay,
  - keeps the latest JPEG so any number of browser/mobile viewers can watch
    without opening extra connections to the Pi.
"""
import os
import threading
import time

import cv2

from .detector import QueenBoxTracker

MODE_STARTING = "starting"
MODE_LIVE = "live"
MODE_DEMO = "demo"
MODE_OFFLINE = "offline"


def open_capture(source, timeout_ms=5000):
    """Supports webcam index ('0', '1'), video file path or stream URL."""
    if isinstance(source, str) and source.isdigit():
        return cv2.VideoCapture(int(source))

    if isinstance(source, str) and "://" in source:
        try:
            return cv2.VideoCapture(
                source,
                cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms,
                 cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms],
            )
        except (cv2.error, TypeError):
            pass

    return cv2.VideoCapture(source)


class HiveCamera(threading.Thread):
    def __init__(
        self,
        hive_id,
        live_source,
        demo_path=None,
        fallback_to_demo=True,
        retry_seconds=30,
        detector=None,
        detect_every_n_frames=2,
        jpeg_quality=80,
        on_queen_change=None,
    ):
        super().__init__(daemon=True, name=f"HiveCamera-{hive_id}")
        self.hive_id = hive_id
        self.live_source = live_source or None
        self.demo_path = demo_path
        self.fallback_to_demo = fallback_to_demo
        self.retry_seconds = retry_seconds
        self.detector = detector
        self.detect_every = max(1, int(detect_every_n_frames))
        self.jpeg_quality = int(jpeg_quality)
        # Called as on_queen_change(hive_id, status, present, conf) on live frames
        self.on_queen_change = on_queen_change

        self.tracker = QueenBoxTracker()
        self._stop_event = threading.Event()
        self._cond = threading.Condition()

        self.mode = MODE_STARTING
        self.latest_jpeg = None
        self.frame_id = 0
        self.last_frame_at = None
        self.detection_error = None

        self._raw_bbox = None
        self._raw_conf = None
        self._frames_seen = 0
        self._last_present = None

        self.queen = {"status": "SCANNING", "present": False, "confidence": 0.0}

    # ------------------------------------------------------------------
    # Public API (used by routes)
    # ------------------------------------------------------------------
    def stop(self):
        self._stop_event.set()

    def wait_for_frame(self, last_frame_id, timeout=5.0):
        """Blocks until a frame newer than last_frame_id exists. Returns (id, jpeg)."""
        with self._cond:
            self._cond.wait_for(
                lambda: self.frame_id != last_frame_id or self._stop_event.is_set(),
                timeout=timeout,
            )
            return self.frame_id, self.latest_jpeg

    def status(self):
        return {
            "mode": self.mode,
            "live_source_configured": bool(self.live_source),
            "last_frame_at": self.last_frame_at,
            "detection_enabled": self.detector is not None and self.detection_error is None,
            "detection_error": self.detection_error,
            "queen": dict(self.queen),
        }

    # ------------------------------------------------------------------
    # Thread loop
    # ------------------------------------------------------------------
    def run(self):
        while not self._stop_event.is_set():
            if self.live_source and self._play_live():
                continue  # live stream ended -> try to reconnect straight away

            demo_ok = (
                self.fallback_to_demo
                and self.demo_path
                and os.path.exists(self.demo_path)
            )
            if demo_ok:
                # Show the demo video, then retry the live camera after retry_seconds
                duration = self.retry_seconds if self.live_source else None
                self._play_demo(duration)
            else:
                self._set_mode(MODE_OFFLINE)
                self._stop_event.wait(min(self.retry_seconds, 5) if self.live_source else 5)

    def _play_live(self):
        """Returns True if the live stream played (and then dropped), False if it never opened."""
        print(f"[camera {self.hive_id}] connecting to {self.live_source}")
        cap = open_capture(self.live_source)
        if not cap.isOpened():
            print(f"[camera {self.hive_id}] live camera unavailable")
            cap.release()
            return False

        self._set_mode(MODE_LIVE)
        self.tracker.reset()
        failures = 0

        while not self._stop_event.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= 10:
                    print(f"[camera {self.hive_id}] live stream lost, reconnecting")
                    break
                time.sleep(0.2)
                continue

            failures = 0
            self._process(frame, live=True)

        cap.release()
        return True

    def _play_demo(self, duration=None):
        cap = cv2.VideoCapture(self.demo_path)
        if not cap.isOpened():
            self._set_mode(MODE_OFFLINE)
            self._stop_event.wait(5)
            return

        self._set_mode(MODE_DEMO)
        self.tracker.reset()
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        frame_delay = 1.0 / min(max(fps, 1), 30)
        started = time.time()

        while not self._stop_event.is_set():
            if duration is not None and time.time() - started > duration:
                break

            t0 = time.time()
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop the video
                continue

            self._process(frame, live=False)

            # Play at the video's real speed (detection time included)
            elapsed = time.time() - t0
            if elapsed < frame_delay:
                time.sleep(frame_delay - elapsed)

        cap.release()

    # ------------------------------------------------------------------
    # Frame processing
    # ------------------------------------------------------------------
    def _set_mode(self, mode):
        if self.mode != mode:
            print(f"[camera {self.hive_id}] mode -> {mode}")
            self.mode = mode
            self._last_present = None

    def _process(self, frame, live):
        self._frames_seen += 1

        if self.detector is not None and self.detection_error is None:
            if self._frames_seen % self.detect_every == 0:
                try:
                    self._raw_bbox, self._raw_conf = self.detector.detect(frame)
                except Exception as e:  # e.g. model file missing
                    self.detection_error = str(e)
                    print(f"[camera {self.hive_id}] detection disabled: {e}")
                    self._raw_bbox, self._raw_conf = None, None

            bbox, status, conf = self.tracker.update(self._raw_bbox, self._raw_conf)
        else:
            bbox, status, conf = None, "DISABLED", 0.0

        present = bbox is not None
        self.queen = {"status": status, "present": present, "confidence": round(float(conf), 3)}

        # Only real (live) observations go into the hive history, not demo video
        if live and self.on_queen_change and present != self._last_present:
            try:
                self.on_queen_change(self.hive_id, status, present, float(conf))
            except Exception as e:
                print(f"[camera {self.hive_id}] could not save queen event: {e}")
        self._last_present = present

        self._draw_overlay(frame, bbox, status, conf, live)

        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if not ok:
            return

        with self._cond:
            self.latest_jpeg = buffer.tobytes()
            self.frame_id += 1
            self.last_frame_at = time.time()
            self._cond.notify_all()

    @staticmethod
    def _draw_overlay(frame, bbox, status, conf, live):
        label = "LIVE hive camera" if live else "DEMO video (live camera offline)"
        cv2.putText(frame, f"Queen Bee Watch | {label}", (15, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        if bbox is not None:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            color = (0, 255, 255) if "LOCKED" in status else (0, 200, 255)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
            cv2.putText(frame, f"Queen | {status} | {conf:.2f}", (x1, max(25, y1 - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)
        elif status != "DISABLED":
            cv2.putText(frame, "Queen: SCANNING...", (15, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)

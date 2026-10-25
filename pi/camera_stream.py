#!/usr/bin/env python3
"""
Raspberry Pi camera stream server (runs ON THE PI).

Serves the hive camera as an MJPEG stream that the Flask app reads:

    http://<pi-ip>:8000/stream.mjpg     live video
    http://<pi-ip>:8000/snapshot.jpg    single picture
    http://<pi-ip>:8000/health          JSON status

Works with a USB webcam (OpenCV) or the Raspberry Pi Camera Module (picamera2).

    python3 camera_stream.py --camera usb --device 0
    python3 camera_stream.py --camera picam
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2


class FrameSource(threading.Thread):
    """Captures frames in one thread; every viewer gets the latest JPEG."""

    def __init__(self, camera, device, width, height, fps, quality):
        super().__init__(daemon=True)
        self.camera = camera
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality

        self.cond = threading.Condition()
        self.jpeg = None
        self.frame_id = 0
        self.last_frame_at = None
        self.error = None

    # -- camera backends ------------------------------------------------
    def _frames_usb(self):
        cap = cv2.VideoCapture(self.device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open USB camera {self.device}")
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("USB camera stopped sending frames")
                yield frame
        finally:
            cap.release()

    def _frames_picam(self):
        from picamera2 import Picamera2  # installed on Raspberry Pi OS

        picam = Picamera2()
        config = picam.create_video_configuration(
            main={"size": (self.width, self.height), "format": "RGB888"}
        )
        picam.configure(config)
        picam.start()
        try:
            while True:
                # "RGB888" in picamera2 is BGR byte order, which is what OpenCV expects
                yield picam.capture_array()
        finally:
            picam.stop()

    # -- capture loop -----------------------------------------------------
    def run(self):
        delay = 1.0 / self.fps
        while True:
            try:
                frames = self._frames_picam() if self.camera == "picam" else self._frames_usb()
                for frame in frames:
                    t0 = time.time()
                    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                    if ok:
                        with self.cond:
                            self.jpeg = buf.tobytes()
                            self.frame_id += 1
                            self.last_frame_at = time.time()
                            self.error = None
                            self.cond.notify_all()
                    spare = delay - (time.time() - t0)
                    if spare > 0:
                        time.sleep(spare)
            except Exception as e:
                self.error = str(e)
                print("Camera error:", e, "- retrying in 5 s")
                time.sleep(5)

    def wait_for_frame(self, last_id, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.frame_id != last_id, timeout=timeout)
            return self.frame_id, self.jpeg


def make_handler(source):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep the console quiet

        def do_GET(self):
            if self.path.startswith("/stream.mjpg"):
                self._stream()
            elif self.path.startswith("/snapshot.jpg"):
                _, jpeg = source.wait_for_frame(-1)
                if jpeg is None:
                    self.send_error(503, "No frame yet")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpeg)))
                self.end_headers()
                self.wfile.write(jpeg)
            elif self.path.startswith("/health"):
                body = json.dumps({
                    "ok": source.error is None and source.jpeg is not None,
                    "camera": source.camera,
                    "error": source.error,
                    "last_frame_age_seconds": (
                        round(time.time() - source.last_frame_at, 1) if source.last_frame_at else None
                    ),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def _stream(self):
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            last_id = -1
            try:
                while True:
                    frame_id, jpeg = source.wait_for_frame(last_id)
                    if jpeg is None or frame_id == last_id:
                        continue
                    last_id = frame_id
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                    self.wfile.write(jpeg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # viewer disconnected

    return Handler


def main():
    p = argparse.ArgumentParser(description="Hive camera MJPEG server")
    p.add_argument("--camera", choices=["usb", "picam"], default="usb")
    p.add_argument("--device", type=int, default=0, help="USB camera index")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    source = FrameSource(args.camera, args.device, args.width, args.height, args.fps, args.quality)
    source.start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(source))
    server.daemon_threads = True
    print(f"Streaming {args.camera} camera on http://0.0.0.0:{args.port}/stream.mjpg")
    server.serve_forever()


if __name__ == "__main__":
    main()

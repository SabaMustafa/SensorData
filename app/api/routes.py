"""
JSON API (version 1), mounted at /api/v1.

Used by the web dashboard, the Raspberry Pi and, later, the mobile app.

  GET  /health                         server is running
  GET  /hives                          list hives
  GET  /hives/<id>                     one hive
  GET  /hives/<id>/status              latest sensors + queen + camera (dashboard polls this)
  POST /hives/<id>/readings            Pi sends sensor readings (needs X-API-Key)
  GET  /hives/<id>/readings?hours=24   sensor history for charts
  GET  /hives/<id>/queen/events        queen seen / not seen history
  POST /hives/<id>/audio-analysis      Pi sends audio analysis results (needs X-API-Key)
  GET  /hives/<id>/audio-analysis      analysis history
  PUT  /hives/<id>/recordings/<file>   Pi uploads one WAV recording (needs X-API-Key)
  GET  /hives/<id>/recordings          list of recordings
  GET  /hives/<id>/recordings/<file>   download / play one recording
  POST /detect                         queen detection on one uploaded image
"""
import base64
import os
import re
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from flask import Blueprint, current_app, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from ..extensions import db
from ..models import (
    AudioAnalysis,
    AudioRecording,
    Hive,
    QueenEvent,
    SensorReading,
    iso,
    utcnow,
)

api_bp = Blueprint("api", __name__)

# Accepted sensor fields and their valid ranges.
# Short aliases (e.g. "temperature") are accepted from the Pi as well.
SENSOR_FIELDS = {
    "temperature_c": (-40.0, 85.0),
    "humidity_pct": (0.0, 100.0),
    "weight_kg": (0.0, 300.0),
}
ALIASES = {"temperature": "temperature_c", "humidity": "humidity_pct", "weight": "weight_kg"}
MAX_BATCH = 500


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@api_bp.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"error": e.message}), e.status


@api_bp.errorhandler(HTTPException)
def handle_http_error(e):
    return jsonify({"error": e.description}), e.code


def get_hive_or_404(hive_id):
    hive = db.session.get(Hive, hive_id)
    if hive is None:
        raise ApiError(f"Hive {hive_id} not found", 404)
    return hive


def require_api_key(hive):
    if not hive.check_api_key(request.headers.get("X-API-Key")):
        raise ApiError("Missing or invalid API key", 401)


def get_camera(hive_id):
    return current_app.extensions.get("cameras", {}).get(hive_id)


# ---------------------------------------------------------------------
# General
# ---------------------------------------------------------------------
@api_bp.get("/health")
def health():
    return jsonify({"status": "ok", "server_time": iso(utcnow())})


@api_bp.get("/hives")
def list_hives():
    hives = Hive.query.order_by(Hive.id).all()
    return jsonify({"hives": [h.to_dict() for h in hives]})


@api_bp.get("/hives/<int:hive_id>")
def get_hive(hive_id):
    return jsonify(get_hive_or_404(hive_id).to_dict())


# ---------------------------------------------------------------------
# Live status (dashboard and mobile app poll this every few seconds)
# ---------------------------------------------------------------------
@api_bp.get("/hives/<int:hive_id>/status")
def hive_status(hive_id):
    hive = get_hive_or_404(hive_id)

    latest = (
        SensorReading.query.filter_by(hive_id=hive.id)
        .order_by(SensorReading.recorded_at.desc())
        .first()
    )
    offline_after = current_app.config["SENSOR_OFFLINE_AFTER_SECONDS"]
    sensors_online = bool(
        latest and (utcnow() - latest.received_at).total_seconds() <= offline_after
    )

    last_seen = (
        QueenEvent.query.filter_by(hive_id=hive.id, queen_present=True)
        .order_by(QueenEvent.timestamp.desc())
        .first()
    )

    camera = get_camera(hive.id)
    if camera:
        cam = camera.status()
        last_frame_age = time.time() - cam["last_frame_at"] if cam["last_frame_at"] else None
        camera_info = {
            "mode": cam["mode"],
            "live_source_configured": cam["live_source_configured"],
            "last_frame_age_seconds": round(last_frame_age, 1) if last_frame_age is not None else None,
            "detection_enabled": cam["detection_enabled"],
            "detection_error": cam["detection_error"],
        }
        queen = cam["queen"]
    else:
        camera_info = {"mode": "disabled"}
        queen = {"status": "UNAVAILABLE", "present": False, "confidence": 0.0}

    queen["last_seen_live"] = iso(last_seen.timestamp) if last_seen else None

    return jsonify({
        "hive": hive.to_dict(),
        "server_time": iso(utcnow()),
        "sensors": {
            "online": sensors_online,
            "latest": latest.to_dict() if latest else None,
        },
        "queen": queen,
        "camera": camera_info,
    })


# ---------------------------------------------------------------------
# Sensor readings
# ---------------------------------------------------------------------
def _parse_time(value):
    if value in (None, ""):
        return utcnow()
    if isinstance(value, (int, float)):  # unix timestamp
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(f"Invalid recorded_at: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    if dt > utcnow() + timedelta(minutes=5):
        raise ApiError("recorded_at is in the future (check the Pi clock)")
    return dt


def _parse_reading(item):
    if not isinstance(item, dict):
        raise ApiError("Each reading must be a JSON object")

    values = {}
    extra = {}
    for key, value in item.items():
        key = ALIASES.get(key, key)
        if key in ("recorded_at", "extra"):
            continue
        if key in SENSOR_FIELDS:
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ApiError(f"{key} must be a number")
            low, high = SENSOR_FIELDS[key]
            if not low <= value <= high:
                raise ApiError(f"{key}={value} is outside the valid range {low}..{high}")
            values[key] = value
        elif isinstance(value, (int, float, str, bool)) or value is None:
            # Unknown sensors are kept, so new sensors need no code change
            extra[key] = value

    if isinstance(item.get("extra"), dict):
        extra.update(item["extra"])

    if not values and not extra:
        raise ApiError("Reading contains no sensor values")

    return SensorReading(
        recorded_at=_parse_time(item.get("recorded_at")),
        extra=extra or None,
        **values,
    )


@api_bp.post("/hives/<int:hive_id>/readings")
def add_readings(hive_id):
    hive = get_hive_or_404(hive_id)

    if not hive.check_api_key(request.headers.get("X-API-Key")):
        raise ApiError("Missing or invalid API key", 401)

    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError("Request body must be JSON")

    # Accept one reading, a list, or {"readings": [...]} (Pi sending a backlog)
    if isinstance(payload, dict) and "readings" in payload:
        items = payload["readings"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = [payload]

    if not isinstance(items, list) or not items:
        raise ApiError("No readings provided")
    if len(items) > MAX_BATCH:
        raise ApiError(f"Too many readings in one request (max {MAX_BATCH})")

    readings = [_parse_reading(item) for item in items]

    # The Pi re-sends a batch if it never got our answer (Wi-Fi drop), so a
    # reading already stored for that hive and time is skipped, not doubled.
    times = [r.recorded_at for r in readings]
    seen = {
        t for (t,) in db.session.query(SensorReading.recorded_at).filter(
            SensorReading.hive_id == hive.id, SensorReading.recorded_at.in_(times)
        )
    }
    new = []
    for r in readings:
        if r.recorded_at in seen:
            continue
        seen.add(r.recorded_at)
        r.hive_id = hive.id
        new.append(r)
    db.session.add_all(new)
    db.session.commit()

    return jsonify({
        "saved": len(new),
        "duplicates": len(readings) - len(new),
        "latest": readings[-1].to_dict(),
    }), 201


def _downsample(rows, max_points):
    """Averages readings into buckets so charts stay fast for long periods."""
    if len(rows) <= max_points:
        return [r.to_dict() for r in rows]

    size = -(-len(rows) // max_points)  # ceiling division
    points = []
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        point = {"recorded_at": iso(chunk[len(chunk) // 2].recorded_at)}
        for field in SENSOR_FIELDS:
            vals = [getattr(r, field) for r in chunk if getattr(r, field) is not None]
            point[field] = round(sum(vals) / len(vals), 2) if vals else None
        points.append(point)
    return points


@api_bp.get("/hives/<int:hive_id>/readings")
def reading_history(hive_id):
    hive = get_hive_or_404(hive_id)

    hours = request.args.get("hours", default=24, type=float)
    max_points = request.args.get("max_points", default=500, type=int)
    if not 0 < hours <= 24 * 366:
        raise ApiError("hours must be between 0 and 8784")
    max_points = min(max(max_points, 10), 5000)

    since = utcnow() - timedelta(hours=hours)
    rows = (
        SensorReading.query.filter(
            SensorReading.hive_id == hive.id, SensorReading.recorded_at >= since
        )
        .order_by(SensorReading.recorded_at)
        .all()
    )

    return jsonify({
        "hive_id": hive.id,
        "hours": hours,
        "count": len(rows),
        "points": _downsample(rows, max_points),
    })


@api_bp.get("/hives/<int:hive_id>/queen/events")
def queen_events(hive_id):
    hive = get_hive_or_404(hive_id)
    hours = request.args.get("hours", default=24, type=float)
    since = utcnow() - timedelta(hours=hours)

    events = (
        QueenEvent.query.filter(QueenEvent.hive_id == hive.id, QueenEvent.timestamp >= since)
        .order_by(QueenEvent.timestamp.desc())
        .limit(500)
        .all()
    )
    return jsonify({"hive_id": hive.id, "events": [e.to_dict() for e in events]})


# ---------------------------------------------------------------------
# Audio from the Pi: analysis results and the recordings themselves
# ---------------------------------------------------------------------
ANALYSIS_NUMBERS = (
    "duration_s", "rms_dbfs", "peak_dbfs", "spl_estimate_db",
    "calibration_db_offset", "dominant_hz",
)
# Recording names like 20261025_125215.WAV; nothing that could leave the folder
SAFE_WAV_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,200}\.wav$", re.IGNORECASE)


def _number(item, key, kind=float):
    value = item.get(key)
    if value is None or value == "":
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ApiError(f"{key} must be a number")


@api_bp.post("/hives/<int:hive_id>/audio-analysis")
def add_audio_analysis(hive_id):
    hive = get_hive_or_404(hive_id)
    require_api_key(hive)

    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError("Request body must be JSON")
    # Accept one result, a list, or {"results": [...]}
    if isinstance(payload, dict) and "results" in payload:
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    else:
        items = [payload]
    if not isinstance(items, list) or not items:
        raise ApiError("No results provided")
    if len(items) > MAX_BATCH:
        raise ApiError(f"Too many results in one request (max {MAX_BATCH})")

    rows = []
    for item in items:
        if not isinstance(item, dict) or not item.get("filename"):
            raise ApiError("Each result must be a JSON object with a filename")
        row = AudioAnalysis(
            hive_id=hive.id,
            filename=str(item["filename"])[:200],
            recorded_at=_parse_time(item.get("recorded_at")),
            analysed_at=_parse_time(item.get("analysed_at")) if item.get("analysed_at") else None,
            sample_rate_hz=_number(item, "sample_rate_hz", int),
            band_hz=str(item.get("band_hz") or "")[:50],
            audio_device=str(item.get("audio_device") or "")[:200],
            peaks=item.get("peaks") if isinstance(item.get("peaks"), list) else [],
        )
        for key in ANALYSIS_NUMBERS:
            setattr(row, key, _number(item, key))
        rows.append(row)

    # One result per recording: a result sent twice is skipped
    names = [r.filename for r in rows]
    seen = {
        n for (n,) in db.session.query(AudioAnalysis.filename).filter(
            AudioAnalysis.hive_id == hive.id, AudioAnalysis.filename.in_(names)
        )
    }
    new = []
    for r in rows:
        if r.filename not in seen:
            seen.add(r.filename)
            new.append(r)
    db.session.add_all(new)
    db.session.commit()
    return jsonify({"saved": len(new), "duplicates": len(rows) - len(new)}), 201


@api_bp.get("/hives/<int:hive_id>/audio-analysis")
def audio_analysis_history(hive_id):
    hive = get_hive_or_404(hive_id)
    hours = request.args.get("hours", default=24, type=float)
    since = utcnow() - timedelta(hours=hours)
    rows = (
        AudioAnalysis.query.filter(
            AudioAnalysis.hive_id == hive.id, AudioAnalysis.recorded_at >= since
        )
        .order_by(AudioAnalysis.recorded_at)
        .limit(5000)
        .all()
    )
    return jsonify({"hive_id": hive.id, "results": [r.to_dict() for r in rows]})


@api_bp.put("/hives/<int:hive_id>/recordings/<filename>")
def put_recording(hive_id, filename):
    """The body is the WAV file itself (Content-Type: audio/wav)."""
    hive = get_hive_or_404(hive_id)
    require_api_key(hive)

    if not SAFE_WAV_NAME.match(filename):
        raise ApiError("filename must be a plain .wav name, e.g. 20261025_125215.WAV")
    length = request.content_length
    if not length:
        raise ApiError("Empty body: send the WAV bytes with a Content-Length")
    if length > current_app.config["MAX_AUDIO_MB"] * 1024 * 1024:
        raise ApiError(f"Recording is larger than {current_app.config['MAX_AUDIO_MB']} MB", 413)
    recorded_at = _parse_time(request.headers.get("X-Recorded-At"))

    existing = AudioRecording.query.filter_by(hive_id=hive.id, filename=filename).first()
    if existing and existing.size_bytes == length and os.path.exists(existing.path):
        return jsonify({"status": "exists", "recording": existing.to_dict()}), 200

    folder = os.path.join(current_app.config["AUDIO_DIR"], str(hive.id))
    os.makedirs(folder, exist_ok=True)
    final = os.path.join(folder, filename)
    partial = final + ".part"

    # Written to .part first, so a dropped connection never leaves half a file
    written = 0
    with open(partial, "wb") as f:
        while True:
            chunk = request.stream.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    if written != length:
        os.remove(partial)
        raise ApiError(f"Upload incomplete: received {written} of {length} bytes")
    os.replace(partial, final)

    recording = existing or AudioRecording(hive_id=hive.id, filename=filename)
    recording.recorded_at = recorded_at
    recording.received_at = utcnow()
    recording.size_bytes = written
    recording.path = final
    db.session.add(recording)
    db.session.commit()
    return jsonify({"status": "saved", "recording": recording.to_dict()}), 201


@api_bp.get("/hives/<int:hive_id>/recordings")
def list_recordings(hive_id):
    hive = get_hive_or_404(hive_id)
    hours = request.args.get("hours", default=24, type=float)
    since = utcnow() - timedelta(hours=hours)
    rows = (
        AudioRecording.query.filter(
            AudioRecording.hive_id == hive.id, AudioRecording.recorded_at >= since
        )
        .order_by(AudioRecording.recorded_at.desc())
        .limit(1000)
        .all()
    )
    return jsonify({"hive_id": hive.id, "recordings": [r.to_dict() for r in rows]})


@api_bp.get("/hives/<int:hive_id>/recordings/<filename>")
def get_recording(hive_id, filename):
    hive = get_hive_or_404(hive_id)
    recording = AudioRecording.query.filter_by(hive_id=hive.id, filename=filename).first()
    if recording is None or not os.path.exists(recording.path):
        raise ApiError(f"Recording {filename} not found", 404)
    return send_file(recording.path, mimetype="audio/wav", conditional=True)


# ---------------------------------------------------------------------
# Queen detection on a single image (mobile page / future mobile app)
# ---------------------------------------------------------------------
def _read_image():
    import cv2

    if "image" in request.files:
        data = request.files["image"].read()
    else:
        payload = request.get_json(silent=True) or {}
        img_b64 = payload.get("image")
        if not img_b64:
            raise ApiError("No image received (send JSON {'image': <base64>} or a file 'image')")
        if "," in img_b64:  # data URL: "data:image/jpeg;base64,...."
            img_b64 = img_b64.split(",", 1)[1]
        try:
            data = base64.b64decode(img_b64)
        except ValueError:
            raise ApiError("Image is not valid base64")

    frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ApiError("Could not decode image")
    return frame


@api_bp.post("/detect")
def detect():
    detector = current_app.extensions.get("queen_detector")
    if detector is None:
        raise ApiError("Queen detection is disabled on this server", 503)

    frame = _read_image()
    bbox, conf = detector.detect(frame)

    if bbox is None:
        return jsonify({"status": "SCANNING", "box": None, "conf": 0.0})

    x1, y1, x2, y2 = bbox
    return jsonify({
        "status": "TRACKING",
        "box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        "conf": conf,
        "image_size": {"width": frame.shape[1], "height": frame.shape[0]},
    })

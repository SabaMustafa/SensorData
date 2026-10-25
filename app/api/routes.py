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
  POST /detect                         queen detection on one uploaded image
"""
import base64
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

from ..extensions import db
from ..models import Hive, QueenEvent, SensorReading, iso, utcnow

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
    for r in readings:
        r.hive_id = hive.id
    db.session.add_all(readings)
    db.session.commit()

    return jsonify({"saved": len(readings), "latest": readings[-1].to_dict()}), 201


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

import os

from flask import Flask

from config import Config
from .extensions import db
from .models import Hive, QueenEvent, hash_api_key
from .services.camera import HiveCamera
from .services.detector import QueenDetector


def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)

    from .api.routes import api_bp
    from .dashboard.routes import dashboard_bp
    from .streaming.routes import streaming_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(streaming_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    with app.app_context():
        db.create_all()
        _ensure_default_hive(app)

    # Shared services, available in routes as current_app.extensions[...]
    detector = None
    if app.config["DETECTION_ENABLED"]:
        detector = QueenDetector(app.config["QUEEN_MODEL_PATH"], conf=app.config["QUEEN_CONF"])
    app.extensions["queen_detector"] = detector
    app.extensions["cameras"] = {}

    if app.config["CAMERA_ENABLED"]:
        _start_cameras(app, detector)

    return app


def _ensure_default_hive(app):
    """Creates "Hive 1" on first start. More hives can be added later."""
    if Hive.query.first() is None:
        hive = Hive(
            name=app.config["DEFAULT_HIVE_NAME"],
            location=app.config["DEFAULT_HIVE_LOCATION"],
            api_key_hash=hash_api_key(app.config["DEFAULT_HIVE_API_KEY"]),
        )
        db.session.add(hive)
        db.session.commit()
        print(f"Created default hive: {hive.name} (id={hive.id})")


def _start_cameras(app, detector):
    def save_queen_event(hive_id, status, present, conf):
        with app.app_context():
            db.session.add(QueenEvent(
                hive_id=hive_id, status=status, queen_present=present, confidence=conf
            ))
            db.session.commit()

    # One camera for now (CAMERA_URL in .env). With more hives, each hive
    # would get its own camera URL stored in the database.
    with app.app_context():
        hive = Hive.query.order_by(Hive.id).first()

    camera = HiveCamera(
        hive_id=hive.id,
        live_source=app.config["CAMERA_URL"],
        demo_path=app.config["DEMO_VIDEO_PATH"],
        fallback_to_demo=app.config["CAMERA_FALLBACK_TO_DEMO"],
        retry_seconds=app.config["CAMERA_RETRY_SECONDS"],
        detector=detector,
        detect_every_n_frames=app.config["DETECT_EVERY_N_FRAMES"],
        jpeg_quality=app.config["STREAM_JPEG_QUALITY"],
        on_queen_change=save_queen_event,
    )
    camera.start()
    app.extensions["cameras"][hive.id] = camera

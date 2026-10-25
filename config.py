import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load settings from a local .env file (copy .env.example to .env)
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Config:
    # ---------------------------------------------------------
    # Flask
    # ---------------------------------------------------------
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-env-file")

    # ---------------------------------------------------------
    # Database
    # SQLite now. To move to PostgreSQL later, only change DATABASE_URL, e.g.
    # postgresql+psycopg://user:password@localhost/beehive
    # ---------------------------------------------------------
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "sqlite:///" + os.path.join(BASE_DIR, "instance", "beehive.db"),
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ---------------------------------------------------------
    # Default hive (created automatically on first start)
    # ---------------------------------------------------------
    DEFAULT_HIVE_NAME = os.getenv("DEFAULT_HIVE_NAME", "Hive 1")
    DEFAULT_HIVE_LOCATION = os.getenv("DEFAULT_HIVE_LOCATION", "Garden")
    # The Raspberry Pi must send this key in the X-API-Key header
    DEFAULT_HIVE_API_KEY = os.getenv("DEFAULT_HIVE_API_KEY", "dev-hive-1-key")

    # ---------------------------------------------------------
    # Camera
    # CAMERA_URL: the Pi stream, e.g. http://192.168.1.50:8000/stream.mjpg
    #             or "0" for a webcam plugged into this computer.
    # If the live camera is unavailable, the demo video is shown instead
    # (when CAMERA_FALLBACK_TO_DEMO is true) and the live camera is retried.
    # ---------------------------------------------------------
    CAMERA_ENABLED = _bool("CAMERA_ENABLED", True)
    CAMERA_URL = os.getenv("CAMERA_URL", "").strip()
    CAMERA_FALLBACK_TO_DEMO = _bool("CAMERA_FALLBACK_TO_DEMO", True)
    CAMERA_RETRY_SECONDS = int(os.getenv("CAMERA_RETRY_SECONDS", "30"))
    DEMO_VIDEO_PATH = os.getenv(
        "DEMO_VIDEO_PATH", os.path.join(BASE_DIR, "data", "demo", "DemoVideo.mp4")
    )
    STREAM_JPEG_QUALITY = int(os.getenv("STREAM_JPEG_QUALITY", "80"))

    # ---------------------------------------------------------
    # Queen detection
    # 0.17 was selected from the validation/F1-based threshold analysis
    # ---------------------------------------------------------
    DETECTION_ENABLED = _bool("DETECTION_ENABLED", True)
    QUEEN_MODEL_PATH = os.getenv(
        "QUEEN_MODEL_PATH", os.path.join(BASE_DIR, "models", "best.pt")
    )
    QUEEN_CONF = float(os.getenv("QUEEN_CONF", "0.17"))
    # Run YOLO on every Nth frame to save CPU (1 = every frame)
    DETECT_EVERY_N_FRAMES = int(os.getenv("DETECT_EVERY_N_FRAMES", "2"))

    # ---------------------------------------------------------
    # Sensors
    # Hive is shown as "offline" if no reading arrives within this time
    # ---------------------------------------------------------
    SENSOR_OFFLINE_AFTER_SECONDS = int(os.getenv("SENSOR_OFFLINE_AFTER_SECONDS", "180"))

    # ---------------------------------------------------------
    # Audio from the Pi (bee-node)
    # Recordings are saved as files here, one folder per hive.
    # A 5-minute 16 kHz recording is about 10 MB.
    # ---------------------------------------------------------
    AUDIO_DIR = os.getenv("AUDIO_DIR", os.path.join(BASE_DIR, "instance", "audio"))
    MAX_AUDIO_MB = int(os.getenv("MAX_AUDIO_MB", "500"))

    # ---------------------------------------------------------
    # Model information shown on the dashboard
    # ---------------------------------------------------------
    MODEL_INFO = {
        "name": "YOLOv8n Baseline",
        "size": "5.96 MB",
        "parameters": "3.01M",
        "cpu_fps": "34.70 FPS",
    }

    DASHBOARD_TITLE = "Queen Bee Watch"
    DASHBOARD_SUBTITLE = "Live hive camera, queen detection and sensor monitoring"


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CAMERA_ENABLED = False
    DETECTION_ENABLED = False
    DEFAULT_HIVE_API_KEY = "test-key"
    AUDIO_DIR = os.path.join(BASE_DIR, "instance", "test-audio")

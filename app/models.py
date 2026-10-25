"""
Database tables.

Everything is linked to a Hive from the start, so adding more hives later
needs no database redesign. For now there is one hive ("Hive 1").
"""
import hashlib
from datetime import datetime, timezone

from .extensions import db


def utcnow():
    # Stored as naive UTC (SQLite has no timezone support)
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def hash_api_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Hive(db.Model):
    __tablename__ = "hives"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(200))
    # Only the hash is stored, never the key itself
    api_key_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    readings = db.relationship("SensorReading", back_populates="hive", lazy="dynamic")

    def check_api_key(self, key):
        return bool(key) and hash_api_key(key) == self.api_key_hash

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "location": self.location,
            "created_at": iso(self.created_at),
        }


class SensorReading(db.Model):
    __tablename__ = "sensor_readings"

    id = db.Column(db.Integer, primary_key=True)
    hive_id = db.Column(db.Integer, db.ForeignKey("hives.id"), nullable=False)
    # When the Pi measured it (may be earlier than received_at if the Pi was offline)
    recorded_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    temperature_c = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
    weight_kg = db.Column(db.Float)
    # Any other sensor values (sound level, outside temperature, ...) as JSON
    extra = db.Column(db.JSON)

    hive = db.relationship("Hive", back_populates="readings")

    __table_args__ = (db.Index("ix_readings_hive_time", "hive_id", "recorded_at"),)

    def to_dict(self):
        return {
            "id": self.id,
            "hive_id": self.hive_id,
            "recorded_at": iso(self.recorded_at),
            "temperature_c": self.temperature_c,
            "humidity_pct": self.humidity_pct,
            "weight_kg": self.weight_kg,
            "extra": self.extra or {},
        }


class QueenEvent(db.Model):
    """
    One row each time the queen status changes (e.g. SCANNING -> LOCKED).
    Used for "queen last seen" and history, without storing every frame.
    """
    __tablename__ = "queen_events"

    id = db.Column(db.Integer, primary_key=True)
    hive_id = db.Column(db.Integer, db.ForeignKey("hives.id"), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow)
    status = db.Column(db.String(30), nullable=False)
    queen_present = db.Column(db.Boolean, nullable=False)
    confidence = db.Column(db.Float)

    __table_args__ = (db.Index("ix_queen_events_hive_time", "hive_id", "timestamp"),)

    def to_dict(self):
        return {
            "timestamp": iso(self.timestamp),
            "status": self.status,
            "queen_present": self.queen_present,
            "confidence": self.confidence,
        }

#!/usr/bin/env python3
"""
Rescue Radar - app.py (Railway-ready, destructive DB reset support)

This Flask single-file backend:
- Uses DATABASE_URL if provided (Railway). Falls back to sqlite in instance/.
- Exposes endpoints:
  - GET  /api/v1/readings/latest        -> latest reading (JSON)
  - GET  /api/v1/readings/all           -> list recent readings
  - POST /api/v1/readings               -> create/update reading (requires x-api-key)
  - POST /admin/reset-db                -> DROP ALL TABLES then CREATE TABLES (requires x-api-key)
  - POST /admin/init-db                 -> create tables if missing (requires x-api-key)
- Model includes the requested fields: detected (bool), range_cm (float), angle_deg (float).
- Safe-by-default: reset only runs when you call /admin/reset-db with the correct API key.
- WARNING: /admin/reset-db is destructive — it drops all tables and data.

Run: python3 app.py
"""

import os
import uuid
import logging
from datetime import datetime
from io import BytesIO
import pkgutil
import importlib.util

# ensure pkgutil.get_loader exists on odd Python builds
if not hasattr(pkgutil, "get_loader"):
    def _compat_get_loader(name):
        try:
            spec = importlib.util.find_spec(name)
            return spec.loader if spec is not None else None
        except Exception:
            return None
    pkgutil.get_loader = _compat_get_loader

from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# Optional socketio (real-time) — not required for reset/migrations
try:
    from flask_socketio import SocketIO
    SOCKETIO_AVAILABLE = True
except Exception:
    SocketIO = None
    SOCKETIO_AVAILABLE = False

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rescue_radar")

# Flask app
app = Flask(__name__)
CORS(app)

# Socket.IO (if installed)
if SOCKETIO_AVAILABLE:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
    logger.info("✔ flask_socketio available — realtime enabled")
else:
    socketio = None
    logger.info("ℹ flask_socketio not installed — realtime disabled")

# Instance dir & DB path (sqlite fallback)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)
sqlite_path = os.path.join(INSTANCE_DIR, "rescue_radar.db")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    # If using Railway-provided DATABASE_URL it likely includes the driver (postgres/mysql)
    # SQLAlchemy will accept it as-is (e.g. mysql+pymysql://... or postgresql://...)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    logger.info("Using DATABASE_URL from environment")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{sqlite_path}"
    logger.info("Using local SQLite database at %s", sqlite_path)

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "rescue-radar-dev")

db = SQLAlchemy(app)

# ---------------- Model ----------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, unique=True, index=True)

    # Required fields
    detected = db.Column(db.Boolean, nullable=False, default=False)
    range_cm = db.Column(db.Float, nullable=True)
    angle_deg = db.Column(db.Float, nullable=True)

    # legacy / optional fields kept for compatibility
    distance_cm = db.Column(db.Float, nullable=True)
    temperature_c = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
    gas_ppm = db.Column(db.Float)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)

    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        ts = self.timestamp
        iso = ts.isoformat() + "Z" if isinstance(ts, datetime) else str(ts)
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "detected": bool(self.detected),
            "range_cm": self.range_cm,
            "angle_deg": self.angle_deg,
            "distance_cm": self.distance_cm,
            "temperature_c": self.temperature_c,
            "humidity_pct": self.humidity_pct,
            "gas_ppm": self.gas_ppm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": iso,
        }

# ---------------- Helpers ----------------
def require_key(req):
    return req.headers.get("x-api-key") == WRITE_API_KEY

def parse_bool(v):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")

def to_float(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

# ---------------- Routes ----------------
@app.route("/")
def home():
    latest = VictimReading.query.order_by(VictimReading.timestamp.desc()).first()
    if not latest:
        return "<h2>Rescue Radar</h2><p>No readings yet.</p>", 200
    status = "DETECTED" if latest.detected else "NO PERSON"
    return (
        f"<h2>Rescue Radar</h2>"
        f"<p>Status: {status}</p>"
        f"<p>Range: {latest.range_cm if latest.range_cm is not None else (latest.distance_cm if latest.distance_cm is not None else 'N/A')} cm</p>"
        f"<p>Angle: {latest.angle_deg if latest.angle_deg is not None else 'N/A'}°</p>"
        f"<p>Victim: {latest.victim_id} • {latest.timestamp} UTC</p>"
    ), 200

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    # minimal request logging for debugging
    try:
        logger.info("Incoming request headers:")
        for k, v in request.headers.items():
            logger.info("%s: %s", k, v)
        logger.info("Incoming raw body: %s", request.get_data(as_text=True))
    except Exception:
        logger.exception("failed to log request")

    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # accept new + legacy keys
    detected = parse_bool(data.get("detected", data.get("person_detected", data.get("found"))))
    raw_range = data.get("range_cm", data.get("range", data.get("distance_cm", data.get("distance"))))
    range_cm = to_float(raw_range)
    raw_angle = data.get("angle_deg", data.get("angle"))
    angle_deg = to_float(raw_angle)

    distance_cm = to_float(data.get("distance_cm", data.get("distance")))
    temperature = to_float(data.get("temperature"))
    humidity = to_float(data.get("humidity"))
    gas = to_float(data.get("gas"))
    latitude = to_float(data.get("latitude"))
    longitude = to_float(data.get("longitude"))

    victim_id = data.get("victim_id") or f"vic-{uuid.uuid4().hex[:8]}"

    reading = VictimReading.query.filter_by(victim_id=victim_id).first()
    if reading:
        reading.detected = detected
        reading.range_cm = range_cm if range_cm is not None else reading.range_cm
        reading.angle_deg = angle_deg if angle_deg is not None else reading.angle_deg
        reading.distance_cm = distance_cm if distance_cm is not None else reading.distance_cm
        reading.temperature_c = temperature if temperature is not None else reading.temperature_c
        reading.humidity_pct = humidity if humidity is not None else reading.humidity_pct
        reading.gas_ppm = gas if gas is not None else reading.gas_ppm
        reading.latitude = latitude if latitude is not None else reading.latitude
        reading.longitude = longitude if longitude is not None else reading.longitude
        reading.timestamp = datetime.utcnow()
        action = "UPDATED"
    else:
        reading = VictimReading(
            victim_id=victim_id,
            detected=detected,
            range_cm=range_cm if range_cm is not None else distance_cm,
            angle_deg=angle_deg,
            distance_cm=distance_cm,
            temperature_c=temperature,
            humidity_pct=humidity,
            gas_ppm=gas,
            latitude=latitude,
            longitude=longitude,
            timestamp=datetime.utcnow(),
        )
        db.session.add(reading)
        action = "CREATED"

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("DB commit failed")
        return jsonify({"error": "database error"}), 500

    # Emit socket event if available
    if SOCKETIO_AVAILABLE:
        try:
            socketio.emit("reading_update", {"reading": reading.to_dict()})
        except Exception:
            logger.exception("socket emit failed")

    logger.info("%s victim %s detected=%s range=%s angle=%s", action, victim_id, reading.detected, reading.range_cm, reading.angle_deg)
    return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200

@app.route("/api/v1/readings/latest", methods=["GET"])
def latest_reading():
    latest = VictimReading.query.order_by(VictimReading.timestamp.desc()).first()
    if not latest:
        return jsonify({"reading": None}), 200
    return jsonify({"reading": latest.to_dict()}), 200

@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 500)
    q = VictimReading.query.order_by(VictimReading.timestamp.desc())
    items = q.limit(per_page).offset((page - 1) * per_page).all()
    total = q.count()
    return jsonify({
        "readings": [r.to_dict() for r in items],
        "page": page,
        "per_page": per_page,
        "total": total,
    }), 200

# ---------------- Admin (destructive) ----------------
@app.route("/admin/reset-db", methods=["POST"])
def reset_db():
    """
    Destructive: drops ALL tables then recreates them from the model.
    Requires x-api-key header.
    Intended for controlled use (e.g. dev / one-off migrations).
    """
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        logger.warning("ADMIN RESET DB requested - dropping all tables")
        # Drop all tables (destructive)
        db.drop_all()
        # Recreate tables from models
        db.create_all()
        logger.warning("ADMIN RESET DB completed - new schema created")
        return jsonify({"status": "ok", "msg": "Dropped and recreated all tables"}), 200
    except Exception:
        logger.exception("admin reset-db failed")
        return jsonify({"error": "reset failed"}), 500

@app.route("/admin/init-db", methods=["POST"])
def init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        db.create_all()
        return jsonify({"status": "ok"}), 200
    except Exception:
        logger.exception("init-db failed")
        return jsonify({"error": "init-db failed"}), 500

# ---------------- Startup ----------------
if __name__ == "__main__":
    # create tables on startup (non-destructive)
    with app.app_context():
        try:
            db.create_all()
            logger.info("DB tables ensured (did not drop existing)")
        except Exception:
            logger.exception("db.create_all failed on startup")

    port = int(os.environ.get("PORT", 5001))
    # Run with socketio if available (keeps compatibility)
    if SOCKETIO_AVAILABLE:
        logger.info("Running with Socket.IO on port %d", port)
        socketio.run(app, host="0.0.0.0", port=port)
    else:
        logger.info("Running Flask on port %d", port)
        app.run(host="0.0.0.0", port=port)

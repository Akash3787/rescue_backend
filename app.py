import os
import uuid
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("rescue_radar")

# ---------------- Flask App ----------------
app = Flask(__name__)
CORS(app)

# ---------------- Optional Socket.IO ----------------
try:
    from flask_socketio import SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
    SOCKETIO_AVAILABLE = True
    logger.info("✔ flask_socketio available — realtime enabled")
except Exception:
    socketio = None
    SOCKETIO_AVAILABLE = False
    logger.warning("⚠ flask_socketio NOT installed — realtime disabled")

# ---------------- Database ----------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    logger.info("Using DATABASE_URL")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///rescue_radar.db?check_same_thread=False"
    logger.info("Using local SQLite database")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "rescue-radar-dev")

db = SQLAlchemy(app)

# ---------------- Model ----------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    distance_cm = db.Column(db.Float, nullable=False)
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
            "distance_cm": self.distance_cm,
            "temperature_c": self.temperature_c,
            "humidity_pct": self.humidity_pct,
            "gas_ppm": self.gas_ppm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": iso,
        }

# ---------------- Helper ----------------
def require_key(req):
    return req.headers.get("x-api-key") == WRITE_API_KEY

# ---------------- Routes ----------------
@app.route("/")
def home():
    latest = VictimReading.query.order_by(VictimReading.timestamp.desc()).first()
    if not latest:
        return "<h2>Rescue Radar</h2><p>No readings yet.</p>"
    return f"<h2>Latest distance: {latest.distance_cm:.1f} cm</h2><p>Victim: {latest.victim_id} @ {latest.timestamp} UTC</p>"

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    raw_distance = data.get("distance_cm", data.get("distance"))
    if raw_distance is None:
        return jsonify({"error": "distance_cm required"}), 400

    try:
        distance = float(raw_distance)
    except:
        return jsonify({"error": "Invalid distance value"}), 400

    victim_id = data.get("victim_id") or f"vic-{uuid.uuid4().hex[:8]}"

    # parse optional
    def to_float(v):
        try:
            return float(v) if v is not None else None
        except:
            return None

    temperature = to_float(data.get("temperature"))
    humidity = to_float(data.get("humidity"))
    gas = to_float(data.get("gas"))
    latitude = to_float(data.get("latitude"))
    longitude = to_float(data.get("longitude"))

    # upsert
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()
    if reading:
        reading.distance_cm = distance
        reading.temperature_c = temperature
        reading.humidity_pct = humidity
        reading.gas_ppm = gas
        reading.latitude = latitude
        reading.longitude = longitude
        reading.timestamp = datetime.utcnow()
        action = "UPDATED"
    else:
        reading = VictimReading(
            victim_id=victim_id,
            distance_cm=distance,
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

    # SOCKET.IO EMIT ONLY IF AVAILABLE
    if SOCKETIO_AVAILABLE:
        try:
            socketio.emit("reading_update", {"reading": reading.to_dict()})
        except Exception:
            logger.exception("socket emit failed")

    logger.info("%s victim %s distance=%.2f", action, victim_id, distance)
    return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200

@app.route("/api/v1/readings/all")
def all_readings():
    readings = (
        VictimReading.query.order_by(VictimReading.timestamp.desc()).limit(500).all()
    )
    return jsonify({"readings": [r.to_dict() for r in readings]})

@app.route("/admin/init-db", methods=["POST"])
def init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    db.create_all()
    return jsonify({"status": "ok"})

# ---------------- Run ----------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()

    # If socketio is installed, run via socketio
    if SOCKETIO_AVAILABLE:
        logger.info("Running with Socket.IO on port 5001")
        socketio.run(app, host="0.0.0.0", port=5001)
    else:
        logger.info("Running Flask without Socket.IO on port 5001")
        app.run(host="0.0.0.0", port=5001)

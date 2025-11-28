# app.py
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import uuid
import os
import logging

# -------------------------
# App + logging
# -------------------------
app = Flask(__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# -------------------------
# Database config
# -------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
logger.info("STARTUP: DATABASE_URL = %s", DATABASE_URL)

if not DATABASE_URL:
    logger.warning("DATABASE_URL NOT FOUND. Using local SQLite fallback.")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Ensure tables exist when imported (works for gunicorn import-time)
try:
    with app.app_context():
        db.create_all()
        logger.info("db.create_all() executed at import time")
except Exception as e:
    logger.exception("Error running db.create_all() at import time: %s", e)

# -------------------------
# API key for write ops
# -------------------------
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "secret")

def require_key(req):
    key = req.headers.get("x-api-key")
    return key == WRITE_API_KEY

# -------------------------
# DB model
# -------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True)
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": (self.timestamp.astimezone(timezone.utc).isoformat() if isinstance(self.timestamp, datetime) else str(self.timestamp)) + "Z",
        }

# -------------------------
# Routes
# -------------------------
@app.route("/")
def home():
    return jsonify({"status": "ok", "msg": "Rescue backend online."}), 200

# Admin endpoint to create tables manually (protected by API key)
@app.route("/admin/init-db", methods=["POST"])
def admin_init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with app.app_context():
            db.create_all()
        return jsonify({"status": "ok", "msg": "db.create_all() executed"}), 200
    except Exception as e:
        logger.exception("admin init-db failed: %s", e)
        return jsonify({"error": "failed", "detail": str(e)}), 500

# -------------
# CREATE / DEDUPE
# -------------
# Replace previous insert behaviour with debounce/dedupe logic.
# Server will update last row or insert a new one based on thresholds.
THRESHOLD_CM = float(os.environ.get("THRESHOLD_CM", "2.0"))   # min change to create new row
MAX_INTERVAL_S = int(os.environ.get("MAX_INTERVAL_S", "10"))  # force new row after this many seconds

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    try:
        # Ensure numeric distance (expected in cm).
        distance_cm = float(data.get("distance_cm"))
    except (TypeError, ValueError):
        return jsonify({"error": "distance_cm required and must be numeric"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    # get latest reading for this victim
    last = (
        VictimReading.query
        .filter_by(victim_id=victim_id)
        .order_by(VictimReading.timestamp.desc())
        .first()
    )

    now = datetime.now(timezone.utc)

    if last is None:
        # first time -> insert
        reading = VictimReading(
            victim_id=victim_id,
            distance_cm=distance_cm,
            latitude=latitude,
            longitude=longitude,
            timestamp=now
        )
        db.session.add(reading)
        db.session.commit()
        return jsonify({"status": "created", "reading": reading.to_dict()}), 201

    # compute delta and time difference
    try:
        last_ts = last.timestamp if isinstance(last.timestamp, datetime) else datetime.fromisoformat(str(last.timestamp))
    except Exception:
        last_ts = last.timestamp if isinstance(last.timestamp, datetime) else now

    dt = (now - last_ts).total_seconds()
    delta_cm = abs(distance_cm - float(last.distance_cm or 0.0))

    if delta_cm >= THRESHOLD_CM or dt >= MAX_INTERVAL_S:
        # meaningful change or forced by time window -> insert
        reading = VictimReading(
            victim_id=victim_id,
            distance_cm=distance_cm,
            latitude=latitude,
            longitude=longitude,
            timestamp=now
        )
        db.session.add(reading)
        db.session.commit()
        return jsonify({"status": "created", "reading": reading.to_dict()}), 201
    else:
        # small/no change -> update last row (prevents DB spam)
        last.distance_cm = distance_cm
        last.latitude = latitude
        last.longitude = longitude
        last.timestamp = now
        db.session.commit()
        return jsonify({"status": "updated", "reading": last.to_dict()}), 200

# Get all readings (newest first)
@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.desc())
        .all()
    )
    return jsonify([r.to_dict() for r in readings]), 200

# Latest for a victim
@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = (
        VictimReading.query
        .filter_by(victim_id=victim_id)
        .order_by(VictimReading.timestamp.desc())
        .first()
    )
    if reading is None:
        return jsonify({"error": "No readings for this victim"}), 404
    return jsonify(reading.to_dict()), 200

# Export PDF of all readings (ascending by time)
@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.asc())
        .all()
    )

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Title
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, height - 50, "Rescue Radar - Victim Readings Export")

    # Table header
    y = height - 90
    p.setFont("Helvetica-Bold", 10)
    headers = ["ID", "Victim", "Distance(cm)", "Lat", "Lon", "Time"]
    x_positions = [50, 100, 200, 300, 360, 420]

    for i, h in enumerate(headers):
        p.drawString(x_positions[i], y, h)

    y -= 20
    p.setFont("Helvetica", 9)

    for r in readings:
        if y < 60:
            p.showPage()
            y = height - 60
            p.setFont("Helvetica", 9)

        values = [
            str(r.id),
            (r.victim_id[:12] if r.victim_id else ""),
            f"{r.distance_cm:.2f}",
            f"{r.latitude or 0:.4f}",
            f"{r.longitude or 0:.4f}",
            (r.timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if isinstance(r.timestamp, datetime) else str(r.timestamp)),
        ]

        for i, val in enumerate(values):
            p.drawString(x_positions[i], y, val)

        y -= 16

    p.showPage()
    p.save()

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="victim_readings.pdf",
        mimetype="application/pdf",
    )

# -------------------------
# Start server when run locally
# -------------------------
if __name__ == "__main__":
    # helpful info on startup
    logger.info("Starting local Flask dev server on 0.0.0.0:5001 (debug mode).")
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5001, debug=True)

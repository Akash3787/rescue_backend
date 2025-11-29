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

from datetime import datetime, timezone, timedelta

# dedupe / update policy
DISTANCE_TOLERANCE_CM = 2.0    # treat changes <= 2 cm as the same reading
TIME_WINDOW_SEC = 30           # within 30 seconds - update existing instead of insert

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
    # store timezone-aware timestamps (we'll set UTC on insert)
    timestamp = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self):
        ts = self.timestamp
        if isinstance(ts, datetime):
            ts_iso = ts.astimezone(timezone.utc).isoformat()
        else:
            ts_iso = str(ts)
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": ts_iso + "Z",
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
THRESHOLD_CM = float(os.environ.get("THRESHOLD_CM", "2.0"))   # min change to create new row
MAX_INTERVAL_S = int(os.environ.get("MAX_INTERVAL_S", "10"))  # force new row after this many seconds

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # accept either distance_cm or distance_m (convert meters -> cm)
    if "distance_cm" in data:
        try:
            distance_cm = float(data.get("distance_cm"))
        except (TypeError, ValueError):
            return jsonify({"error": "distance_cm must be numeric"}), 400
    elif "distance_m" in data:
        try:
            distance_cm = float(data.get("distance_m")) * 100.0
        except (TypeError, ValueError):
            return jsonify({"error": "distance_m must be numeric"}), 400
    else:
        return jsonify({"error": "distance_cm or distance_m required"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])
    lat = data.get("latitude")
    lon = data.get("longitude")

    try:
        # Get the latest reading for this victim id (if any)
        last = (
            VictimReading.query
            .filter_by(victim_id=victim_id)
            .order_by(VictimReading.timestamp.desc())
            .first()
        )

        # current time (naive UTC to match SQLAlchemy default if you used datetime.utcnow)
        now = datetime.utcnow()

        if last:
            last_ts = last.timestamp
            # normalize timezone-aware timestamps to naive UTC for safe subtraction
            if getattr(last_ts, "tzinfo", None) is not None:
                last_ts = last_ts.astimezone(timezone.utc).replace(tzinfo=None)

            dt = (now - last_ts).total_seconds()
            diff = abs(distance_cm - (last.distance_cm or 0.0))

            # if change is small and within short time window => update (no duplicate)
            if diff <= DISTANCE_TOLERANCE_CM and dt <= TIME_WINDOW_SEC:
                # update only timestamp and GPS if newly provided (optional)
                last.timestamp = now
                if lat is not None:
                    last.latitude = lat
                if lon is not None:
                    last.longitude = lon
                # optionally update distance to newest reading (comment/uncomment as you prefer)
                # last.distance_cm = distance_cm

                db.session.commit()
                return jsonify({"status": "ok", "action": "updated_existing", "reading": last.to_dict()}), 200

        # otherwise insert a new reading
        new_reading = VictimReading(
            victim_id=victim_id,
            distance_cm=distance_cm,
            latitude=lat,
            longitude=lon,
            timestamp=now
        )
        db.session.add(new_reading)
        db.session.commit()
        return jsonify({"status": "ok", "action": "inserted_new", "reading": new_reading.to_dict()}), 201

    except Exception as e:
        logger.exception("Error creating reading: %s", e)
        return jsonify({"error": "Internal server error", "detail": str(e)}), 500

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
    logger.info("Starting local Flask dev server on 0.0.0.0:5001 (debug mode).")
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5001, debug=True)

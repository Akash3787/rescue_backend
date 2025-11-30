# app.py - non-blocking DB init & safer engine options
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
import uuid
import os
import logging
import threading
import time

# -----------------------------------------------------
# APP INIT
# -----------------------------------------------------
app = Flask(__name__)
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

# -----------------------------------------------------
# DATABASE CONFIG (Railway uses DATABASE_URL)
# -----------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
logger.info("STARTUP: DATABASE_URL = %s", DATABASE_URL)

if not DATABASE_URL:
    logger.warning("DATABASE_URL NOT FOUND. Using local SQLite fallback.")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

# Safer engine options to avoid long blocking connections (esp. for MySQL)
engine_opts = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
# add a short connect timeout for pymysql if using mysql
if DATABASE_URL and DATABASE_URL.startswith("mysql"):
    engine_opts["connect_args"] = {"connect_timeout": 5}
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------------------------------
# API KEY FOR SECURITY
# -----------------------------------------------------
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "secret")

def require_key(req):
    key = req.headers.get("x-api-key")
    return key == WRITE_API_KEY

# -----------------------------------------------------
# DATABASE MODEL
# -----------------------------------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True)
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    # use naive UTC datetime everywhere to avoid tz mixups
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        # render timestamp as ISO in UTC with Z suffix
        ts = self.timestamp
        if isinstance(ts, datetime):
            s = ts.replace(tzinfo=None).isoformat() + "Z"
        else:
            s = str(ts)
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": s,
        }

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    return jsonify({"status": "ok", "msg": "Rescue backend online."}), 200

# Create a reading
@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}

    distance_cm = data.get("distance_cm")
    if distance_cm is None:
        return jsonify({"error": "distance_cm required"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])

    # optional: prevent duplicate noise entries by comparing with last reading
    try:
        now = datetime.utcnow()
        last = (
            VictimReading.query
            .filter_by(victim_id=victim_id)
            .order_by(VictimReading.timestamp.desc())
            .first()
        )
        save = True
        if last:
            # compute seconds difference safely (both naive UTC)
            last_ts = last.timestamp
            # last_ts should be naive UTC because we stored datetime.utcnow()
            if isinstance(last_ts, datetime):
                diff_seconds = (now - last_ts).total_seconds()
                # If reading is identical and within short time window, skip saving.
                if diff_seconds < 5 and abs(float(distance_cm) - float(last.distance_cm)) < 0.01:
                    save = False
    except Exception:
        # In case of any DB error, fall back to saving the reading to avoid data loss.
        logger.exception("Error while checking last reading; will save current reading.")

    if not save:
        return jsonify({"status": "skipped", "reason": "duplicate/too-fast"}), 200

    reading = VictimReading(
        victim_id=victim_id,
        distance_cm=float(distance_cm),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        timestamp=datetime.utcnow(),
    )

    try:
        db.session.add(reading)
        db.session.commit()
    except Exception as e:
        logger.exception("DB commit failed")
        db.session.rollback()
        return jsonify({"error": "db_error", "detail": str(e)}), 500

    return jsonify({"status": "ok", "reading": reading.to_dict()}), 201

# Get all readings
@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.desc())
        .all()
    )
    return jsonify([r.to_dict() for r in readings]), 200

# Latest reading for victim
@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = (
        VictimReading.query
        .filter_by(victim_id=victim_id)
        .order_by(VictimReading.timestamp.desc())
        .first()
    )

    if not reading:
        return jsonify({"error": "No readings for this victim"}), 404

    return jsonify(reading.to_dict()), 200

# Export PDF
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
            (r.victim_id or "")[:12],
            f"{(r.distance_cm or 0):.2f}",
            f"{(r.latitude or 0):.4f}",
            f"{(r.longitude or 0):.4f}",
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
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

# Admin: trigger DB init manually (protected)
@app.route("/admin/init-db", methods=["POST"])
def admin_init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with app.app_context():
            db.create_all()
        return jsonify({"status": "ok", "msg": "db.create_all() executed"}), 200
    except Exception as e:
        logger.exception("admin init failed")
        return jsonify({"error": "db_init_failed", "detail": str(e)}), 500

# -----------------------------------------------------
# BACKGROUND DB INIT (non-blocking at import)
# -----------------------------------------------------
def _background_db_init(delay_seconds=1, retries=5, backoff=2):
    """
    Try creating tables in background with retries. This avoids blocking worker import.
    """
    def _worker():
        time.sleep(delay_seconds)
        attempt = 0
        while attempt < retries:
            attempt += 1
            try:
                logger.info("background_db_init: attempt %d", attempt)
                with app.app_context():
                    db.create_all()
                logger.info("background_db_init: success")
                return
            except Exception as e:
                logger.exception("background_db_init: failed (attempt %d): %s", attempt, e)
                time.sleep(backoff * attempt)
        logger.error("background_db_init: all attempts failed")
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

# ONLY start background init when running under real process (avoid when imported by test runners)
if __name__ != "__main__":
    # when Gunicorn imports the module, kick off background DB init
    try:
        _background_db_init(delay_seconds=1, retries=4, backoff=2)
        logger.info("background DB init scheduled")
    except Exception:
        logger.exception("Failed to schedule background DB init")

# -----------------------------------------------------
# START SERVER (for local dev)
# -----------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        # local dev - create tables synchronously so local testing is quick
        db.create_all()
    app.run(host="0.0.0.0", port=5001, debug=True)

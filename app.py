import os
import uuid
import logging
from io import BytesIO
from datetime import datetime

from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# ---- app + logger
app = Flask(__name__)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ---- env / db config
DATABASE_URL = os.environ.get("DATABASE_URL")
logger.info("STARTUP: DATABASE_URL = %s", DATABASE_URL)

if not DATABASE_URL:
    logger.warning("DATABASE_URL NOT FOUND. Using local SQLite fallback.")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------------------------------
# DATABASE MODEL (define model BEFORE create_all)
# -----------------------------------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True)
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp.isoformat() + "Z",
        }

# -----------------------------------------------------
# Ensure tables exist on import (runs when Gunicorn imports app)
# -----------------------------------------------------
try:
    with app.app_context():
        db.create_all()
        logger.info("db.create_all() executed at import time")
except Exception as e:
    logger.exception("Error running db.create_all() at import time: %s", e)

# -----------------------------------------------------
# API KEY FOR SECURITY
# -----------------------------------------------------
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "secret")

def require_key(req):
    key = req.headers.get("x-api-key")
    return key == WRITE_API_KEY

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    return jsonify({"status": "ok", "msg": "Rescue backend online."}), 200


@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    distance_cm = data.get("distance_cm")
    if distance_cm is None:
        return jsonify({"error": "distance_cm required"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])

    reading = VictimReading(
        victim_id=victim_id,
        distance_cm=float(distance_cm),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
    )

    try:
        db.session.add(reading)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("DB insert failed: %s", e)
        return jsonify({"error": "DB insert failed", "message": str(e)}), 500

    return jsonify({"status": "ok", "reading": reading.to_dict()}), 201


@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    readings = VictimReading.query.order_by(VictimReading.timestamp.desc()).all()
    return jsonify([r.to_dict() for r in readings]), 200


@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = VictimReading.query.filter_by(victim_id=victim_id).order_by(VictimReading.timestamp.desc()).first()
    if not reading:
        return jsonify({"error": "No readings for this victim"}), 404
    return jsonify(reading.to_dict()), 200


@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    readings = VictimReading.query.order_by(VictimReading.timestamp.asc()).all()

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, height - 50, "Rescue Radar - Victim Readings Export")

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
            r.victim_id[:12],
            f"{r.distance_cm:.2f}",
            f"{r.latitude or 0:.4f}",
            f"{r.longitude or 0:.4f}",
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        ]

        for i, val in enumerate(values):
            p.drawString(x_positions[i], y, val)

        y -= 16

    p.showPage()
    p.save()
    buffer.seek(0)

    return send_file(buffer, as_attachment=True, download_name="victim_readings.pdf", mimetype="application/pdf")


# -----------------------------------------------------
# ADMIN: Manual DB init endpoint (protected by API key)
# -----------------------------------------------------
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
        return jsonify({"error": "init failed", "message": str(e)}), 500


# -----------------------------------------------------
# START SERVER (only used when running python app.py locally)
# -----------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

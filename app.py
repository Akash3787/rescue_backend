# app.py (with simple API key protection)
import os
import uuid
from io import BytesIO
from datetime import datetime

from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# -------------------------
# CONFIG
# -------------------------
app = Flask(__name__)
CORS(app)

# Database URL from env (Railway will provide)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://flask_user:strongpassword@localhost/flask_app"
)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# API key for writes (set in Railway env vars). If empty -> writes allowed (dev only)
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "").strip()

db = SQLAlchemy(app)


# -------------------------
# MODEL
# -------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True)
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": self.timestamp.isoformat() + "Z",
        }


# -------------------------
# HELPERS
# -------------------------
def _check_api_key():
    """Return True if request has correct API key or no key is required (dev)."""
    if not WRITE_API_KEY:
        return True  # dev mode: no key set
    # First check header
    header_key = request.headers.get("x-api-key")
    if header_key and header_key == WRITE_API_KEY:
        return True
    # fallback: query param
    q = request.args.get("api_key")
    if q and q == WRITE_API_KEY:
        return True
    return False


# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def home():
    return jsonify({"status": "ok", "msg": "Rescue backend online."})


# --- temporary debug wrapper for /api/v1/readings ---
import traceback
import logging

logger = logging.getLogger(__name__)

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    try:
        # AUTH
        if not _check_api_key():
            return jsonify({"error": "Unauthorized - invalid or missing API key"}), 401

        data = request.get_json() or {}
        distance_cm = data.get("distance_cm")
        if distance_cm is None:
            return jsonify({"error": "distance_cm is required"}), 400

        victim_id = data.get("victim_id")
        if victim_id is None:
            victim_id = "vic-" + uuid.uuid4().hex[:8]

        reading = VictimReading(
            victim_id=victim_id,
            distance_cm=float(distance_cm),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
        )
        db.session.add(reading)
        db.session.commit()

        return jsonify({"status": "ok", "reading": reading.to_dict()}), 201

    except Exception as e:
        # Log full traceback to server logs (Railway)
        tb = traceback.format_exc()
        logger.error("create_reading error: %s\n%s", str(e), tb)

        # Return error + traceback in JSON so you can see it via curl
        return jsonify({
            "error": "Internal server error (debug). See 'trace' for details.",
            "message": str(e),
            "trace": tb.splitlines()[-30:]  # last ~30 lines
        }), 500


@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    readings = VictimReading.query.order_by(VictimReading.timestamp.desc()).all()
    return jsonify([r.to_dict() for r in readings]), 200


@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = (
        VictimReading.query
        .filter_by(victim_id=victim_id)
        .order_by(VictimReading.timestamp.desc())
        .first()
    )
    if reading is None:
        return jsonify({"error": "No readings found for this victim_id"}), 404
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
    p.drawString(50, y, "ID")
    p.drawString(90, y, "Victim ID")
    p.drawString(220, y, "Distance (cm)")
    p.drawString(320, y, "Lat")
    p.drawString(380, y, "Lon")
    p.drawString(450, y, "Timestamp")

    y -= 20
    p.setFont("Helvetica", 9)

    for r in readings:
        if y < 60:
            p.showPage()
            y = height - 60
            p.setFont("Helvetica", 9)

        p.drawString(50, y, str(r.id))
        p.drawString(90, y, (r.victim_id or "")[:12])
        p.drawString(220, y, f"{r.distance_cm:.2f}")
        p.drawString(320, y, f"{r.latitude or 0:.4f}")
        p.drawString(380, y, f"{r.longitude or 0:.4f}")
        p.drawString(450, y, r.timestamp.strftime("%Y-%m-%d %H:%M:%S"))
        y -= 16

    p.showPage()
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="victim_readings.pdf", mimetype="application/pdf")


# -------------------------
# START (local dev)
# -------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))

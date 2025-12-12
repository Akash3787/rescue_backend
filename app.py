#!/usr/bin/env python3
"""
Rescue Radar - app.py (final)
- Accepts readings with: detected, range_cm, angle_deg (also supports legacy keys)
- Logs incoming request headers + raw body for debugging
- Uses absolute sqlite path in instance/, ensures folder exists
- Optional Socket.IO emit if flask_socketio installed
- Optional PDF export if reportlab installed
- Admin init-db endpoint
Run: python3 app.py
"""

# Compatibility: ensure pkgutil.get_loader exists (some Python builds remove it)
import pkgutil
import importlib.util

if not hasattr(pkgutil, "get_loader"):
    def _compat_get_loader(name):
        try:
            spec = importlib.util.find_spec(name)
            return spec.loader if spec is not None else None
        except Exception:
            return None
    pkgutil.get_loader = _compat_get_loader

import os
import uuid
import logging
from datetime import datetime
from io import BytesIO
from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# Try to import reportlab for PDF export (optional)
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    logger.info("⚠ flask_socketio NOT installed — realtime disabled")

# ---------------- Ensure instance dir & DB path ----------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)  # ensure dir exists

# optional debug: report absolute path
sqlite_path = os.path.join(INSTANCE_DIR, "rescue_radar.db")
logger.info("ABSOLUTE SQLITE PATH -> %s", sqlite_path)

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    logger.info("Using DATABASE_URL from environment")
else:
    # Use absolute sqlite path
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

    # Primary fields requested
    detected = db.Column(db.Boolean, nullable=False, default=False)
    range_cm = db.Column(db.Float, nullable=True)
    angle_deg = db.Column(db.Float, nullable=True)

    # Backwards-compatible legacy fields (optional, not required)
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
        return "<h2>Rescue Radar API</h2><p>No readings yet.</p>"
    status = "DETECTED" if latest.detected else "NO PERSON"
    return (
        f"<h2>Rescue Radar API</h2>"
        f"<p>Status: {status}</p>"
        f"<p>Range: {latest.range_cm if latest.range_cm is not None else (latest.distance_cm if latest.distance_cm is not None else 'N/A')} cm</p>"
        f"<p>Angle: {latest.angle_deg if latest.angle_deg is not None else 'N/A'}°</p>"
        f"<p>Victim: {latest.victim_id} • {latest.timestamp} UTC</p>"
    )

@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    # Debug: log headers + raw body
    try:
        logger.info("---- Incoming request headers ----")
        for k, v in request.headers.items():
            logger.info("%s: %s", k, v)
        raw_body = request.get_data(as_text=True)
        logger.info("---- Incoming raw body ----\n%s", raw_body)
    except Exception:
        logger.exception("Failed to log incoming request")

    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    # Accept both new and legacy keys:
    # new: detected, range_cm, angle_deg
    # legacy: distance_cm (we'll map to range_cm), distance/distance_cm, others ignored
    detected = parse_bool(data.get("detected", data.get("person_detected", data.get("found"))))

    raw_range = data.get("range_cm", data.get("range", data.get("distance_cm", data.get("distance"))))
    range_cm = to_float(raw_range)

    raw_angle = data.get("angle_deg", data.get("angle"))
    angle_deg = to_float(raw_angle)

    # legacy optional fields
    distance_cm = to_float(data.get("distance_cm", data.get("distance")))
    temperature = to_float(data.get("temperature"))
    humidity = to_float(data.get("humidity"))
    gas = to_float(data.get("gas"))
    latitude = to_float(data.get("latitude"))
    longitude = to_float(data.get("longitude"))

    victim_id = data.get("victim_id") or f"vic-{uuid.uuid4().hex[:8]}"

    # Upsert by victim_id
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()
    if reading:
        reading.detected = detected
        reading.range_cm = range_cm if range_cm is not None else reading.range_cm
        reading.angle_deg = angle_deg if angle_deg is not None else reading.angle_deg
        # preserve legacy fields where provided
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

    # Emit realtime event (if available)
    if SOCKETIO_AVAILABLE:
        try:
            socketio.emit("reading_update", {"reading": reading.to_dict()})
        except Exception:
            logger.exception("socket emit failed")

    logger.info("%s victim %s detected=%s range=%s angle=%s",
                action, victim_id, reading.detected, reading.range_cm, reading.angle_deg)
    return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200

@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 500)
    q = VictimReading.query.order_by(VictimReading.timestamp.desc())
    # simple paginate manually if needed
    items = q.limit(per_page).offset((page - 1) * per_page).all()
    total = q.count()
    return jsonify({
        "readings": [r.to_dict() for r in items],
        "page": page,
        "per_page": per_page,
        "total": total,
    }), 200

@app.route("/api/v1/readings/latest", methods=["GET"])
def latest_reading():
    latest = VictimReading.query.order_by(VictimReading.timestamp.desc()).first()
    if not latest:
        return jsonify({"reading": None}), 200
    return jsonify({"reading": latest.to_dict()}), 200

@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_pdf():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not REPORTLAB_AVAILABLE:
        return jsonify({"error": "PDF export not available - reportlab not installed"}), 503
    try:
        readings = VictimReading.query.order_by(VictimReading.timestamp.desc()).limit(500).all()
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=16, spaceAfter=12)

        elements.append(Paragraph("Rescue Radar - Victim Readings", title_style))
        elements.append(Spacer(1, 0.1 * inch))

        table_data = [["ID", "Victim ID", "Detected", "Range (cm)", "Angle (°)", "Timestamp"]]
        for r in readings:
            detected_str = "YES" if r.detected else "NO"
            range_str = f"{r.range_cm:.1f}" if r.range_cm is not None else (f"{r.distance_cm:.1f}" if r.distance_cm is not None else "N/A")
            angle_str = f"{r.angle_deg:.1f}" if r.angle_deg is not None else "N/A"
            ts_str = r.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if isinstance(r.timestamp, datetime) else str(r.timestamp)
            table_data.append([str(r.id), r.victim_id, detected_str, range_str, angle_str, ts_str])

        table = Table(table_data, repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#2E86AB")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ]))
        elements.append(table)
        doc.build(elements)
        buffer.seek(0)
        return Response(buffer.getvalue(), mimetype="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=rescue_radar_report.pdf"})
    except Exception:
        logger.exception("PDF export failed")
        return jsonify({"error": "PDF generation failed"}), 500

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

# ---------------- Run ----------------
if __name__ == "__main__":
    # ensure instance dir exists (already created above but be safe)
    os.makedirs(INSTANCE_DIR, exist_ok=True)

    with app.app_context():
        try:
            db.create_all()
        except Exception:
            logger.exception("db.create_all failed on startup")

    port = int(os.environ.get("PORT", 5001))

    if SOCKETIO_AVAILABLE:
        logger.info("Running with Socket.IO on port %d", port)
        socketio.run(app, host="0.0.0.0", port=port)
    else:
        logger.info("Running Flask without Socket.IO on port %d", port)
        app.run(host="0.0.0.0", port=port)

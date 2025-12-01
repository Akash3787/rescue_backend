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
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL.replace("postgres://", "postgresql://")

# Safer engine options to avoid long blocking connections (esp. for MySQL/PostgreSQL)
engine_opts = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_timeout": 20,
    "max_overflow": 10
}

# Add connect timeout for MySQL/PostgreSQL
if DATABASE_URL:
    if "mysql" in DATABASE_URL.lower():
        engine_opts["connect_args"] = {"connect_timeout": 5}
    elif "postgres" in DATABASE_URL.lower():
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
    human_confidence = db.Column(db.Float)  # NEW: From YOLO
    animal_confidence = db.Column(db.Float)  # NEW: From YOLO
    human_count = db.Column(db.Integer, default=0)
    animal_count = db.Column(db.Integer, default=0)
    motion_detected = db.Column(db.Boolean, default=False)
    detected_class = db.Column(db.String(20))  # person/animal/none
    rescue_priority = db.Column(db.String(10))  # HIGH/MEDIUM/LOW
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
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
            "human_confidence": self.human_confidence,
            "animal_confidence": self.animal_confidence,
            "human_count": self.human_count,
            "animal_count": self.animal_count,
            "motion_detected": self.motion_detected,
            "detected_class": self.detected_class,
            "rescue_priority": self.rescue_priority,
            "timestamp": s,
        }

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "msg": "🚨 Rescue Radar Backend v2.0 - LIVE!",
        "endpoints": ["/api/v1/readings", "/api/v1/readings/all", "/api/v1/readings/export/pdf"]
    }), 200

# Create a reading (YOLO + Radar integration)
@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    distance_cm = data.get("distance_cm")
    if distance_cm is None:
        return jsonify({"error": "distance_cm required"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])

    # Anti-duplicate filter (5s debounce)
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
            last_ts = last.timestamp
            if isinstance(last_ts, datetime):
                diff_seconds = (now - last_ts).total_seconds()
                if (diff_seconds < 5 and
                    abs(float(distance_cm) - float(last.distance_cm)) < 0.01 and
                    data.get("human_confidence", 0) == last.human_confidence):
                    save = False
    except Exception:
        logger.exception("Duplicate check failed; saving anyway")

    if not save:
        return jsonify({"status": "skipped", "reason": "duplicate"}), 200

    # Save YOLO + Radar data
    reading = VictimReading(
        victim_id=victim_id,
        distance_cm=float(distance_cm),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        human_confidence=data.get("human_confidence"),
        animal_confidence=data.get("animal_confidence"),
        human_count=data.get("human_count", 0),
        animal_count=data.get("animal_count", 0),
        motion_detected=data.get("motion_detected", False),
        detected_class=data.get("detected_class"),
        rescue_priority=data.get("rescue_priority", "LOW"),
        timestamp=datetime.utcnow(),
    )

    try:
        db.session.add(reading)
        db.session.commit()
        logger.info(f"SAVED: {victim_id} | {distance_cm}cm | {reading.rescue_priority}")
    except Exception as e:
        logger.exception("DB commit failed")
        db.session.rollback()
        return jsonify({"error": "db_error", "detail": str(e)}), 500

    return jsonify({"status": "saved", "reading": reading.to_dict()}), 201

# Get all readings
@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.desc())
        .limit(100)  # Prevent huge responses
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

# HIGH PRIORITY alerts only
@app.route("/api/v1/alerts/high", methods=["GET"])
def high_priority_alerts():
    alerts = (
        VictimReading.query
        .filter_by(rescue_priority="HIGH")
        .order_by(VictimReading.timestamp.desc())
        .limit(20)
        .all()
    )
    return jsonify([r.to_dict() for r in alerts]), 200

# Export PDF Report
@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.desc())
        .limit(50)
        .all()
    )

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Header
    p.setFont("Helvetica-Bold", 18)
    p.drawString(50, height - 60, "🚨 RESCUE RADAR - MISSION REPORT")
    p.setFont("Helvetica", 10)
    p.drawString(50, height - 85, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    # Table
    y = height - 110
    p.setFont("Helvetica-Bold", 9)
    headers = ["ID", "Victim", "Dist(cm)", "Human%", "Priority", "Time"]
    x_pos = [40, 90, 160, 230, 300, 370]
    
    for i, h in enumerate(headers):
        p.drawString(x_pos[i], y, h)

    y -= 18
    p.setFont("Helvetica", 8)

    high_priority_count = 0
    for r in readings:
        if y < 80:
            p.showPage()
            y = height - 60
            p.setFont("Helvetica", 8)

        prio_color = "red" if r.rescue_priority == "HIGH" else "black"
        p.setFillColor(prio_color)
        
        values = [
            str(r.id)[:4],
            (r.victim_id or "")[:8],
            f"{r.distance_cm:.0f}",
            f"{r.human_confidence*100:.0f}%" if r.human_confidence else "--",
            r.rescue_priority or "LOW",
            r.timestamp.strftime("%H:%M")
        ]
        
        if r.rescue_priority == "HIGH":
            high_priority_count += 1

        for i, val in enumerate(values):
            p.drawString(x_pos[i], y, val)

        p.setFillColor("black")
        y -= 14

    # Summary
    p.showPage()
    p.setFont("Helvetica-Bold", 12)
    p.drawString(50, height - 100, f"HIGH PRIORITY ALERTS: {high_priority_count}")
    
    p.showPage()
    p.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"rescue_report_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf",
        mimetype="application/pdf",
    )

# Admin endpoints
@app.route("/admin/init-db", methods=["POST"])
def admin_init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        with app.app_context():
            db.create_all()
        return jsonify({"status": "ok", "msg": "Tables created"}), 200
    except Exception as e:
        logger.exception("DB init failed")
        return jsonify({"error": str(e)}), 500

@app.route("/admin/status", methods=["GET"])
def admin_status():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    total = VictimReading.query.count()
    high_priority = VictimReading.query.filter_by(rescue_priority="HIGH").count()
    
    return jsonify({
        "status": "online",
        "total_readings": total,
        "high_priority_alerts": high_priority,
        "database": app.config["SQLALCHEMY_DATABASE_URI"].split("://")[-1][:50] + "..."
    })

# -----------------------------------------------------
# BACKGROUND DB INIT (non-blocking)
# -----------------------------------------------------
def _background_db_init(delay_seconds=1, retries=5, backoff=2):
    def _worker():
        time.sleep(delay_seconds)
        attempt = 0
        while attempt < retries:
            attempt += 1
            try:
                logger.info("BG DB init attempt %d/%d", attempt, retries)
                with app.app_context():
                    db.create_all()
                logger.info("✅ Background DB init SUCCESS")
                return
            except Exception as e:
                logger.warning("BG DB init failed (attempt %d): %s", attempt, e)
                time.sleep(backoff ** attempt)
        logger.error("❌ Background DB init ALL RETRIES FAILED")
    
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

# Auto-start background init for production (Gunicorn)
if __name__ != "__main__":
    try:
        _background_db_init()
        logger.info("Background DB init scheduled")
    except:
        logger.exception("Failed to schedule BG DB init")

# -----------------------------------------------------
# START SERVER
# -----------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()  # Local dev sync create
    logger.info("🚀 Rescue Radar Backend v2.0 starting on port 5001")
    app.run(host="0.0.0.0", port=5001, debug=True)

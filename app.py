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
from sqlalchemy.exc import SQLAlchemyError

# -----------------------------------------------------
# APP INIT
# -----------------------------------------------------
app = Flask(__name__)
logger = logging.getLogger("app")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)

# -----------------------------------------------------
# DATABASE CONFIG (Railway uses DATABASE_URL)
# -----------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
logger.info("STARTUP: DATABASE_URL = %s", DATABASE_URL)

if not DATABASE_URL:
    logger.warning("DATABASE_URL NOT FOUND. Using local SQLite fallback.")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db?check_same_thread=False"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

# Secure API key - NO DEFAULT
WRITE_API_KEY = os.environ.get("WRITE_API_KEY")
if not WRITE_API_KEY:
    raise ValueError("WRITE_API_KEY environment variable is required")

# Safer engine options to avoid long blocking connections
engine_opts = {
    "pool_pre_ping": True,
    "pool_recycle": 280,  # Less than MySQL wait_timeout (28800)
    "pool_timeout": 20,
    "max_overflow": 10
}

# Database-specific connect args
if DATABASE_URL:
    if "mysql" in DATABASE_URL.lower():
        engine_opts["connect_args"] = {"connect_timeout": 5}
    elif "postgres" in DATABASE_URL.lower():
        engine_opts["connect_args"] = {"connect_timeout": 10}

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CRITICAL: Initialize db BEFORE models
db = SQLAlchemy(app)

# -----------------------------------------------------
# DATABASE MODEL (MOVED AFTER db INIT)
# -----------------------------------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"

    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True, unique=True)  # UNIQUE!
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        """Convert to JSON-serializable dict with proper UTC ISO format"""
        ts = self.timestamp
        if isinstance(ts, datetime):
            # Ensure naive UTC -> ISO + Z
            iso_ts = ts.replace(tzinfo=None).isoformat() + "Z"
        else:
            iso_ts = str(ts)
        return {
            "id": self.id,
            "victim_id": self.victim_id,
            "distance_cm": self.distance_cm,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timestamp": iso_ts,
        }

# -----------------------------------------------------
# API KEY FOR SECURITY
# -----------------------------------------------------
def require_key(req):
    key = req.headers.get("x-api-key")
    return key == WRITE_API_KEY

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    return jsonify({"status": "ok", "msg": "Rescue backend online."}), 200

# ✅ FIXED: UPSERT - EXACTLY 1 ROW PER VICTIM (no duplicates!)
@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    distance_cm = data.get("distance_cm")
    
    if distance_cm is None:
        return jsonify({"error": "distance_cm required"}), 400
    
    try:
        distance_cm = float(distance_cm)
        if not (0 <= distance_cm <= 10000):  # Reasonable radar range 0-100m
            return jsonify({"error": "distance_cm must be 0-10000"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "distance_cm must be a valid number"}), 400

    victim_id = data.get("victim_id") or ("vic-" + uuid.uuid4().hex[:8])

    # ✅ UPSERT: Always 1 row per victim - UPDATE existing or INSERT new
    try:
        # Find existing victim (or None)
        reading = VictimReading.query.filter_by(victim_id=victim_id).first()
        
        if reading:
            # UPDATE existing row with NEW values (NO DUPLICATES!)
            reading.distance_cm = distance_cm
            reading.latitude = data.get("latitude")
            reading.longitude = data.get("longitude")
            reading.timestamp = datetime.utcnow()  # Fresh timestamp
            action = "UPDATED"
        else:
            # INSERT new victim
            reading = VictimReading(
                victim_id=victim_id,
                distance_cm=distance_cm,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                timestamp=datetime.utcnow(),
            )
            db.session.add(reading)
            action = "CREATED"

        db.session.commit()
        logger.info("%s victim %s: %.2fcm", action, victim_id, distance_cm)
        return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200
        
    except SQLAlchemyError as e:
        logger.exception("DB upsert failed")
        db.session.rollback()
        return jsonify({"error": "db_error", "detail": str(e)}), 500

# Get all victims (1 per victim - paginated)
@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)
    
    # Query DISTINCT victims (latest only)
    readings = (
        VictimReading.query
        .order_by(VictimReading.timestamp.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify({
        "readings": [r.to_dict() for r in readings.items],
        "page": page,
        "per_page": per_page,
        "total": readings.total,
        "pages": readings.pages
    }), 200

# Latest reading for victim (now same as single row)
@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()

    if not reading:
        return jsonify({"error": "No readings for this victim"}), 404

    return jsonify(reading.to_dict()), 200

# 🔧 CLEANUP: Remove old duplicate victims (run ONCE)
@app.route("/admin/clean-duplicates", methods=["POST"])
def clean_duplicates():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        # Delete ALL duplicates, keep only 1 latest per victim_id
        result = db.session.query(
            VictimReading.victim_id,
            db.func.max(VictimReading.timestamp).label('max_ts')
        ).group_by(VictimReading.victim_id).subquery()
        
        # Keep only the latest entry per victim
        latest_ids = db.session.query(result.c.max_ts).distinct().subquery()
        deleted = VictimReading.query.filter(
            ~VictimReading.timestamp.in_(db.session.query(latest_ids))
        ).delete(synchronize_session=False)
        
        db.session.commit()
        logger.info("Cleaned %d duplicate readings", deleted)
        return jsonify({"status": "cleaned", "deleted": deleted}), 200
    except Exception as e:
        db.session.rollback()
        logger.exception("Cleanup failed")
        return jsonify({"error": "cleanup_failed", "detail": str(e)}), 500

# FIXED: Export PDF (SECURE + ROBUST)
@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    
    # Optional date filtering
    from_date_str = request.args.get("from")
    to_date_str = request.args.get("to")
    
    query = VictimReading.query.order_by(VictimReading.timestamp.asc())
    if from_date_str:
        try:
            from_date = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            query = query.filter(VictimReading.timestamp >= from_date)
        except ValueError:
            return jsonify({"error": "Invalid from date format (YYYY-MM-DD)"}), 400
    
    readings = query.limit(5000).all()
    
    if not readings:
        return jsonify({"error": "No readings found"}), 404

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    page_num = 1

    # Title
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, height - 50, "Rescue Radar - Victim Readings Export")
    p.setFont("Helvetica", 10)
    p.drawString(50, height - 75, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    p.drawString(50, height - 95, f"Total Victims: {len(readings)}")

    # Wider table columns
    y = height - 130
    p.setFont("Helvetica-Bold", 9)
    headers = ["ID", "Victim ID", "Distance(cm)", "Latitude", "Longitude", "UTC Time"]
    x_positions = [50, 120, 220, 320, 390, 460]

    for i, h in enumerate(headers):
        p.drawString(x_positions[i], y, h)

    y -= 20
    p.setFont("Helvetica", 8)

    for r in readings:
        if y < 80:
            p.setFont("Helvetica", 7)
            p.drawString(50, 50, f"Page {page_num}")
            p.showPage()
            page_num += 1
            y = height - 60
            p.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                p.drawString(x_positions[i], y, h)
            y -= 20
            p.setFont("Helvetica", 8)

        values = [
            str(r.id),
            r.victim_id[:20],
            f"{r.distance_cm:.1f}" if r.distance_cm is not None else "N/A",
            f"{r.latitude:.4f}" if r.latitude is not None else "N/A",
            f"{r.longitude:.4f}" if r.longitude is not None else "N/A",
            r.timestamp.strftime("%Y-%m-%d %H:%M") if r.timestamp else "N/A"
        ]

        for i, val in enumerate(values):
            p.drawString(x_positions[i], y, val)

        y -= 15

    # Final page footer
    p.setFont("Helvetica", 7)
    p.drawString(50, 50, f"Final Page {page_num}")
    p.showPage()
    p.save()

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"victim_readings_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf",
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
        return jsonify({"status": "ok", "msg": "db.create_all() executed"}), 200
    except Exception as e:
        logger.exception("admin init failed")
        return jsonify({"error": "db_init_failed", "detail": str(e)}), 500

# -----------------------------------------------------
# BACKGROUND DB INIT (IMPROVED)
# -----------------------------------------------------
def _background_db_init(delay_seconds=2, retries=5, backoff=2):
    def _worker():
        time.sleep(delay_seconds)
        attempt = 0
        while attempt < retries:
            attempt += 1
            try:
                logger.info("background_db_init: attempt %d/%d", attempt, retries)
                with app.app_context():
                    db.create_all()
                logger.info("background_db_init: SUCCESS")
                return
            except Exception as e:
                logger.warning("background_db_init: failed (attempt %d): %s", attempt, e)
                time.sleep(backoff ** attempt)
        logger.error("background_db_init: ALL RETRIES FAILED")
    
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

if __name__ != "__main__":
    try:
        _background_db_init()
        logger.info("Background DB init scheduled")
    except Exception:
        logger.exception("Failed to schedule background DB init")

# -----------------------------------------------------
# START SERVER (for local dev)
# -----------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        logger.info("Local dev: Tables created")
    app.run(host="0.0.0.0", port=5001, debug=True)

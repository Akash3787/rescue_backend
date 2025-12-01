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
    victim_id = db.Column(db.String(64), nullable=False, index=True)
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

# Create a reading
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

    # Optional: prevent duplicate noise entries
    save = True
    try:
        now = datetime.utcnow()
        last = (
            VictimReading.query
            .filter_by(victim_id=victim_id)
            .order_by(VictimReading.timestamp.desc())
            .first()
        )
        if last and last.timestamp:
            diff_seconds = (now - last.timestamp).total_seconds()
            if diff_seconds < 5 and abs(distance_cm - last.distance_cm) < 0.01:
                save = False
                logger.info("Skipped duplicate reading for %s (%.2fcm)", victim_id, distance_cm)
    except SQLAlchemyError as e:
        logger.warning("Error checking duplicate: %s. Saving anyway.", e)

    if not save:
        return jsonify({"status": "skipped", "reason": "duplicate/too-fast"}), 200

    reading = VictimReading(
        victim_id=victim_id,
        distance_cm=distance_cm,
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        timestamp=datetime.utcnow(),
    )

    try:
        db.session.add(reading)
        db.session.commit()
        logger.info("Saved reading %s: %.2fcm", victim_id, distance_cm)
    except SQLAlchemyError as e:
        logger.exception("DB commit failed")
        db.session.rollback()
        return jsonify({"error": "db_error", "detail": str(e)}), 500

    return jsonify({"status": "ok", "reading": reading.to_dict()}), 201

# Get all readings (paginated)
@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)
    
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

# FIXED: Export PDF (SECURE + ROBUST)
@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    if not require_key(request):  # CRITICAL SECURITY FIX
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
    
    readings = query.limit(5000).all()  # Prevent OOM
    
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
    p.drawString(50, height - 95, f"Total Readings: {len(readings)} | Unique Victims: {len(set(r.victim_id for r in readings))}")

    # FIXED: Wider table columns
    y = height - 130
    p.setFont("Helvetica-Bold", 9)
    headers = ["ID", "Victim ID", "Distance(cm)", "Latitude", "Longitude", "UTC Time"]
    x_positions = [50, 120, 220, 320, 390, 460]  # Wider spacing

    for i, h in enumerate(headers):
        p.drawString(x_positions[i], y, h)

    y -= 20
    p.setFont("Helvetica", 8)

    victim_count = {}
    for r in readings:
        if y < 80:
            # Page footer
            p.setFont("Helvetica", 7)
            p.drawString(50, 50, f"Page {page_num} | Victims: {len(victim_count)}")
            p.showPage()
            page_num += 1
            y = height - 60
            # Re-draw headers
            p.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                p.drawString(x_positions[i], y, h)
            y -= 20
            p.setFont("Helvetica", 8)

        # FIXED: Proper null handling + full victim_id
        values = [
            str(r.id),
            r.victim_id[:20],  # Truncate only if too long for page
            f"{r.distance_cm:.1f}" if r.distance_cm is not None else "N/A",
            f"{r.latitude:.4f}" if r.latitude is not None else "N/A",
            f"{r.longitude:.4f}" if r.longitude is not None else "N/A",
            # FIXED: Proper UTC formatting
            r.timestamp.strftime("%Y-%m-%d %H:%M") if r.timestamp else "N/A"
        ]

        victim_count[r.victim_id] = victim_count.get(r.victim_id, 0) + 1
        
        for i, val in enumerate(values):
            p.drawString(x_positions[i], y, val)

        y -= 15

    # Final page footer
    p.setFont("Helvetica", 7)
    p.drawString(50, 50, f"Final Page {page_num} | Total Victims: {len(victim_count)}")
    p.showPage()
    p.save()

    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"victim_readings_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.pdf",
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
# BACKGROUND DB INIT (IMPROVED - with proper context)
# -----------------------------------------------------
def _background_db_init(delay_seconds=2, retries=5, backoff=2):
    """Thread-safe background DB init with proper app context"""
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

# Start background init for production (Gunicorn/Railway)
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

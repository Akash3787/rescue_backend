from flask import Flask, request, jsonify, send_file, Response  # Added Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS  # NEW: Flutter cross-origin access
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
import serial
import serial.tools.list_ports
import cv2  # NEW: USB Endoscope camera

# -----------------------------------------------------
# APP INIT
# -----------------------------------------------------
app = Flask(__name__)
CORS(app)  # NEW: Enable Flutter dashboard access
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

# NEW: ESP32 Serial Configuration
ESP_PORT = os.environ.get("ESP_PORT", "/dev/cu.usbserial-*")  # MacOS-friendly default
BAUD_RATE = 115200
esp_serial = None  # Global serial connection

# NEW: USB Endoscope Camera (Global)
cap = None

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
# NEW: USB ENDOSCOPE FUNCTIONS
# -----------------------------------------------------
def init_camera():
    """Initialize USB endoscope camera (device 1: HD camera)"""
    global cap
    try:
        cap = cv2.VideoCapture(1)  # Your detected HD camera
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Zero latency
        logger.info("✅ Endoscope camera initialized (640x480@15fps uyvy422)")
        return True
    except Exception as e:
        logger.error(f"❌ Camera init failed: {e}")
        return False

def gen_frames():
    """MJPEG generator for Flutter dashboard"""
    global cap
    while cap and cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        frame_bytes = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n'
        yield frame_bytes

# -----------------------------------------------------
# ESP32 SERIAL FUNCTIONS (UNCHANGED)
# -----------------------------------------------------
def connect_esp():
    """Connect to ESP32 serial port"""
    global esp_serial
    if esp_serial and esp_serial.is_open:
        return True
    
    try:
        logger.info(f"🔌 Connecting to ESP32 on {ESP_PORT}")
        esp_serial = serial.Serial(ESP_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)  # ESP boot time
        logger.info("✅ ESP32 connected on %s", ESP_PORT)
        return True
    except Exception as e:
        logger.error("❌ ESP32 connection failed: %s on port %s", e, ESP_PORT)
        return False

def send_esp_command(cmd):
    """Send command to ESP32"""
    if not connect_esp():
        logger.error("❌ ESP not connected - command '%s' failed", cmd)
        return False
    
    try:
        esp_serial.write(f"{cmd}\n".encode())
        esp_serial.flush()
        logger.info("📤 ESP CMD: %s", cmd)
        time.sleep(0.1)  # Small delay for ESP processing
        return True
    except Exception as e:
        logger.error("❌ ESP send failed: %s", e)
        return False

def find_esp_ports():
    """Find available serial ports (DEBUG)"""
    ports = serial.tools.list_ports.comports()
    esp_ports = []
    for port in ports:
        if "esp" in port.description.lower() or "ch340" in port.description.lower() or "cp210" in port.description.lower():
            esp_ports.append(port.device)
    return esp_ports

# -----------------------------------------------------
# DATABASE MODEL (UNCHANGED)
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
# API KEY FOR SECURITY (UNCHANGED)
# -----------------------------------------------------
def require_key(req):
    key = req.headers.get("x-api-key")
    return key == WRITE_API_KEY

# -----------------------------------------------------
# ROUTES - YOUR ORIGINAL + NEW CAMERA ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    esp_status = "✅ Connected" if connect_esp() else "❌ Disconnected"
    camera_status = "✅ Live" if cap and cap.isOpened() else "❌ Offline"
    if not cap:
        init_camera()  # Auto-init camera
    ports = find_esp_ports()
    return jsonify({
        "status": "ok",
        "msg": "Rescue Radar Backend - LIVE CAMERA + RADAR",
        "esp_status": esp_status,
        "camera_status": camera_status,
        "available_ports": ports,
        "esp_port": ESP_PORT,
        "camera_device": 1  # HD camera index
    }), 200

# NEW: LIVE CAMERA STREAM (MJPEG for Flutter dashboard)
@app.route('/stream')
def video_feed():
    """Live MJPEG stream - matches Flutter _mjpegUrl"""
    global cap
    if not cap or not cap.isOpened():
        if not init_camera():
            return "Camera unavailable", 500
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# NEW: SINGLE SNAPSHOT (Flutter fallback)
@app.route('/snap')
def snap():
    """Single frame snapshot - matches Flutter _snapUrl"""
    global cap
    if not cap or not cap.isOpened():
        if not init_camera():
            return "Camera unavailable", 500
    ret, frame = cap.read()
    if ret:
        ret, buffer = cv2.imencode('.jpg', frame)
        return Response(buffer.tobytes(), mimetype='image/jpeg')
    return "Camera read failed", 500

# NEW: CAMERA STATUS API
@app.route('/api/camera/status')
def camera_status():
    """Camera health check for Flutter"""
    global cap
    return jsonify({
        "live": cap.isOpened() if cap else False,
        "device": 1,
        "resolution": "640x480@15fps",
        "format": "uyvy422"
    })

# ✅ FIXED: UPSERT - EXACTLY 1 ROW PER VICTIM (YOUR ORIGINAL)
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

# NEW: SOS BUTTON - Triggers BUZZER on ESP32 (YOUR CODE)
@app.route("/send-sos", methods=["POST"])
def send_sos():
    logger.info("🚨 SOS BUTTON PRESSED FROM DASHBOARD!")
    success = send_esp_command("SOS_ON")  # Send SOS command to ESP32
    if success:
        logger.info("✅ SOS command sent to ESP32 buzzer")
    return jsonify({
        "status": "sos_triggered",
        "success": success,
        "message": "Buzzer SOS pattern activated on ESP32" if success else "ESP connection failed"
    }), 200

# NEW: LIGHT TOGGLE - Controls LED on ESP32 (YOUR CODE)
@app.route("/toggle-light", methods=["POST"])
def toggle_light():
    data = request.get_json() or {}
    status = data.get("status", "OFF")
    
    cmd = "LIGHT_ON" if status.upper() == "ON" else "LIGHT_OFF"
    success = send_esp_command(cmd)
    
    logger.info("💡 LIGHT TOGGLE: %s -> ESP32 CMD: %s", status, cmd)
    return jsonify({
        "status": "light_toggled",
        "light_status": status,
        "esp_command": cmd,
        "success": success
    }), 200

# Get all victims (1 per victim - paginated) (YOUR CODE)
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

# Latest reading for victim (now same as single row) (YOUR CODE)
@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()

    if not reading:
        return jsonify({"error": "No readings for this victim"}), 404

    return jsonify(reading.to_dict()), 200

# 🔧 CLEANUP: Remove old duplicate victims (YOUR CODE)
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

# FIXED: Export PDF (YOUR CODE)
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

# NEW: ESP32 Status/Debug endpoint (YOUR CODE)
@app.route("/api/esp/status", methods=["GET"])
def esp_status():
    connected = connect_esp()
    ports = find_esp_ports()
    return jsonify({
        "connected": connected,
        "port": ESP_PORT,
        "available_ports": ports,
        "serial_open": esp_serial.is_open if esp_serial else False
    }), 200

# Admin endpoints (YOUR CODE)
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
# BACKGROUND DB INIT (IMPROVED) (YOUR CODE)
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
# START SERVER (MODIFIED: Auto-init camera)
# -----------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        init_camera()  # NEW: Auto-initialize USB endoscope
        logger.info("Local dev: Tables created + Camera initialized")
        logger.info(f"ESP32 port set to: {ESP_PORT}")
        logger.info("Available ESP ports: %s", find_esp_ports())
    logger.info("🚀 RESCUE RADAR Server + LIVE CAMERA starting on http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)

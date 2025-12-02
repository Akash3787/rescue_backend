from flask import Flask, request, jsonify, send_file, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
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
# CLOUD-SAFE CONDITIONAL IMPORTS (RAILWAY FIX)
# -----------------------------------------------------
SERIAL_AVAILABLE = False
OPENCV_AVAILABLE = False
esp_serial = None
cap = None

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
    logging.info("✅ pyserial loaded (ESP32 ready)")
except ImportError:
    logging.warning("⚠️  pyserial not available - ESP32 disabled (Railway/cloud normal)")

try:
    import cv2
    OPENCV_AVAILABLE = True
    logging.info("✅ OpenCV loaded (USB camera ready)")
except ImportError:
    logging.warning("⚠️  opencv-python not available - USB camera disabled (Railway normal)")

# -----------------------------------------------------
# APP INIT
# -----------------------------------------------------
app = Flask(__name__)
CORS(app)
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# -----------------------------------------------------
# DATABASE CONFIG
# -----------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
logger.info("STARTUP: DATABASE_URL = %s", "SET" if DATABASE_URL else "LOCAL")

if not DATABASE_URL:
    logger.info("Using local SQLite")
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db?check_same_thread=False"
else:
    logger.info("Using cloud database")
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

WRITE_API_KEY = os.environ.get("WRITE_API_KEY")
if not WRITE_API_KEY:
    WRITE_API_KEY = "rescue-radar-secret"  # Dev fallback
    logger.warning("Using dev API key - set WRITE_API_KEY env var!")

ESP_PORT = os.environ.get("ESP_PORT", "/dev/cu.usbserial-*")
BAUD_RATE = 115200

engine_opts = {"pool_pre_ping": True, "pool_recycle": 280, "pool_timeout": 20, "max_overflow": 10}
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_opts
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# -----------------------------------------------------
# CLOUD-SAFE CAMERA FUNCTIONS (FIXED I/O ERROR)
# -----------------------------------------------------
def init_camera():
    """Fixed USB endoscope init - handles uyvy422 + fallbacks"""
    global cap
    if not OPENCV_AVAILABLE:
        logger.info("⏭️  Camera skipped (no OpenCV)")
        return False
        
    try:
        # Try device 1 (your HD camera)
        cap = cv2.VideoCapture(1)
        
        # EXACT SETTINGS from your ffmpeg output
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        # TEST FRAME - critical for I/O error fix
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            logger.info("✅ Endoscope LIVE (640x480@15fps)")
            return True
            
        logger.info("640x480 failed - trying fallback...")
        cap.release()
        
        # FALLBACK 1: Lower resolution
        cap = cv2.VideoCapture(1)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        cap.set(cv2.CAP_PROP_FPS, 10)
        ret, frame = cap.read()
        if ret and frame is not None:
            logger.info("✅ Endoscope FALLBACK (320x240@10fps)")
            return True
            
        logger.warning("No camera detected - normal for Railway")
        return False
        
    except Exception as e:
        logger.error(f"Camera error: {e}")
        return False

def gen_frames():
    """MJPEG generator with error handling"""
    global cap
    if not cap or not cap.isOpened():
        return
        
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.1)
            continue
            
        try:
            # Fix color format if needed
            if len(frame.shape) == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                frame_bytes = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n'
                yield frame_bytes
        except:
            time.sleep(0.1)

# -----------------------------------------------------
# CLOUD-SAFE ESP32 FUNCTIONS
# -----------------------------------------------------
def connect_esp():
    if not SERIAL_AVAILABLE:
        return False
    global esp_serial
    if esp_serial and esp_serial.is_open:
        return True
    try:
        esp_serial = serial.Serial(ESP_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        logger.info(f"✅ ESP32: {ESP_PORT}")
        return True
    except Exception as e:
        logger.error(f"ESP32 failed: {e}")
        return False

def send_esp_command(cmd):
    if not SERIAL_AVAILABLE or not connect_esp():
        return False
    try:
        esp_serial.write(f"{cmd}\n".encode())
        esp_serial.flush()
        logger.info(f"📤 ESP: {cmd}")
        return True
    except:
        return False

def find_esp_ports():
    if not SERIAL_AVAILABLE:
        return []
    try:
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports if any(x in p.description.lower() for x in ['esp', 'ch340', 'cp210'])]
    except:
        return []

# -----------------------------------------------------
# DATABASE MODEL (UNCHANGED)
# -----------------------------------------------------
class VictimReading(db.Model):
    __tablename__ = "victim_readings"
    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True, unique=True)
    distance_cm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        ts = self.timestamp
        iso_ts = ts.replace(tzinfo=None).isoformat() + "Z" if isinstance(ts, datetime) else str(ts)
        return {"id": self.id, "victim_id": self.victim_id, "distance_cm": self.distance_cm,
                "latitude": self.latitude, "longitude": self.longitude, "timestamp": iso_ts}

def require_key(req):
    return req.headers.get("x-api-key") == WRITE_API_KEY

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    esp_status = "✅ Connected" if SERIAL_AVAILABLE and connect_esp() else "⏭️ Disabled"
    camera_status = "✅ Live" if OPENCV_AVAILABLE and cap and cap.isOpened() else "⏭️ Disabled"
    if OPENCV_AVAILABLE and not cap:
        init_camera()
    return jsonify({
        "status": "ok",
        "msg": "RESCUE RADAR Backend",
        "environment": "cloud" if DATABASE_URL else "local",
        "esp_status": esp_status,
        "camera_status": camera_status,
        "serial_available": SERIAL_AVAILABLE,
        "opencv_available": OPENCV_AVAILABLE,
        "ports": find_esp_ports()
    }), 200

@app.route('/stream')
def video_feed():
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera not supported"}), 503
    if not cap or not cap.isOpened():
        if not init_camera():
            return jsonify({"error": "No camera detected"}), 503
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snap')
def snap():
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera not supported"}), 503
    if not cap or not cap.isOpened():
        init_camera()
    if not cap or not cap.isOpened():
        return "No camera", 503
    ret, frame = cap.read()
    if ret:
        ret, buffer = cv2.imencode('.jpg', frame)
        return Response(buffer.tobytes(), mimetype='image/jpeg')
    return "Capture failed", 500

@app.route('/api/camera/status')
def camera_status():
    return jsonify({
        "live": OPENCV_AVAILABLE and cap and cap.isOpened() if OPENCV_AVAILABLE else False,
        "available": OPENCV_AVAILABLE,
        "device": 1
    })

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
        if not 0 <= distance_cm <= 10000:
            return jsonify({"error": "Invalid distance"}), 400
    except:
        return jsonify({"error": "Invalid number"}), 400

    victim_id = data.get("victim_id") or f"vic-{uuid.uuid4().hex[:8]}"
    try:
        reading = VictimReading.query.filter_by(victim_id=victim_id).first()
        if reading:
            reading.distance_cm = distance_cm
            reading.latitude = data.get("latitude")
            reading.longitude = data.get("longitude")
            reading.timestamp = datetime.utcnow()
            action = "UPDATED"
        else:
            reading = VictimReading(victim_id=victim_id, distance_cm=distance_cm,
                                  latitude=data.get("latitude"), longitude=data.get("longitude"),
                                  timestamp=datetime.utcnow())
            db.session.add(reading)
            action = "CREATED"
        db.session.commit()
        logger.info("%s %s: %.1fcm", action, victim_id, distance_cm)
        return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"error": "Database error"}), 500

@app.route("/send-sos", methods=["POST"])
def send_sos():
    success = send_esp_command("SOS_ON") if SERIAL_AVAILABLE else False
    return jsonify({"status": "sos_triggered", "success": success,
                   "message": "ESP32 unavailable" if not SERIAL_AVAILABLE else "SOS sent"}), 200

@app.route("/toggle-light", methods=["POST"])
def toggle_light():
    data = request.get_json() or {}
    status = data.get("status", "OFF")
    cmd = "LIGHT_ON" if status.upper() == "ON" else "LIGHT_OFF"
    success = send_esp_command(cmd) if SERIAL_AVAILABLE else False
    return jsonify({"status": "light_toggled", "success": success,
                   "light_status": status, "esp_command": cmd}), 200

@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)
    readings = VictimReading.query.order_by(VictimReading.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    return jsonify({
        "readings": [r.to_dict() for r in readings.items],
        "page": page, "per_page": per_page, "total": readings.total, "pages": readings.pages
    }), 200

@app.route("/api/v1/victims/<victim_id>/latest", methods=["GET"])
def latest_reading(victim_id):
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()
    return jsonify(reading.to_dict()) if reading else jsonify({"error": "Not found"}), 404

@app.route("/admin/clean-duplicates", methods=["POST"])
def clean_duplicates():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # Keep only latest per victim
        result = db.session.query(VictimReading.victim_id,
                                db.func.max(VictimReading.timestamp).label('max_ts')
                               ).group_by(VictimReading.victim_id).subquery()
        deleted = db.session.query(VictimReading).filter(
            ~VictimReading.timestamp.in_(db.session.query(result.c.max_ts))
        ).delete(synchronize_session=False)
        db.session.commit()
        return jsonify({"status": "cleaned", "deleted": deleted}), 200
    except:
        db.session.rollback()
        return jsonify({"error": "Cleanup failed"}), 500

@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    readings = VictimReading.query.order_by(VictimReading.timestamp.asc()).limit(5000).all()
    if not readings:
        return jsonify({"error": "No data"}), 404

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, height - 50, "Rescue Radar Export")
    p.setFont("Helvetica", 10)
    p.drawString(50, height - 75, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    
    y = height - 120
    p.setFont("Helvetica-Bold", 9)
    headers = ["ID", "Victim", "Distance", "Lat", "Lon", "Time"]
    for i, h in enumerate(headers):
        p.drawString(50 + i*80, y, h)
    
    p.setFont("Helvetica", 8)
    y -= 20
    for r in readings:
        if y < 100:
            p.showPage()
            y = height - 60
            p.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                p.drawString(50 + i*80, y, h)
            y -= 20
            p.setFont("Helvetica", 8)
        
        p.drawString(50, y, str(r.id))
        p.drawString(130, y, r.victim_id[:12])
        p.drawString(220, y, f"{r.distance_cm:.1f}cm")
        p.drawString(300, y, f"{r.latitude:.3f}" if r.latitude else "N/A")
        p.drawString(380, y, f"{r.longitude:.3f}" if r.longitude else "N/A")
        p.drawString(460, y, r.timestamp.strftime("%H:%M"))
        y -= 15
    
    p.showPage()
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"rescue_data.pdf", mimetype="application/pdf")

@app.route("/api/esp/status", methods=["GET"])
def esp_status():
    return jsonify({
        "connected": SERIAL_AVAILABLE and connect_esp(),
        "available": SERIAL_AVAILABLE,
        "port": ESP_PORT,
        "ports": find_esp_ports()
    })

@app.route("/admin/init-db", methods=["POST"])
def admin_init_db():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        db.create_all()
        return jsonify({"status": "Tables created"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------------------------------------
# STARTUP
# -----------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        if OPENCV_AVAILABLE:
            init_camera()
    logger.info("🚀 RESCUE RADAR ready - Local:%s Cloud:%s",
                "FULL" if SERIAL_AVAILABLE and OPENCV_AVAILABLE else "PARTIAL",
                "API+DB" if DATABASE_URL else "LOCAL")
    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)

# app.py - RESCUE RADAR backend (full edited)

# Compatibility shim for older code expecting pkgutil.get_loader
import pkgutil
import importlib.util
import os
import base64
import logging
import threading
import time
import uuid
from datetime import datetime
from io import BytesIO

# ensure get_loader exists (some Python installs remove it)
if not hasattr(pkgutil, "get_loader"):
    def _compat_get_loader(name):
        try:
            spec = importlib.util.find_spec(name)
            return spec.loader if spec is not None else None
        except Exception:
            return None
    pkgutil.get_loader = _compat_get_loader

from flask import Flask, request, jsonify, send_file, Response, abort
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy.exc import SQLAlchemyError

# Logging
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# CLOUD-SAFE CONDITIONAL IMPORTS
SERIAL_AVAILABLE = False
OPENCV_AVAILABLE = False

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except Exception:
    logger.warning("pyserial not available - ESP32 disabled")

try:
    import cv2
    OPENCV_AVAILABLE = True
except Exception:
    logger.warning("opencv-python not available - camera disabled")

# GLOBALS
esp_serial = None
cap = None

# APP INIT
app = Flask(__name__)
CORS(app)

# DATABASE
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///local_dev.db?check_same_thread=False"
    logger.info("Using SQLite (local)")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    logger.info("Using cloud DB")

WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "rescue-radar-dev")
ESP_PORT = os.environ.get("ESP_PORT", "/dev/cu.usbserial-*")
BAUD_RATE = int(os.environ.get("BAUD_RATE", 115200))

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# Ensure static folder exists for simulation
STATIC_DIR = os.path.join(os.getcwd(), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# If CAMERA_SIMULATE=1 and placeholder missing, create one tiny jpeg
PLACEHOLDER_PATH = os.path.join(STATIC_DIR, "snap.jpg")
_PLACEHOLDER_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxISEhUTEhIVFRUVFRUVFRUVFRUVFRUVFRUWFhUVFRUYHSggGBolGxUVITEhJSkrLi4uFx8zODMtNygtLisBCgoKDg0OGxAQGy0lICYtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLf/AABEIAKAAoAMBIgACEQEDEQH/xAAbAAACAwEBAQAAAAAAAAAAAAAEBQIDBgEHAP/EADQQAAIBAgQDBgQFBQAAAAAAAAECAwQRAAUSIRMxQWFxgZGh8AUiMqHB0fAUM2KSscH/xAAaAQACAwEBAAAAAAAAAAAAAAABAgADBAUG/8QALBEAAgIBAwMCBwAAAAAAAAAAAQIAEQMSITEEMkFRcQQiMlJhgaGx/9oADAMBAAIRAxEAPwD8qKqurq6urrq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6urq6v/2Q=="
)

def _ensure_placeholder():
    if not os.path.exists(PLACEHOLDER_PATH):
        try:
            with open(PLACEHOLDER_PATH, "wb") as f:
                f.write(base64.b64decode(_PLACEHOLDER_BASE64))
            logger.info("Created placeholder static/snap.jpg for CAMERA_SIMULATE")
        except Exception as e:
            logger.error("Failed to create placeholder image: %s", e)

# -----------------------------------------------------
# CAMERA (OpenCV) helpers
# -----------------------------------------------------
def init_camera():
    global cap
    if not OPENCV_AVAILABLE:
        logger.info("Camera skipped (no OpenCV)")
        return False
        
    try:
        # Try indices 0, 1, 2 to find the endoscope
        # Skip the built-in FaceTime camera
        for device_idx in [0, 1, 2]:
            try:
                cap = cv2.VideoCapture(device_idx)
            except Exception as e:
                logger.debug(f"Failed to open device {device_idx}: {e}")
                continue
            
            # Set UYVY422 format
            try:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('U', 'Y', 'V', 'Y'))
            except Exception:
                # Some OpenCV builds may ignore FOURCC; ignore failure
                pass
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 15)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                # not all backends support BUFFERSIZE
                pass
            
            # Test if this camera works
            ret, frame = cap.read()
            if ret and frame is not None and frame.size > 0:
                # Check resolution read back
                try:
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                except Exception:
                    width, height = 0, 0
                logger.info(f"✅ Camera at index {device_idx}: {width}x{height}")
                
                # If you want to skip FaceTime (usually 1280x720), uncomment:
                # if width == 1280 and height == 720:
                #     logger.info(f"Skipping FaceTime camera at index {device_idx}")
                #     cap.release()
                #     continue
                
                logger.info(f"✅ ENDOSCOPE LIVE: {width}x{height} uyvy422 (device {device_idx})")
                return True
            else:
                try:
                    cap.release()
                except Exception:
                    pass
                
        logger.warning("No working camera found")
        return False
        
    except Exception as e:
        logger.error(f"Camera error: {e}")
        return False

def gen_frames():
    global cap
    if not cap or not getattr(cap, "isOpened", lambda: False)():
        return
    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.1)
            continue
        try:
            # convert if needed and encode as jpeg
            if len(frame.shape) == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                frame_bytes = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n'
                yield frame_bytes
                frame_count += 1
                if frame_count % 60 == 0:
                    logger.info(f"Streaming {frame_count} frames...")
        except Exception as e:
            logger.debug("Frame encode error: %s", e)
            time.sleep(0.1)

# -----------------------------------------------------
# ESP32 (serial) helpers
# -----------------------------------------------------
def connect_esp():
    if not SERIAL_AVAILABLE:
        return False
    global esp_serial
    try:
        if esp_serial and getattr(esp_serial, "is_open", False):
            return True
        esp_serial = serial.Serial(ESP_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        logger.info(f"ESP32 connected: {ESP_PORT}")
        return True
    except Exception as e:
        logger.error(f"ESP32 failed: {e}")
        return False

def send_esp_command(cmd):
    if not SERIAL_AVAILABLE:
        return False
    if not connect_esp():
        return False
    try:
        esp_serial.write(f"{cmd}\n".encode())
        esp_serial.flush()
        logger.info(f"ESP CMD: {cmd}")
        return True
    except Exception as e:
        logger.error("Failed to send ESP command: %s", e)
        return False

def find_esp_ports():
    if not SERIAL_AVAILABLE:
        return []
    try:
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports if any(kw in (p.description or "").lower() for kw in ['esp', 'ch340', 'cp210'])]
    except Exception:
        return []

# -----------------------------------------------------
# DATABASE MODEL
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
        return {
            "id": self.id, "victim_id": self.victim_id, "distance_cm": self.distance_cm,
            "latitude": self.latitude, "longitude": self.longitude, "timestamp": iso_ts
        }

def require_key(req):
    return req.headers.get("x-api-key") == WRITE_API_KEY

# -----------------------------------------------------
# SIMULATION: MJPEG generator for static image
# -----------------------------------------------------
def _mjpeg_generator(path):
    """Yield MJPEG frames repeatedly using a static JPEG file."""
    try:
        with open(path, "rb") as f:
            frame = f.read()
    except Exception:
        logger.error("Failed to open placeholder for MJPEG: %s", path)
        return
    boundary = b"--frame\r\n"
    part_header = b"Content-Type: image/jpeg\r\n\r\n"
    while True:
        yield boundary + part_header + frame + b'\r\n'
        time.sleep(0.5)

# -----------------------------------------------------
# ROUTES
# -----------------------------------------------------
@app.route("/")
def home():
    esp_status = "✅ Live" if SERIAL_AVAILABLE and connect_esp() else "⏭️ Disabled"
    camera_status = "✅ Live" if OPENCV_AVAILABLE and cap and getattr(cap, "isOpened", lambda: False)() else "⏭️ Disabled"
    if OPENCV_AVAILABLE and (cap is None or not getattr(cap, "isOpened", lambda: False)()):
        init_camera()
    return jsonify({
        "status": "ok",
        "msg": "RESCUE RADAR - FULL SYSTEM LIVE",
        "environment": "cloud" if DATABASE_URL else "local dev",
        "esp32": esp_status,
        "camera": camera_status,
        "opencv": OPENCV_AVAILABLE,
        "serial": SERIAL_AVAILABLE,
        "ports": find_esp_ports()
    }), 200

@app.route('/stream')
def video_feed():
    # Camera simulation override
    if os.environ.get("CAMERA_SIMULATE") == "1":
        _ensure_placeholder()
        if not os.path.exists(PLACEHOLDER_PATH):
            return jsonify({"error": "Missing static/snap.jpg for camera simulation"}), 503
        return Response(_mjpeg_generator(PLACEHOLDER_PATH), mimetype='multipart/x-mixed-replace; boundary=frame')

    # Real camera path
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera unavailable"}), 503
    if not cap or not getattr(cap, "isOpened", lambda: False)():
        if not init_camera():
            return jsonify({"error": "No camera detected"}), 503
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snap')
def snap():
    # Simulation first
    if os.environ.get("CAMERA_SIMULATE") == "1":
        _ensure_placeholder()
        if not os.path.exists(PLACEHOLDER_PATH):
            return jsonify({"error": "Missing static/snap.jpg for camera simulation"}), 503
        return send_file(PLACEHOLDER_PATH, mimetype="image/jpeg")

    # Real camera
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera unavailable"}), 503
    if not cap or not getattr(cap, "isOpened", lambda: False)():
        init_camera()
    if not cap or not getattr(cap, "isOpened", lambda: False)():
        return jsonify({"error": "No camera"}), 503
    ret, frame = cap.read()
    if ret:
        ret, buffer = cv2.imencode('.jpg', frame)
        return Response(buffer.tobytes(), mimetype='image/jpeg')
    return jsonify({"error": "Capture failed"}), 500

@app.route('/api/camera/status')
def camera_status():
    return jsonify({
        "live": OPENCV_AVAILABLE and cap and getattr(cap, "isOpened", lambda: False)() if OPENCV_AVAILABLE else False,
        "available": OPENCV_AVAILABLE,
        "device": 1,
        "resolution": "640x480@15fps (pref)"
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
            reading = VictimReading(
                victim_id=victim_id, distance_cm=distance_cm,
                latitude=data.get("latitude"), longitude=data.get("longitude"),
                timestamp=datetime.utcnow()
            )
            db.session.add(reading)
            action = "CREATED"
        db.session.commit()
        logger.info("%s victim %s: %.1fcm", action, victim_id, distance_cm)
        return jsonify({"status": "ok", "action": action, "reading": reading.to_dict()}), 200
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.error(f"DB error: {e}")
        return jsonify({"error": "Database error"}), 500

@app.route("/send-sos", methods=["POST"])
def send_sos():
    success = send_esp_command("SOS_ON") if SERIAL_AVAILABLE else False
    logger.info("🚨 SOS pressed - ESP32: %s", "sent" if success else "disabled")
    return jsonify({
        "status": "sos_triggered",
        "success": success,
        "message": "Buzzer activated" if success else "ESP32 unavailable"
    }), 200

@app.route("/toggle-light", methods=["POST"])
def toggle_light():
    data = request.get_json() or {}
    status = data.get("status", "OFF")
    cmd = "LIGHT_ON" if status.upper() == "ON" else "LIGHT_OFF"
    success = send_esp_command(cmd) if SERIAL_AVAILABLE else False
    return jsonify({
        "status": "light_toggled",
        "light_status": status,
        "success": success
    }), 200
    
@app.route("/admin/clear-readings", methods=["POST"])
def clear_readings():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        num_deleted = db.session.query(VictimReading).delete()
        db.session.commit()
        return jsonify({"status": "cleared", "deleted": num_deleted}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
        

@app.route("/api/v1/readings/all", methods=["GET"])
def all_readings():
    page = request.args.get("page", 1, type=int)
    per_page = min(request.args.get("per_page", 50, type=int), 100)
    readings = VictimReading.query.order_by(VictimReading.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        "readings": [r.to_dict() for r in readings.items],
        "page": page, "per_page": per_page,
        "total": readings.total, "pages": readings.pages
    }), 200

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
        logger.error("init-db failed: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/admin/clean-duplicates", methods=["POST"])
def clean_duplicates():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        # keep most recent per victim_id
        subq = db.session.query(
            VictimReading.victim_id,
            db.func.max(VictimReading.timestamp).label('max_ts')
        ).group_by(VictimReading.victim_id).subquery()

        deleted = db.session.query(VictimReading).filter(
            ~VictimReading.timestamp.in_(db.session.query(subq.c.max_ts))
        ).delete(synchronize_session=False)
        db.session.commit()
        return jsonify({"status": "cleaned", "deleted": deleted}), 200
    except Exception as e:
        db.session.rollback()
        logger.error("clean-duplicates failed: %s", e)
        return jsonify({"error": "Cleanup failed", "detail": str(e)}), 500

@app.route("/api/v1/readings/export/pdf", methods=["GET"])
def export_readings_pdf():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    readings = VictimReading.query.order_by(VictimReading.timestamp.asc()).limit(5000).all()
    if not readings:
        return jsonify({"error": "No readings"}), 404

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    p.setFont("Helvetica-Bold", 16)
    p.drawString(50, height-50, "RESCUE RADAR - Victim Report")
    p.setFont("Helvetica", 10)
    p.drawString(50, height-75, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    y = height - 120
    p.setFont("Helvetica-Bold", 9)
    headers = ["ID", "Victim ID", "Distance", "Lat", "Lon", "Time"]
    x_pos = [50, 120, 220, 320, 390, 460]
    for i, h in enumerate(headers):
        p.drawString(x_pos[i], y, h)

    p.setFont("Helvetica", 8)
    y -= 20
    for r in readings:
        if y < 100:
            p.showPage()
            y = height - 60
            p.setFont("Helvetica-Bold", 9)
            for i, h in enumerate(headers):
                p.drawString(x_pos[i], y, h)
            y -= 20
            p.setFont("Helvetica", 8)

        p.drawString(x_pos[0], y, str(r.id))
        p.drawString(x_pos[1], y, (r.victim_id or "")[:15])
        p.drawString(x_pos[2], y, f"{r.distance_cm:.1f}cm")
        p.drawString(x_pos[3], y, f"{r.latitude:.4f}" if r.latitude is not None else "N/A")
        p.drawString(x_pos[4], y, f"{r.longitude:.4f}" if r.longitude is not None else "N/A")
        p.drawString(x_pos[5], y, r.timestamp.strftime("%H:%M"))
        y -= 15

    p.showPage()
    p.save()
    buffer.seek(0)
    return send_file(buffer, as_attachment=True,
                     download_name=f"rescue_report_{datetime.now().strftime('%Y%m%d')}.pdf",
                     mimetype="application/pdf")

# -----------------------------------------------------
# STARTUP
# -----------------------------------------------------
if __name__ == "__main__":
    # Create tables and optionally init camera
    with app.app_context():
        db.create_all()
        if os.environ.get("CAMERA_SIMULATE") == "1":
            _ensure_placeholder()
        if OPENCV_AVAILABLE:
            init_camera()

    logger.info("=" * 50)
    logger.info("🚀 RESCUE RADAR FULL SYSTEM")
    logger.info(f"📡 Local: http://0.0.0.0:5001 | Cloud: {'YES' if DATABASE_URL else 'NO'}")
    logger.info(f"🎥 Camera: {'✅ LIVE' if OPENCV_AVAILABLE and cap else '❌ OFF'}")
    logger.info(f"🔌 ESP32: {'✅ READY' if SERIAL_AVAILABLE else '❌ OFF'}")
    logger.info("=" * 50)

    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)

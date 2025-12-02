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

# CLOUD-SAFE CONDITIONAL IMPORTS
SERIAL_AVAILABLE = False
OPENCV_AVAILABLE = False

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    logging.warning("pyserial not available - ESP32 disabled")

try:
    import cv2
    OPENCV_AVAILABLE = True
except ImportError:
    logging.warning("opencv-python not available - camera disabled")

# GLOBALS
esp_serial = None
cap = None

# -----------------------------------------------------
# APP INIT
# -----------------------------------------------------
app = Flask(__name__)
CORS(app)
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

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
BAUD_RATE = 115200

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# -----------------------------------------------------
# FIXED USB ENDOSCOPE (uyvy422 + fallbacks)
# -----------------------------------------------------
def init_camera():
    global cap
    if not OPENCV_AVAILABLE:
        logger.info("Camera skipped (no OpenCV)")
        return False
        
    try:
        # PRIMARY: Device 1 (your HD endoscope)
        cap = cv2.VideoCapture(1)
        
        # UYVY422 PIXEL FORMAT FIX (from your ffplay)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('U', 'Y', 'V', 'Y'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        
        # CRITICAL: TEST FRAME
        ret, frame = cap.read()
        if ret and frame is not None and frame.size > 0:
            logger.info("✅ ENDOSCOPE LIVE: 640x480 uyvy422")
            return True
            
        logger.info("640x480 failed - fallback...")
        cap.release()
        
        # FALLBACK: Lower res
        cap = cv2.VideoCapture(1)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('U', 'Y', 'V', 'Y'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        cap.set(cv2.CAP_PROP_FPS, 10)
        ret, frame = cap.read()
        if ret and frame is not None:
            logger.info("✅ ENDOSCOPE OK: 320x240 fallback")
            return True
            
        logger.warning("No camera found")
        return False
        
    except Exception as e:
        logger.error(f"Camera error: {e}")
        return False

def gen_frames():
    global cap
    if not cap or not cap.isOpened():
        return
        
    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.1)
            continue
            
        try:
            if len(frame.shape) == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                frame_bytes = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n'
                yield frame_bytes
                frame_count += 1
                if frame_count % 60 == 0:
                    logger.info(f"Streaming {frame_count} frames...")
        except:
            time.sleep(0.1)

# -----------------------------------------------------
# ESP32 FUNCTIONS
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
        logger.info(f"ESP32 connected: {ESP_PORT}")
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
        logger.info(f"ESP CMD: {cmd}")
        return True
    except:
        return False

def find_esp_ports():
    if not SERIAL_AVAILABLE:
        return []
    try:
        ports = serial.tools.list_ports.comports()
        return [p.device for p in ports if any(kw in p.description.lower() for kw in ['esp', 'ch340', 'cp210'])]
    except:
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
# ROUTES - COMPLETE SYSTEM
# -----------------------------------------------------
@app.route("/")
def home():
    esp_status = "✅ Live" if SERIAL_AVAILABLE and connect_esp() else "⏭️ Disabled"
    camera_status = "✅ Live" if OPENCV_AVAILABLE and cap and cap.isOpened() else "⏭️ Disabled"
    if OPENCV_AVAILABLE and not cap:
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
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera unavailable"}), 503
    if not cap or not cap.isOpened():
        if not init_camera():
            return jsonify({"error": "No camera detected"}), 503
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snap')
def snap():
    if not OPENCV_AVAILABLE:
        return jsonify({"error": "Camera unavailable"}), 503
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
        "device": 1,
        "resolution": "640x480@15fps uyvy422"
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
        return jsonify({"error": str(e)}), 500

@app.route("/admin/clean-duplicates", methods=["POST"])
def clean_duplicates():
    if not require_key(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        result = db.session.query(
            VictimReading.victim_id, db.func.max(VictimReading.timestamp).label('max_ts')
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
        p.drawString(x_pos[1], y, r.victim_id[:15])
        p.drawString(x_pos[2], y, f"{r.distance_cm:.1f}cm")
        p.drawString(x_pos[3], y, f"{r.latitude:.4f}" if r.latitude else "N/A")
        p.drawString(x_pos[4], y, f"{r.longitude:.4f}" if r.longitude else "N/A")
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
    with app.app_context():
        db.create_all()
        if OPENCV_AVAILABLE:
            init_camera()
    logger.info("=" * 50)
    logger.info("🚀 RESCUE RADAR FULL SYSTEM")
    logger.info(f"📡 Local: http://0.0.0.0:5001 | Cloud: {DATABASE_URL and 'YES' or 'NO'}")
    logger.info(f"🎥 Camera: {'✅ LIVE' if OPENCV_AVAILABLE and cap else '❌ OFF'}")
    logger.info(f"🔌 ESP32: {'✅ READY' if SERIAL_AVAILABLE else '❌ OFF'}")
    logger.info("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=True, threaded=True)

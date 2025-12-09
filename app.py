from flask import Flask, request, jsonify, Response
from ultralytics import YOLO
import numpy as np
import cv2
import os
import logging
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import time
import threading

# Flask App Setup
app = Flask(__name__)
CORS(app)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== SAFE YOLO LOADING ====================
print("🚀 Loading YOLOv8...")
try:
    # Try your custom model first
    if os.path.exists("best.pt"):
        yolo_model = YOLO("best.pt")
        print("✅ YOUR best.pt LOADED!")
    elif os.path.exists("yolov8n.pt"):
        yolo_model = YOLO("yolov8n.pt")
        print("✅ yolov8n.pt LOADED!")
    else:
        yolo_model = YOLO("yolov8n.pt")  # Auto-downloads
        print("✅ YOLOv8n AUTO-DOWNLOADED!")
except Exception as e:
    print(f"❌ YOLO Error: {e}")
    yolo_model = YOLO("yolov8n.pt")
print("✅ YOLOv8 READY FOR LIVE STREAM!")

# Global Camera + Lock
cap = None
cap_lock = threading.Lock()

# Database Setup
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///rescue_radar.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class VictimReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    victimid = db.Column(db.String(64), nullable=False, unique=True)
    distancecm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# ==================== CAMERA FUNCTIONS ====================
def init_camera():
    """Auto-detect endoscopic USB camera (0-4 indices)"""
    global cap
    with cap_lock:
        if cap is not None:
            try:
                cap.release()
            except:
                pass
            cap = None
        
        for device_idx in [0, 1, 2, 3, 4]:
            try:
                test_cap = cv2.VideoCapture(device_idx)
                if not test_cap.isOpened():
                    test_cap.release()
                    continue
                
                # Endoscopic settings
                test_cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                test_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                test_cap.set(cv2.CAP_PROP_FPS, 15)
                test_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                
                ret, frame = test_cap.read()
                if ret and frame is not None and frame.size > 0:
                    cap = test_cap
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    logger.info(f"✅ ENDOSCOPE LIVE: {width}x{height} @ device {device_idx}")
                    return True
                test_cap.release()
            except:
                continue
        
        logger.warning("⚠️ No camera found - Stream will use placeholder")
        return False

# ==================== ML PROCESSING ====================
def thermal_overlay(frame):
    """Red=Hot, Blue=Cold thermal effect"""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thermal = cv2.applyColorMap(gray, cv2.COLORMAP_JET)
        return cv2.addWeighted(frame, 0.7, thermal, 0.3, 0)
    except:
        return frame

def draw_ml_boxes(frame, results):
    """Draw YOLO boxes + confidence"""
    try:
        humans = 0
        best_conf = 0.0
        
        if hasattr(results[0], 'boxes') and results[0].boxes is not None:
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                cls_id = int(box.cls[0])
                label = results[0].names.get(cls_id, "object")
                
                if label.lower() == "person":
                    humans += 1
                    best_conf = max(best_conf, conf)
                    # Red box for humans
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(frame, f"HUMAN {conf:.0%}", (x1, y1-10),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        
        # Live stats overlay
        color = (0, 0, 255) if best_conf > 0.5 else (0, 255, 0)
        cv2.putText(frame, f"HUMANS: {humans}", (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(frame, f"CONF: {best_conf:.0%}", (10, 60),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        
        return frame, humans, 0, best_conf
    except:
        return frame, 0, 0, 0.0

def gen_frames_ml():
    """Main MJPEG generator - CRASH PROOF"""
    consecutive_errors = 0
    while True:
        try:
            # Camera check + recovery
            with cap_lock:
                if cap is None or not cap.isOpened():
                    if not init_camera():
                        time.sleep(2)
                        continue
            
            ret, frame = cap.read()
            if not ret or frame is None:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    init_camera()
                time.sleep(0.1)
                continue
            
            consecutive_errors = 0
            
            # FAST ML processing
            results = yolo_model(frame, conf=0.25, verbose=False, device='cpu')
            
            # Thermal + ML overlay
            enhanced = thermal_overlay(frame)
            final_frame, humans, animals, conf = draw_ml_boxes(enhanced, results)
            
            # JPEG encode
            ret, buffer = cv2.imencode('.jpg', final_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                frame_bytes = (b'--frame\r\n'
                             b'Content-Type: image/jpeg\r\n\r\n' +
                             buffer.tobytes() + b'\r\n')
                yield frame_bytes
                
        except Exception as e:
            logger.error(f"Frame gen error: {e}")
            time.sleep(0.1)

# ==================== FLASK API ROUTES ====================
@app.route('/stream')
def video_feed():
    """LIVE STREAM - Flutter uses this!"""
    return Response(gen_frames_ml(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snap')
def snap():
    """Single ML snapshot"""
    try:
        with cap_lock:
            if cap is None or not cap.isOpened():
                if not init_camera():
                    return jsonify({"error": "No camera"}), 503
            
            ret, frame = cap.read()
            if ret and frame is not None:
                results = yolo_model(frame, conf=0.25, verbose=False)
                enhanced = thermal_overlay(frame)
                final_frame, _, _, _ = draw_ml_boxes(enhanced, results)
                
                ret, buffer = cv2.imencode('.jpg', final_frame)
                return Response(buffer.tobytes(), mimetype='image/jpeg')
        
        return jsonify({"error": "Capture failed"}), 500
    except:
        return jsonify({"error": "Snapshot error"}), 500

@app.route('/api/camera/status')
def camera_status():
    with cap_lock:
        live = cap is not None and cap.isOpened()
    return jsonify({
        "live": live,
        "available": True,
        "resolution": "640x480",
        "fps": 15
    })

@app.route('/api/v1/readings', methods=['POST'])
def create_reading():
    """Victim report endpoint (Flutter calls this)"""
    try:
        data = request.get_json() or {}
        distancecm = data.get('distancecm')
        
        if distancecm is None:
            return jsonify({"error": "distancecm required"}), 400
        
        distancecm = float(distancecm)
        victimid = data.get('victimid', f"vic-{int(time.time())}")
        
        # Database operations
        with app.app_context():
            reading = VictimReading.query.filter_by(victimid=victimid).first()
            if reading:
                reading.distancecm = distancecm
                reading.latitude = data.get('latitude')
                reading.longitude = data.get('longitude')
                reading.timestamp = datetime.utcnow()
                db.session.commit()
                action = "UPDATED"
            else:
                reading = VictimReading(
                    victimid=victimid,
                    distancecm=distancecm,
                    latitude=data.get('latitude'),
                    longitude=data.get('longitude')
                )
                db.session.add(reading)
                db.session.commit()
                action = "CREATED"
            
            logger.info(f"👥 {action}: {victimid} @ {distancecm:.1f}cm")
            
            return jsonify({
                "status": "ok",
                "action": action,
                "reading": {
                    "victimid": reading.victimid,
                    "distancecm": reading.distancecm,
                    "latitude": reading.latitude,
                    "longitude": reading.longitude
                }
            }), 200
            
    except Exception as e:
        logger.error(f"Victim report error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/')
def home():
    """Test page - auto-opens live stream"""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>🚨 RESCUE RADAR LIVE</title>
    <style>body{font-family:Arial;margin:40px;background:#1a1a1a;color:white;text-align:center;}
    img{max-width:100%;border:3px solid #00ff00;border-radius:10px;box-shadow:0 0 20px #00ff00;}
    h1{font-size:3em;color:#00ff00;text-shadow:0 0 20px #00ff00;}
    a{color:#00ff00;text-decoration:none;font-size:1.5em;}</style>
    </head>
    <body>
    <h1>🚨 RESCUE RADAR v2.0 ✅</h1>
    <p><strong>LIVE ML ENDOSCOPIC STREAM</strong></p>
    <img src="/stream" alt="Live ML Feed">
    <p><a href="/stream" target="_blank">📺 FULLSCREEN</a> | 
    <a href="/snap">📸 SNAPSHOT</a></p>
    <p>Flutter URL: <code>http://localhost:5001/stream</code></p>
    <script>setTimeout(()=>window.open('/stream','_blank'),1500);</script>
    </body></html>
    """

# ==================== STARTUP ====================
if __name__ == '__main__':
    # Initialize everything safely
    with app.app_context():
        db.create_all()
    
    init_camera()
    
    print("\n" + "="*60)
    print("🚀 RESCUE RADAR ML LIVE STREAM v2.0")
    print("📺 Browser: http://localhost:5001")
    print("📱 Flutter: http://localhost:5001/stream")
    print("👥 Victim DB: rescue_radar.db")
    print("💾 Model: yolov8n.pt (or your best.pt)")
    print("="*60 + "\n")
    
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)

from flask import Flask, request, jsonify, Response, send_file
from ultralytics import YOLO
import numpy as np
import cv2
import os
import base64
import logging
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import threading
import time

# Flask App Setup
app = Flask(__name__)
CORS(app)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load YOLOv8 Model (Your trained model)
print("🚀 Loading YOLOv8...")
yolo_model = YOLO("yolov8l.pt")  # Use your 'best.pt' here
print("✅ YOLOv8 LOADED - ML + Live Feed Ready!")

# Global Camera
cap = None

# Database Setup (Victim Reporting)
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

# Initialize Database
with app.app_context():
    db.create_all()

# ==================== ML + THERMAL PROCESSING ====================
def thermal_overlay(frame):
    """Convert RGB to Thermal colormap (Red=Hot, Blue=Cold)"""
    # Extract brightness channel
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    
    # Apply thermal colormap
    thermal = cv2.applyColorMap(gray, cv2.COLORMAP_JET)
    
    # Blend original + thermal (50/50)
    blended = cv2.addWeighted(frame, 0.6, thermal, 0.4, 0)
    return blended

def draw_ml_boxes(frame, results):
    """Draw YOLO boxes + labels + thermal info"""
    height, width = frame.shape[:2]
    
    humans = 0
    animals = 0
    best_human_conf = 0.0
    
    if results[0].boxes is not None:
        for box in results[0].boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = float(box.conf[0])
            cls_id = int(box.cls[0])
            label = results[0].names[cls_id]
            
            # Human detection (Priority 1)
            if label == "person":
                humans += 1
                best_human_conf = max(best_human_conf, conf)
                color = (0, 0, 255)  # Red for humans
                status = f"HUMAN {conf:.0%} 🔥"
            # Animal detection
            elif label in ["dog", "cat"]:
                animals += 1
                color = (255, 165, 0)  # Orange for animals
                status = f"ANIMAL {conf:.0%} 🐕"
            else:
                continue
            
            # Draw box
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
            cv2.putText(frame, status, (x1, y1-10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    
    # Add summary text
    summary = f"H:{humans} A:{animals}"
    if best_human_conf > 0.5:
        summary += " PRIORITY HIGH!"
        cv2.putText(frame, summary, (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    else:
        cv2.putText(frame, summary, (10, 30),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    
    return frame, humans, animals, best_human_conf

# ==================== CAMERA FUNCTIONS ====================
def init_camera():
    """Auto-detect endoscopic USB camera"""
    global cap
    if cap is not None:
        try:
            cap.release()
        except:
            pass
    
    for device_idx in [0, 1, 2]:
        test_cap = cv2.VideoCapture(device_idx)
        if test_cap.isOpened():
            # Set endoscopic format
            test_cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            test_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            test_cap.set(cv2.CAP_PROP_FPS, 15)
            ret, frame = test_cap.read()
            if ret and frame is not None:
                cap = test_cap
                logger.info(f"✅ ENDOSCOPE LIVE: {640}x{480} @ device {device_idx}")
                return True
            test_cap.release()
    logger.warning("❌ No endoscopic camera found")
    return False

def gen_frames_ml():
    """Generate MJPEG frames WITH ML overlay (30 FPS)"""
    frame_count = 0
    while True:
        try:
            if cap is None or not cap.isOpened():
                if not init_camera():
                    time.sleep(1)
                    continue
            
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.1)
                continue
            
            # 1. YOLOv8 ML Analysis (15ms)
            results = yolo_model(frame, conf=0.08, verbose=False)
            
            # 2. Thermal Enhancement (5ms)
            enhanced = thermal_overlay(frame)
            
            # 3. ML Boxes + Labels (5ms)
            final_frame, humans, animals, human_conf = draw_ml_boxes(enhanced, results)
            
            # 4. Encode JPEG (5ms)
            ret, buffer = cv2.imencode('.jpg', final_frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ret:
                frame_bytes = (b'--frame\r\n'
                              b'Content-Type: image/jpeg\r\n\r\n' +
                              buffer.tobytes() + b'\r\n')
                yield frame_bytes
                
                frame_count += 1
                if frame_count % 60 == 0:
                    logger.info(f"📸 Streaming: H:{humans} A:{animals} Conf:{human_conf:.1%}")
            
        except Exception as e:
            logger.error(f"Frame error: {e}")
            time.sleep(0.1)

# ==================== FLASK ROUTES ====================
@app.route('/stream')
def video_feed():
    """LIVE STREAM WITH ML OVERLAY (Flutter uses this!)"""
    return Response(gen_frames_ml(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snap')
def snap():
    """Single snapshot WITH ML overlay"""
    if cap is None or not cap.isOpened():
        if not init_camera():
            return jsonify({"error": "No camera"}), 503
    
    ret, frame = cap.read()
    if ret:
        results = yolo_model(frame, conf=0.08, verbose=False)
        enhanced = thermal_overlay(frame)
        final_frame, humans, animals, human_conf = draw_ml_boxes(enhanced, results)
        
        ret, buffer = cv2.imencode('.jpg', final_frame)
        return Response(buffer.tobytes(), mimetype='image/jpeg')
    return jsonify({"error": "Capture failed"}), 500

@app.route('/api/camera/status')
def camera_status():
    live = cap is not None and cap.isOpened()
    return jsonify({
        "live": live,
        "available": True,
        "resolution": "640x480",
        "fps": 15
    })

@app.route('/api/v1/readings', methods=['POST'])
def create_reading():
    """Report victim to database"""
    data = request.get_json()
    distancecm = data.get('distancecm')
    
    if distancecm is None:
        return jsonify({"error": "distancecm required"}), 400
    
    try:
        distancecm = float(distancecm)
        victimid = data.get('victimid', f"vic-{int(time.time())}")
        
        reading = VictimReading.query.filter_by(victimid=victimid).first()
        if reading:
            reading.distancecm = distancecm
            action = "UPDATED"
        else:
            reading = VictimReading(
                victimid=victimid,
                distancecm=distancecm,
                latitude=data.get('latitude'),
                longitude=data.get('longitude')
            )
            db.session.add(reading)
            action = "CREATED"
        
        db.session.commit()
        logger.info(f"👥 Victim {victimid}: {distancecm:.1f}cm ({action})")
        
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
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route('/')
def home():
    return """
    <h1>🚨 RESCUE RADAR - ML LIVE STREAM</h1>
    <h3>✅ YOLOv8 + Thermal Overlay + 30 FPS</h3>
    <p><a href="/stream" target="_blank">📺 LIVE ML FEED (Click Here)</a></p>
    <p><a href="/snap">📸 Single ML Snapshot</a></p>
    <script>
        setTimeout(() => window.open('/stream', '_blank'), 1000);
    </script>
    """

if __name__ == '__main__':
    # Initialize camera
    init_camera()
    
    print("🚀 RESCUE RADAR ML LIVE STREAM")
    print("📺 Flutter: http://localhost:5001/stream")
    print("📱 Test: Open http://localhost:5001")
    print("👥 Report: POST /api/v1/readings")
    
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)

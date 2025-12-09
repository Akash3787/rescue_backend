import os
import logging
import time
import threading
from datetime import datetime

from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# Optional heavy deps (YOLO, OpenCV) only when running locally
RUN_HEAVY_ML = not os.environ.get("RAILWAY_ENVIRONMENT")

if RUN_HEAVY_ML:
    from ultralytics import YOLO
    import cv2
    import numpy as np

# ==================== FLASK SETUP ====================
app = Flask(__name__)
CORS(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== DATABASE SETUP ====================
# Prefer remote DB from env (Railway), else fallback to local SQLite
db_url = os.environ.get("DATABASE_URL", "sqlite:///rescue_radar.db")
if db_url.startswith("mysql://"):
    # Use PyMySQL driver
    db_url = db_url.replace("mysql://", "mysql+pymysql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


class VictimReading(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    victimid = db.Column(db.String(64), nullable=False, unique=True)
    distancecm = db.Column(db.Float, nullable=False)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


# ==================== YOLO & CAMERA (LOCAL ONLY) ====================
yolo_model = None
cap = None
cap_lock = threading.Lock()


def safe_init_yolo():
    """Load YOLO only when heavy ML is enabled (local)."""
    global yolo_model
    if not RUN_HEAVY_ML:
        return

    try:
        print("🚀 Loading YOLOv8 (local only)...")
        if os.path.exists("best.pt"):
            yolo_model = YOLO("best.pt")
            print("✅ YOUR best.pt LOADED!")
        elif os.path.exists("yolov8n.pt"):
            yolo_model = YOLO("yolov8n.pt")
            print("✅ yolov8n.pt LOADED!")
        else:
            yolo_model = YOLO("yolov8n.pt")
            print("✅ YOLOv8n AUTO-DOWNLOADED!")
        print("✅ YOLOv8 READY FOR LIVE STREAM (LOCAL)!")
    except Exception as e:
        print(f"❌ YOLO Error: {e}")
        yolo_model = None


def init_camera():
    """Auto-detect USB camera (LOCAL ONLY)."""
    if not RUN_HEAVY_ML:
        return False

    global cap
    with cap_lock:
        # Cleanup old
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
            cap = None

        # Try multiple indices
        for device_idx in [0, 1, 2, 3, 4]:
            try:
                test_cap = cv2.VideoCapture(device_idx)
                if not test_cap.isOpened():
                    test_cap.release()
                    continue

                test_cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                test_cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                test_cap.set(cv2.CAP_PROP_FPS, 15)
                test_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                ret, frame = test_cap.read()
                if ret and frame is not None and frame.size > 0:
                    cap = test_cap
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    logger.info(f"✅ CAMERA LIVE: {width}x{height} @ device {device_idx}")
                    return True
                test_cap.release()
            except Exception:
                continue

        logger.warning("⚠️ No camera found - will use placeholder.")
        return False


def thermal_overlay(frame):
    """Red=Hot, Blue=Cold thermal effect."""
    if not RUN_HEAVY_ML:
        return frame
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        thermal = cv2.applyColorMap(gray, cv2.COLORMAP_JET)
        return cv2.addWeighted(frame, 0.7, thermal, 0.3, 0)
    except Exception:
        return frame


def draw_ml_boxes(frame, results):
    """Draw YOLO boxes + confidence."""
    if not RUN_HEAVY_ML or results is None:
        return frame, 0, 0, 0.0

    try:
        humans = 0
        best_conf = 0.0

        if hasattr(results[0], "boxes") and results[0].boxes is not None:
            for box in results[0].boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                cls_id = int(box.cls[0])
                label = results[0].names.get(cls_id, "object")

                if label.lower() == "person":
                    humans += 1
                    best_conf = max(best_conf, conf)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    cv2.putText(
                        frame,
                        f"HUMAN {conf:.0%}",
                        (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 0, 255),
                        2,
                    )

        color = (0, 0, 255) if best_conf > 0.5 else (0, 255, 0)
        cv2.putText(
            frame, f"HUMANS: {humans}", (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2
        )
        cv2.putText(
            frame, f"CONF: {best_conf:.0%}", (10, 60),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2
        )

        return frame, humans, 0, best_conf
    except Exception:
        return frame, 0, 0, 0.0


def gen_frames_ml():
    """Main MJPEG generator (LOCAL ONLY)."""
    if not RUN_HEAVY_ML or yolo_model is None:
        # On Railway this generator should not be called
        while True:
            time.sleep(1)
            yield b""

    consecutive_errors = 0
    while True:
        try:
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

            results = yolo_model(frame, conf=0.25, verbose=False, device="cpu")
            enhanced = thermal_overlay(frame)
            final_frame, humans, animals, conf = draw_ml_boxes(enhanced, results)

            ret, buffer = cv2.imencode(".jpg", final_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret:
                frame_bytes = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                )
                yield frame_bytes

        except Exception as e:
            logger.error(f"Frame gen error: {e}")
            time.sleep(0.1)


# ==================== ROUTES ====================
@app.route("/stream")
def video_feed():
    """Live stream (LOCAL only; disabled on Railway)."""
    if not RUN_HEAVY_ML or yolo_model is None:
        return jsonify({"error": "Live stream not available on server"}), 503
    return Response(gen_frames_ml(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/snap")
def snap():
    """Single ML snapshot (LOCAL only)."""
    if not RUN_HEAVY_ML or yolo_model is None:
        return jsonify({"error": "Snapshot not available on server"}), 503

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

                ret, buffer = cv2.imencode(".jpg", final_frame)
                return Response(buffer.tobytes(), mimetype="image/jpeg")

        return jsonify({"error": "Capture failed"}), 500
    except Exception as e:
        logger.error(f"Snapshot error: {e}")
        return jsonify({"error": "Snapshot error"}), 500


@app.route("/api/camera/status")
def camera_status():
    if not RUN_HEAVY_ML:
        return jsonify({
            "live": False,
            "available": False,
            "resolution": None,
            "fps": None,
            "environment": "railway"
        })

    with cap_lock:
        live = cap is not None and cap.isOpened()
    return jsonify({
        "live": live,
        "available": True,
        "resolution": "640x480",
        "fps": 15,
        "environment": "local"
    })


@app.route("/api/v1/readings", methods=["POST"])
def create_reading():
    """Victim report endpoint."""
    try:
        data = request.get_json() or {}
        distancecm = data.get("distancecm")

        if distancecm is None:
            return jsonify({"error": "distancecm required"}), 400

        distancecm = float(distancecm)
        victimid = data.get("victimid", f"vic-{int(time.time())}")

        reading = VictimReading.query.filter_by(victimid=victimid).first()
        if reading:
            reading.distancecm = distancecm
            reading.latitude = data.get("latitude")
            reading.longitude = data.get("longitude")
            reading.timestamp = datetime.utcnow()
            db.session.commit()
            action = "UPDATED"
        else:
            reading = VictimReading(
                victimid=victimid,
                distancecm=distancecm,
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
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
                "longitude": reading.longitude,
            },
        }), 200

    except Exception as e:
        logger.error(f"Victim report error: {e}")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/")
def home():
    """Simple home page that works both on Railway and locally."""
    env = "LOCAL+ML" if RUN_HEAVY_ML else "RAILWAY"
    return f"""
    <!DOCTYPE html>
    <html>
    <head><title>🚨 RESCUE RADAR LIVE ({env})</title></head>
    <body style="font-family:Arial;margin:40px;background:#1a1a1a;color:white;text-align:center;">
        <h1 style="color:#00ff00;">🚨 RESCUE RADAR v2.0 ✅ ({env})</h1>
        <p><strong>Victim API Online</strong></p>
        <p>POST <code>/api/v1/readings</code> with JSON body from Flutter.</p>
        <p>Camera/YOLO stream is only available when running locally.</p>
        <p>Flutter Stream URL (local only): <code>http://localhost:5001/stream</code></p>
    </body>
    </html>
    """


# ==================== STARTUP ====================
with app.app_context():
    db.create_all()

if RUN_HEAVY_ML:
    safe_init_yolo()
    init_camera()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("\n" + "=" * 60)
    print("🚀 RESCUE RADAR BACKEND")
    print(f"📺 Environment: {'LOCAL+ML' if RUN_HEAVY_ML else 'RAILWAY'}")
    print(f"🌐 Listening on port {port}")
    print(f"💾 DB: {db_url}")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


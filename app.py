"""
Rescue Radar - Flask backend (single-file)
Features:
- SQLite by default (can use DATABASE_URL env var)
- /api/v1/readings POST to create/update a victim reading (distance + optional T/H/G + lat/lon)
- Emits Socket.IO event "reading_update" on new/updated reading
- /api/v1/readings/all to fetch the most recent readings (paginated)
- Simple homepage showing latest distance in plain text (for quick browser debug)
- /stream and camera simulate removed for brevity (focus: distance)

Run: python rescue_radar_backend.py
"""

import os
import uuid
import logging
from datetime import datetime
from io import BytesIO
from flask import Flask, request, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_socketio import SocketIO

# ---- logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("rescue_radar")

# ---- app + config
app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    logger.info("Using DATABASE_URL from environment")
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///rescue_radar.db?check_same_thread=False"
    logger.info("Using local SQLite database rescue_radar.db")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
WRITE_API_KEY = os.environ.get("WRITE_API_KEY", "rescue-radar-dev")

# ---- db
db = SQLAlchemy(app)

class VictimReading(db.Model):
    __tablename__ = 'victim_readings'
    id = db.Column(db.Integer, primary_key=True)
    victim_id = db.Column(db.String(64), nullable=False, index=True, unique=True)
    distance_cm = db.Column(db.Float, nullable=False)
    temperature_c = db.Column(db.Float)
    humidity_pct = db.Column(db.Float)
    gas_ppm = db.Column(db.Float)
    latitude = db.Column(db.Float)
    longitude = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'victim_id': self.victim_id,
            'distance_cm': self.distance_cm,
            'temperature_c': self.temperature_c,
            'humidity_pct': self.humidity_pct,
            'gas_ppm': self.gas_ppm,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'timestamp': self.timestamp.isoformat() + 'Z' if isinstance(self.timestamp, datetime) else str(self.timestamp)
        }

# ---- helpers
def require_key(req):
    return req.headers.get('x-api-key') == WRITE_API_KEY

# ---- routes
@app.route('/')
def home():
    # simple debug page: show latest distance
    latest = VictimReading.query.order_by(VictimReading.timestamp.desc()).first()
    if not latest:
        return '<h2>Rescue Radar</h2><p>No readings yet.</p>', 200
    return f'<h2>Latest distance: {latest.distance_cm:.1f} cm</h2><p>victim: {latest.victim_id} @ {latest.timestamp}</p>', 200

@app.route('/api/v1/readings', methods=['POST'])
def create_reading():
    if not require_key(request):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(force=True) or {}
    # distance required
    distance = data.get('distance_cm')
    if distance is None:
        return jsonify({'error': 'distance_cm required'}), 400
    try:
        distance = float(distance)
    except Exception:
        return jsonify({'error': 'Invalid distance value'}), 400

    # optional fields
    temperature = data.get('temperature')
    humidity = data.get('humidity')
    gas = data.get('gas')
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    victim_id = data.get('victim_id') or f"vic-{uuid.uuid4().hex[:8]}"

    try:
        temperature = float(temperature) if temperature is not None else None
    except Exception:
        temperature = None
    try:
        humidity = float(humidity) if humidity is not None else None
    except Exception:
        humidity = None
    try:
        gas = float(gas) if gas is not None else None
    except Exception:
        gas = None

    # upsert by victim_id
    reading = VictimReading.query.filter_by(victim_id=victim_id).first()
    if reading:
        reading.distance_cm = distance
        reading.temperature_c = temperature
        reading.humidity_pct = humidity
        reading.gas_ppm = gas
        reading.latitude = latitude
        reading.longitude = longitude
        reading.timestamp = datetime.utcnow()
        action = 'UPDATED'
    else:
        reading = VictimReading(
            victim_id=victim_id,
            distance_cm=distance,
            temperature_c=temperature,
            humidity_pct=humidity,
            gas_ppm=gas,
            latitude=latitude,
            longitude=longitude,
            timestamp=datetime.utcnow()
        )
        db.session.add(reading)
        action = 'CREATED'

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception('DB commit failed')
        return jsonify({'error': 'database error'}), 500

    # emit socketio event for realtime clients
    try:
        socketio.emit('reading_update', {'reading': reading.to_dict()})
    except Exception:
        logger.exception('socket emit failed')

    logger.info('%s victim %s distance=%.2f', action, victim_id, distance)
    return jsonify({'status': 'ok', 'action': action, 'reading': reading.to_dict()}), 200

@app.route('/api/v1/readings/all', methods=['GET'])
def all_readings():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 500)
    q = VictimReading.query.order_by(VictimReading.timestamp.desc())
    pag = q.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        'readings': [r.to_dict() for r in pag.items],
        'page': page,
        'per_page': per_page,
        'total': pag.total,
        'pages': pag.pages
    }), 200

# ---- admin helpers
@app.route('/admin/init-db', methods=['POST'])
def admin_init_db():
    if not require_key(request):
        return jsonify({'error': 'Unauthorized'}), 401
    db.create_all()
    return jsonify({'status': 'ok'}), 200

# ---- run
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    logger.info('Starting Rescue Radar backend on 0.0.0.0:5001')
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)), debug=False)


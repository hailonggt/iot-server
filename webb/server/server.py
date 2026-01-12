import os
import io
import json
import time
import math
import random
import secrets
import functools

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

app = Flask(
    __name__,
    static_folder=WEB_DIR,
    template_folder=WEB_DIR,
    static_url_path=""
)

CORS(app)


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "https://baochay-cad24-default-rtdb.asia-southeast1.firebasedatabase.app"
)

DEVICE_KEY = os.getenv("DEVICE_KEY", "").strip()

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "123456")

TOKENS = {}
TOKEN_TTL_SEC = 12 * 60 * 60

AI_SAMPLES = []
MAX_AI_SAMPLES = 2000
DEFAULT_VENT = 0.5


def _now_ts():
    return int(time.time())


def init_firebase():
    if firebase_admin._apps:
        return

    env_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if not env_json:
        raise RuntimeError("Missing FIREBASE_SERVICE_ACCOUNT_JSON")

    cred_dict = json.loads(env_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})


@app.before_request
def before_any_request():
    init_firebase()


def fb_ref(path):
    return db.reference(path)


# serve web
@app.route("/")
def home():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(WEB_DIR, path)


def compute_online(last_ts, timeout_sec=25):
    if not last_ts:
        return False
    return (_now_ts() - int(last_ts)) <= timeout_sec


def build_payload_from_request(data):
    smoke = int(data.get("smoke", 0) or 0)
    temperature = float(data.get("temperature", 0) or 0)
    humidity = float(data.get("humidity", 0) or 0)

    ventilation = data.get("ventilation", DEFAULT_VENT)
    try:
        ventilation = float(ventilation)
    except Exception:
        ventilation = DEFAULT_VENT

    ventilation = max(0.0, min(1.0, ventilation))

    return {
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "ventilation": ventilation,
        "timestamp": _now_ts()
    }


def mean_std(values):
    if not values:
        return 0.0, 1.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var) if var > 0 else 1.0
    return mean, std


def ai_evaluate(sample):
    if len(AI_SAMPLES) < 30:
        return "AN TOÀN", 1

    smokes = [s["smoke"] for s in AI_SAMPLES]
    temps = [s["temperature"] for s in AI_SAMPLES]
    hums = [s["humidity"] for s in AI_SAMPLES]

    mean_s, std_s = mean_std(smokes)
    mean_t, std_t = mean_std(temps)
    mean_h, std_h = mean_std(hums)

    z_smoke = (sample["smoke"] - mean_s) / std_s
    z_temp = (sample["temperature"] - mean_t) / std_t
    z_hum = (sample["humidity"] - mean_h) / std_h

    if z_smoke >= 3.0 or z_temp >= 3.0:
        return "NGUY HIỂM", 3
    if z_smoke >= 1.5 or z_temp >= 1.8 or z_hum >= 2.5:
        return "CẢNH BÁO", 2
    return "AN TOÀN", 1


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "server_time": _now_ts()})


@app.post("/api/sensor")
def post_sensor():
    if DEVICE_KEY:
        got = request.headers.get("X-Device-Key", "").strip()
        if not got or not secrets.compare_digest(got, DEVICE_KEY):
            return jsonify({"ok": False, "error": "Invalid device key"}), 401

    data = request.get_json(silent=True) or {}
    sample = build_payload_from_request(data)

    status, level = ai_evaluate(sample)
    payload = {**sample, "status": status, "level": level}

    AI_SAMPLES.append(sample)
    if len(AI_SAMPLES) > MAX_AI_SAMPLES:
        AI_SAMPLES.pop(0)

    fb_ref("sensor/current").set(payload)
    fb_ref("sensor/history").push(payload)

    return jsonify({"ok": True, "status": status, "level": level})


@app.get("/api/current")
def get_current():
    cur = fb_ref("sensor/current").get() or {}
    last_ts = int(cur.get("timestamp", 0) or 0)
    cur["online"] = compute_online(last_ts)
    return jsonify(cur)


@app.get("/api/history")
def get_history():
    limit = int(request.args.get("limit", 20))
    snap = fb_ref("sensor/history").order_by_child("timestamp").limit_to_last(limit).get() or {}

    items = []
    for key, val in snap.items():
        val["_key"] = key
        items.append(val)

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"ok": True, "items": items})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

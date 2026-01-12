import os
import io
import json
import time
import math
import secrets
import functools

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

from openpyxl import Workbook


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

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "123456")

DEVICE_KEY = os.getenv("DEVICE_KEY", "").strip()

TOKENS = {}
TOKEN_TTL = 12 * 60 * 60


def now_ts():
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
def before_any():
    init_firebase()


def fb(path):
    return db.reference(path)


@app.route("/")
def home():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(WEB_DIR, path)


def compute_online(last_ts):
    if not last_ts:
        return False
    return (now_ts() - int(last_ts)) <= 25


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "server_time": now_ts()})


@app.post("/api/sensor")
def post_sensor():
    if DEVICE_KEY:
        got = request.headers.get("X-Device-Key", "").strip()
        if not got or not secrets.compare_digest(got, DEVICE_KEY):
            return jsonify({"ok": False, "error": "Invalid device key"}), 401

    data = request.get_json() or {}

    payload = {
        "smoke": int(data.get("smoke", 0)),
        "temperature": float(data.get("temperature", 0)),
        "humidity": float(data.get("humidity", 0)),
        "timestamp": now_ts(),
    }

    payload["status"] = "AN TOÀN"
    payload["level"] = 1

    fb("sensor/current").set(payload)
    fb("sensor/history").push(payload)

    return jsonify({"ok": True})


@app.get("/api/current")
def api_current():
    cur = fb("sensor/current").get() or {}
    ts = int(cur.get("timestamp", 0) or 0)
    cur["online"] = compute_online(ts)
    return jsonify(cur)


@app.get("/api/history")
def api_history():
    limit = int(request.args.get("limit", 20))
    snap = fb("sensor/history").order_by_child("timestamp").limit_to_last(limit).get() or {}

    items = []
    for k, v in snap.items():
        items.append(v)

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"ok": True, "items": items})


@app.post("/api/login")
def login():
    data = request.get_json() or {}
    if data.get("username") != ADMIN_USER or data.get("password") != ADMIN_PASS:
        return jsonify({"ok": False, "error": "Sai tài khoản hoặc mật khẩu"})

    token = secrets.token_hex(24)
    TOKENS[token] = now_ts()

    return jsonify({"ok": True, "token": token})


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401

        token = auth.replace("Bearer ", "")
        ts = TOKENS.get(token)

        if not ts or now_ts() - ts > TOKEN_TTL:
            return jsonify({"ok": False, "error": "Token expired"}), 401

        return fn(*args, **kwargs)
    return wrapper


@app.post("/api/logout")
@require_auth
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "")
    TOKENS.pop(token, None)
    return jsonify({"ok": True})


@app.get("/api/admin/export_excel")
@require_auth
def export_excel():
    snap = fb("sensor/history").order_by_child("timestamp").get() or {}

    wb = Workbook()
    ws = wb.active
    ws.append(["Time", "Smoke", "Temp", "Hum", "Status"])

    for k, v in snap.items():
        ws.append([
            time.strftime("%H:%M:%S", time.localtime(v["timestamp"])),
            v.get("smoke", 0),
            v.get("temperature", 0),
            v.get("humidity", 0),
            v.get("status", "")
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return send_from_directory(
        directory=".",
        path="iot.xlsx",
        as_attachment=True
    )


@app.post("/api/admin/delete_history")
@require_auth
def delete_history():
    fb("sensor/history").delete()
    return jsonify({"ok": True})


@app.post("/api/admin/train_ai")
@require_auth
def train_ai():
    return jsonify({"ok": True, "trained_samples": 0})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

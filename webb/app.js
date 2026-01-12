import os
import json
import time
import random

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

app = Flask(__name__, static_folder="../web", static_url_path="")
CORS(app)

DATABASE_URL = "https://baochay-cad24-default-rtdb.asia-southeast1.firebasedatabase.app"

def init_firebase():
    if firebase_admin._apps:
        return

    key_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not key_json:
        raise RuntimeError("Missing FIREBASE_SERVICE_ACCOUNT_JSON")

    cred = credentials.Certificate(json.loads(key_json))
    firebase_admin.initialize_app(cred, {
        "databaseURL": DATABASE_URL
    })

init_firebase()

def now_ts():
    return int(time.time())

def fb_ref(path):
    return db.reference(path)

@app.route("/")
def home():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/<path:path>")
def static_proxy(path):
    return send_from_directory(app.static_folder, path)

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": now_ts()})

@app.post("/api/sensor")
def post_sensor():
    data = request.get_json(silent=True) or {}

    payload = {
        "smoke": int(data.get("smoke", 0)),
        "temperature": float(data.get("temperature", 0)),
        "humidity": float(data.get("humidity", 0)),
        "timestamp": now_ts()
    }

    fb_ref("sensor/current").set(payload)
    fb_ref("sensor/history").push(payload)

    return jsonify({"ok": True})

@app.get("/api/current")
def get_current():
    cur = fb_ref("sensor/current").get() or {}
    return jsonify(cur)

@app.get("/api/history")
def get_history():
    snap = fb_ref("sensor/history").order_by_child("timestamp").limit_to_last(20).get() or {}

    items = []
    for _, val in snap.items():
        items.append(val)

    items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    return jsonify({"ok": True, "items": items})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

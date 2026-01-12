import os
import io
import time
import json
import pickle

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import numpy as np
from sklearn.ensemble import IsolationForest
from openpyxl import Workbook


# ==============================
# PATH
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))
MODEL_PATH = os.path.join(BASE_DIR, "model.pkl")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "https://baochay-cad24-default-rtdb.asia-southeast1.firebasedatabase.app"
)

# DÙNG ĐÚNG BIẾN BẠN ĐẶT TRÊN RENDER
FIREBASE_CRED_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

SECRET_KEY = os.getenv("IOT_SECRET_KEY", "iot_secret_key_change_me")
ADMIN_USER = os.getenv("IOT_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("IOT_ADMIN_PASS", "admin123")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))

ONLINE_WINDOW_SEC = 20

SMOKE_SAFE_MAX = 400
SMOKE_DANGER_MIN = 700
TEMP_DANGER_MIN = 55

STATUS_SAFE = "AN TOÀN"
STATUS_WARN = "CẢNH BÁO"
STATUS_DANGER = "NGUY HIỂM"


# ==============================
# FLASK APP
# ==============================
app = Flask(
    __name__,
    static_folder=WEB_DIR,
    static_url_path=""
)
CORS(app)

serializer = URLSafeTimedSerializer(SECRET_KEY)


# ==============================
# FIREBASE INIT
# ==============================
FIREBASE_OK = False
FIREBASE_ERR = ""


def init_firebase():
    global FIREBASE_OK, FIREBASE_ERR

    if firebase_admin._apps:
        return

    try:
        if not FIREBASE_CRED_JSON:
            raise Exception("Missing FIREBASE_SERVICE_ACCOUNT_JSON")

        cred_dict = json.loads(FIREBASE_CRED_JSON)
        cred = credentials.Certificate(cred_dict)

        firebase_admin.initialize_app(cred, {
            "databaseURL": DATABASE_URL
        })

        FIREBASE_OK = True
        print("Firebase init OK")

    except Exception as e:
        FIREBASE_OK = False
        FIREBASE_ERR = str(e)
        print("Firebase init FAILED:", FIREBASE_ERR)


init_firebase()


def firebase_required():
    if not FIREBASE_OK:
        return jsonify({
            "ok": False,
            "error": "Firebase init lỗi",
            "detail": FIREBASE_ERR
        }), 500
    return None


def fb_ref(path):
    return db.reference(path)


# ==============================
# AUTH
# ==============================
def issue_token(username):
    return serializer.dumps({"u": username})


def verify_token(token):
    try:
        serializer.loads(token, max_age=TOKEN_TTL_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


def get_bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def require_admin():
    token = get_bearer_token()
    return verify_token(token)


# ==============================
# AI
# ==============================
def load_model():
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


def save_model(payload):
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(payload, f)


def train_ai(rows):
    if len(rows) < 50:
        return {"ok": False, "error": "Cần tối thiểu 50 bản ghi"}

    X = []
    for r in rows:
        X.append([
            float(r["smoke"]),
            float(r["temperature"]),
            float(r["humidity"])
        ])

    X = np.array(X)
    mu = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1

    Xn = (X - mu) / std

    model = IsolationForest(n_estimators=300, contamination=0.03, random_state=42)
    model.fit(Xn)

    save_model({
        "model": model,
        "mu": mu,
        "std": std
    })

    return {"ok": True}


def ai_predict(smoke, temperature, humidity):
    payload = load_model()
    if not payload:
        return {"ok": False}

    model = payload["model"]
    mu = payload["mu"]
    std = payload["std"]

    x = np.array([[smoke, temperature, humidity]])
    xn = (x - mu) / std

    pred = model.predict(xn)[0]
    return {"anomaly": pred == -1}


# ==============================
# STATUS LOGIC
# ==============================
def compute_status(smoke, temperature, humidity):
    if smoke >= SMOKE_DANGER_MIN or temperature >= TEMP_DANGER_MIN:
        return STATUS_DANGER

    if smoke >= SMOKE_SAFE_MAX:
        return STATUS_WARN

    ai = ai_predict(smoke, temperature, humidity)
    if ai.get("anomaly"):
        return STATUS_WARN

    return STATUS_SAFE


# ==============================
# ROUTES
# ==============================
@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.post("/api/sensor")
def api_sensor():
    chk = firebase_required()
    if chk:
        return chk

    data = request.get_json() or {}

    smoke = float(data.get("smoke", 0))
    temperature = float(data.get("temperature", 0))
    humidity = float(data.get("humidity", 0))
    timestamp = int(time.time())

    status = compute_status(smoke, temperature, humidity)

    fb_ref("current").set({
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": timestamp,
        "status": status
    })

    fb_ref("history").push({
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": timestamp,
        "status": status
    })

    return jsonify({"ok": True})


@app.get("/api/current")
def api_current():
    chk = firebase_required()
    if chk:
        return chk

    cur = fb_ref("current").get() or {}
    ts = int(cur.get("timestamp", 0))
    now = int(time.time())

    online = ts and (now - ts) <= ONLINE_WINDOW_SEC

    return jsonify({
        "ok": True,
        "smoke": cur.get("smoke"),
        "temperature": cur.get("temperature"),
        "humidity": cur.get("humidity"),
        "timestamp": ts,
        "online": online,
        "status": cur.get("status", STATUS_SAFE)
    })


@app.get("/api/history")
def api_history():
    chk = firebase_required()
    if chk:
        return chk

    limit = int(request.args.get("limit", 20))
    data = fb_ref("history").order_by_key().limit_to_last(limit).get() or {}

    items = []
    for k in sorted(data.keys()):
        items.append(data[k])

    return jsonify({"ok": True, "items": items})


@app.post("/api/login")
def api_login():
    data = request.get_json() or {}
    if data.get("username") != ADMIN_USER or data.get("password") != ADMIN_PASS:
        return jsonify({"ok": False}), 401

    token = issue_token(data["username"])
    return jsonify({"ok": True, "token": token})


@app.post("/api/admin/train_ai")
def api_train_ai():
    if not require_admin():
        return jsonify({"ok": False}), 401

    rows = fb_ref("history").get() or {}
    rows = list(rows.values())

    return jsonify(train_ai(rows))


@app.get("/api/admin/export_excel")
def api_export_excel():
    if not require_admin():
        return jsonify({"ok": False}), 401

    rows = fb_ref("history").get() or {}
    rows = list(rows.values())

    wb = Workbook()
    ws = wb.active
    ws.append(["timestamp", "smoke", "temperature", "humidity", "status"])

    for r in rows:
        ws.append([
            r["timestamp"],
            r["smoke"],
            r["temperature"],
            r["humidity"],
            r["status"]
        ])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)

    return send_file(bio, as_attachment=True, download_name="iot_history.xlsx")


@app.post("/api/admin/delete_history")
def api_delete_history():
    if not require_admin():
        return jsonify({"ok": False}), 401

    fb_ref("history").delete()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

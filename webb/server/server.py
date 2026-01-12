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
# PATHS
# ==============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))
MODEL_PATH = os.path.join(BASE_DIR, "model.pkl")

# ==============================
# ENV CONFIG
# ==============================
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "https://baochay-cad24-default-rtdb.asia-southeast1.firebasedatabase.app"
)

# Render Environment Variable: FIREBASE_SERVICE_ACCOUNT_JSON
FIREBASE_CRED_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")

# Optional: lock device post by header X-Device-Key
DEVICE_KEY = os.getenv("DEVICE_KEY", "")

SECRET_KEY = os.getenv("IOT_SECRET_KEY", "iot_secret_key_change_me")
ADMIN_USER = os.getenv("IOT_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("IOT_ADMIN_PASS", "admin123")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))

ONLINE_WINDOW_SEC = 20

# ==============================
# SMOKE THRESHOLDS
# Safe: < 400
# Warn: 400 - 700
# Danger: > 700
# ==============================
SMOKE_SAFE_MAX = 400.0
SMOKE_WARN_MIN = 400.0
SMOKE_DANGER_GT = 700.0

TEMP_DANGER_MIN = 55.0

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
        FIREBASE_OK = True
        return

    try:
        if not FIREBASE_CRED_JSON:
            raise Exception("Missing FIREBASE_SERVICE_ACCOUNT_JSON")

        cred_dict = json.loads(FIREBASE_CRED_JSON)
        cred = credentials.Certificate(cred_dict)

        firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

        FIREBASE_OK = True
        FIREBASE_ERR = ""
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


def fb_ref(path: str):
    return db.reference(path)


def require_device_key():
    """
    Nếu DEVICE_KEY có set trong Render env
    ESP32 phải gửi header: X-Device-Key: <DEVICE_KEY>
    Nếu không set thì cho qua
    """
    if not DEVICE_KEY:
        return True
    got = request.headers.get("X-Device-Key", "")
    return got == DEVICE_KEY


# ==============================
# AUTH
# ==============================
def issue_token(username: str) -> str:
    return serializer.dumps({"u": username})


def verify_token(token: str) -> bool:
    try:
        serializer.loads(token, max_age=TOKEN_TTL_SECONDS)
        return True
    except (BadSignature, SignatureExpired):
        return False


def get_bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def require_admin() -> bool:
    token = get_bearer_token()
    return bool(token) and verify_token(token)


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
        try:
            X.append([
                float(r.get("smoke", 0)),
                float(r.get("temperature", 0)),
                float(r.get("humidity", 0)),
            ])
        except Exception:
            continue

    if len(X) < 50:
        return {"ok": False, "error": "Dữ liệu lỗi quá nhiều, còn dưới 50 bản ghi hợp lệ"}

    X = np.array(X)
    mu = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1

    Xn = (X - mu) / std

    model = IsolationForest(
        n_estimators=300,
        contamination=0.03,
        random_state=42
    )
    model.fit(Xn)

    save_model({
        "model": model,
        "mu": mu,
        "std": std
    })

    return {"ok": True}


def ai_predict(smoke: float, temperature: float, humidity: float):
    payload = load_model()
    if not payload:
        return {"ok": False}

    model = payload["model"]
    mu = payload["mu"]
    std = payload["std"]

    x = np.array([[smoke, temperature, humidity]])
    xn = (x - mu) / std

    pred = model.predict(xn)[0]
    return {"ok": True, "anomaly": pred == -1}


# ==============================
# HISTORY HELPERS
# ==============================
def fetch_history_items(limit: int | None = None):
    """
    Không dùng order_by_child("timestamp") để khỏi bị Firebase bắt index rules
    Lấy theo key (push id) rồi sort timestamp tại server
    """
    ref = fb_ref("history")

    if limit is not None:
        data = ref.order_by_key().limit_to_last(int(limit)).get() or {}
    else:
        data = ref.get() or {}

    if not isinstance(data, dict):
        return []

    items = list(data.values())
    items.sort(key=lambda x: int(x.get("timestamp", 0) or 0))  # ASC
    return items


# ==============================
# STATUS LOGIC
# ==============================
def compute_status(smoke: float, temperature: float, humidity: float) -> str:
    if smoke > SMOKE_DANGER_GT or temperature >= TEMP_DANGER_MIN:
        return STATUS_DANGER

    if smoke >= SMOKE_WARN_MIN:
        return STATUS_WARN

    ai = ai_predict(smoke, temperature, humidity)
    if ai.get("ok") and ai.get("anomaly"):
        return STATUS_WARN

    return STATUS_SAFE


# ==============================
# ROUTES
# ==============================
@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "firebase_ok": FIREBASE_OK,
        "firebase_err": FIREBASE_ERR
    })


@app.post("/api/sensor")
def api_sensor():
    chk = firebase_required()
    if chk:
        return chk

    if not require_device_key():
        return jsonify({"ok": False, "error": "Device key invalid"}), 401

    data = request.get_json(silent=True) or {}

    try:
        smoke = float(data.get("smoke", 0))
        temperature = float(data.get("temperature", 0))
        humidity = float(data.get("humidity", 0))
    except Exception:
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

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

    ts = int(cur.get("timestamp", 0) or 0)
    now = int(time.time())
    online = bool(ts) and (now - ts) <= ONLINE_WINDOW_SEC

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
    items = fetch_history_items(limit=limit)
    return jsonify({"ok": True, "items": items})


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if username != ADMIN_USER or password != ADMIN_PASS:
        return jsonify({"ok": False, "error": "Sai tài khoản hoặc mật khẩu"}), 401

    token = issue_token(username)
    return jsonify({"ok": True, "token": token})


@app.post("/api/logout")
def api_logout():
    return jsonify({"ok": True})


@app.post("/api/admin/train_ai")
def api_train_ai():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    limit = body.get("limit")
    limit = int(limit) if limit else None

    rows = fetch_history_items(limit=limit)
    return jsonify(train_ai(rows))


@app.get("/api/admin/export_excel")
def api_export_excel():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    limit = int(request.args.get("limit", 2000))
    rows = fetch_history_items(limit=limit)

    wb = Workbook()
    ws = wb.active
    ws.title = "history"
    ws.append(["timestamp", "time", "smoke", "temperature", "humidity", "status"])

    for r in rows:
        ts = int(r.get("timestamp", 0) or 0)
        tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""
        ws.append([
            ts,
            tstr,
            r.get("smoke"),
            r.get("temperature"),
            r.get("humidity"),
            r.get("status")
        ])

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)

    return send_file(
        bio,
        as_attachment=True,
        download_name="iot_history.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.post("/api/admin/delete_history")
def api_delete_history():
    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    fb_ref("history").delete()
    fb_ref("current").delete()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

import os
import io
import time
import math
import json
import pickle

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import numpy as np
from sklearn.ensemble import IsolationForest
from openpyxl import Workbook


# =========================
# ĐƯỜNG DẪN THEO ĐÚNG CẤU TRÚC REPO CỦA BẠN
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          # webb/server
WEB_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))        # webb

MODEL_PATH = os.path.join(BASE_DIR, "model.pkl")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "https://baochay-cad24-default-rtdb.asia-southeast1.firebasedatabase.app"
)

FIREBASE_CRED_PATH = os.getenv("FIREBASE_CRED_PATH", os.path.join(BASE_DIR, "serviceAccountKey.json"))
FIREBASE_CRED_JSON = os.getenv("FIREBASE_CRED_JSON", "")

SECRET_KEY = os.getenv("IOT_SECRET_KEY", "change_me_to_a_long_random_secret")
ADMIN_USER = os.getenv("IOT_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("IOT_ADMIN_PASS", "admin123")
TOKEN_TTL_SECONDS = int(os.getenv("TOKEN_TTL_SECONDS", "3600"))

ONLINE_WINDOW_SEC = int(os.getenv("ONLINE_WINDOW_SEC", "20"))

# Ngưỡng cứng
SMOKE_SAFE_MAX = float(os.getenv("SMOKE_SAFE_MAX", "300"))
SMOKE_DANGER_MIN = float(os.getenv("SMOKE_DANGER_MIN", "700"))
TEMP_DANGER_MIN = float(os.getenv("TEMP_DANGER_MIN", "55"))

# AI
FEATURES = ["smoke", "temperature", "humidity"]
AI_SCORE_WARN = float(os.getenv("AI_SCORE_WARN", "0.65"))

# Chuỗi trạng thái đúng UI của bạn đang dùng
STATUS_SAFE = "AN TOÀN"
STATUS_WARN = "CẢNH BÁO"
STATUS_DANGER = "NGUY HIỂM"

# Firebase path
FB_PATH_CURRENT = "current"
FB_PATH_HISTORY = "history"

# =========================
# FLASK APP
# =========================
app = Flask(
    __name__,
    static_folder=WEB_DIR,
    template_folder=WEB_DIR,
    static_url_path=""
)
CORS(app)

serializer = URLSafeTimedSerializer(SECRET_KEY)


# =========================
# FIREBASE INIT
# =========================
FIREBASE_OK = False
FIREBASE_ERR = ""


def init_firebase():
    global FIREBASE_OK, FIREBASE_ERR

    if firebase_admin._apps:
        FIREBASE_OK = True
        return

    try:
        if FIREBASE_CRED_JSON.strip():
            cred_dict = json.loads(FIREBASE_CRED_JSON)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})
        elif os.path.exists(FIREBASE_CRED_PATH):
            cred = credentials.Certificate(FIREBASE_CRED_PATH)
            firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})
        else:
            firebase_admin.initialize_app(options={"databaseURL": DATABASE_URL})

        FIREBASE_OK = True
    except Exception as e:
        FIREBASE_OK = False
        FIREBASE_ERR = str(e)


init_firebase()


def firebase_required():
    if not FIREBASE_OK:
        return jsonify({
            "ok": False,
            "error": "Firebase chưa init được",
            "detail": FIREBASE_ERR,
            "hint": "Đặt serviceAccountKey.json cạnh server.py hoặc set FIREBASE_CRED_PATH hoặc FIREBASE_CRED_JSON"
        }), 500
    return None


def fb_ref(path: str):
    return db.reference(path)


# =========================
# AUTH TOKEN
# =========================
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
    return verify_token(token)


# =========================
# AI MODEL
# =========================
def load_model_payload():
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def save_model_payload(payload):
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(payload, f)


def train_ai(rows):
    if not rows or len(rows) < 50:
        return {"ok": False, "error": "Cần tối thiểu 50 bản ghi để huấn luyện"}

    X = []
    for r in rows:
        try:
            X.append([
                float(r.get("smoke", 0)),
                float(r.get("temperature", 0)),
                float(r.get("humidity", 0)),
            ])
        except Exception:
            pass

    if len(X) < 50:
        return {"ok": False, "error": "Dữ liệu không đủ sau khi làm sạch"}

    X = np.array(X, dtype=float)
    mu = X.mean(axis=0)
    std = X.std(axis=0)
    std = np.where(std < 1e-6, 1.0, std)
    Xn = (X - mu) / std

    model = IsolationForest(
        n_estimators=300,
        contamination=0.03,
        random_state=42
    )
    model.fit(Xn)

    # Ngưỡng mềm gợi ý từ dữ liệu bình thường
    preds = model.predict(Xn)
    normal = X[preds == 1]
    if normal.shape[0] < 20:
        normal = X

    suggested = {
        "smoke_soft": float(np.percentile(normal[:, 0], 97)),
        "temp_soft": float(np.percentile(normal[:, 1], 97)),
        "hum_soft": float(np.percentile(normal[:, 2], 97)),
    }

    save_model_payload({
        "model": model,
        "mu": mu,
        "std": std,
        "suggested": suggested
    })

    return {"ok": True, "token": "trained", "suggested": suggested}


def ai_predict(smoke, temperature, humidity):
    payload = load_model_payload()
    if payload is None:
        return {"ok": False, "error": "Chưa có AI, hãy bấm Huấn luyện AI"}

    model = payload["model"]
    mu = payload["mu"]
    std = payload["std"]

    x = np.array([[float(smoke), float(temperature), float(humidity)]], dtype=float)
    xn = (x - mu) / std

    pred = int(model.predict(xn)[0])
    score_raw = float(-model.score_samples(xn)[0])
    score = float(1.0 - math.exp(-score_raw))

    return {
        "ok": True,
        "anomaly": bool(pred == -1),
        "score": score,
        "suggested": payload.get("suggested", {})
    }


# =========================
# LOGIC TRẠNG THÁI KHỚP UI
# =========================
def compute_status(smoke, temperature, humidity):
    # Luật cứng ưu tiên an toàn
    if (smoke is not None and float(smoke) >= SMOKE_DANGER_MIN) or (temperature is not None and float(temperature) >= TEMP_DANGER_MIN):
        return STATUS_DANGER

    if smoke is not None and float(smoke) >= SMOKE_SAFE_MAX:
        return STATUS_WARN

    # AI hỗ trợ cảnh báo sớm nếu có model
    ai = ai_predict(smoke, temperature, humidity)
    if ai.get("ok"):
        if ai.get("anomaly") or float(ai.get("score", 0)) >= AI_SCORE_WARN:
            return STATUS_WARN

    return STATUS_SAFE


# =========================
# PARSE PAYLOAD ESP32
# =========================
def parse_sensor_payload(data: dict):
    # ESP32 của bạn đang gửi: smoke, temperature, humidity
    smoke = float(data.get("smoke", 0))
    temperature = float(data.get("temperature", data.get("temp", 0)))
    humidity = float(data.get("humidity", data.get("hum", 0)))

    # timestamp server tự tạo theo giây epoch để UI formatTimeFromTs dùng được
    timestamp = int(time.time())

    return smoke, temperature, humidity, timestamp


# =========================
# FIREBASE READ WRITE
# =========================
def write_current(smoke, temperature, humidity, timestamp, status):
    fb_ref(FB_PATH_CURRENT).set({
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": timestamp,
        "status": status
    })


def push_history(smoke, temperature, humidity, timestamp, status):
    fb_ref(FB_PATH_HISTORY).push({
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": timestamp,
        "status": status
    })


def read_current():
    return fb_ref(FB_PATH_CURRENT).get() or {}


def read_history(limit: int):
    limit = max(1, min(int(limit), 5000))
    data = fb_ref(FB_PATH_HISTORY).order_by_key().limit_to_last(limit).get()
    if not data:
        return []

    keys = sorted(data.keys())
    items = []
    for k in keys:
        r = data.get(k, {})
        try:
            items.append({
                "smoke": float(r.get("smoke", 0)),
                "temperature": float(r.get("temperature", 0)),
                "humidity": float(r.get("humidity", 0)),
                "timestamp": int(r.get("timestamp", 0)),
                "status": str(r.get("status", STATUS_SAFE)),
            })
        except Exception:
            pass
    return items


def delete_history():
    fb_ref(FB_PATH_HISTORY).delete()


# =========================
# ROUTES
# =========================
@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/sensor")
def api_sensor():
    chk = firebase_required()
    if chk:
        return chk

    data = request.get_json(silent=True) or {}
    try:
        smoke, temperature, humidity, timestamp = parse_sensor_payload(data)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid payload"}), 400

    status = compute_status(smoke, temperature, humidity)

    write_current(smoke, temperature, humidity, timestamp, status)
    push_history(smoke, temperature, humidity, timestamp, status)

    return jsonify({"ok": True, "status": status, "timestamp": timestamp})


@app.get("/api/current")
def api_current():
    chk = firebase_required()
    if chk:
        return chk

    cur = read_current()
    now = int(time.time())

    try:
        ts = int(cur.get("timestamp", 0))
    except Exception:
        ts = 0

    online = bool(ts and (now - ts) <= ONLINE_WINDOW_SEC)

    # Nếu chưa có data, vẫn trả đúng key để app.js không văng lỗi
    if not cur or not ts:
        return jsonify({
            "ok": True,
            "smoke": None,
            "temperature": None,
            "humidity": None,
            "timestamp": 0,
            "online": False,
            "status": STATUS_SAFE
        })

    smoke = float(cur.get("smoke", 0))
    temperature = float(cur.get("temperature", 0))
    humidity = float(cur.get("humidity", 0))

    status = str(cur.get("status", ""))
    if not status:
        status = compute_status(smoke, temperature, humidity)

    return jsonify({
        "ok": True,
        "smoke": smoke,
        "temperature": temperature,
        "humidity": humidity,
        "timestamp": ts,
        "online": online,
        "status": status
    })


@app.get("/api/history")
def api_history():
    chk = firebase_required()
    if chk:
        return chk

    limit = request.args.get("limit", "20")
    try:
        limit = int(limit)
    except Exception:
        limit = 20

    items = read_history(limit)

    return jsonify({
        "ok": True,
        "items": items
    })


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if username != ADMIN_USER or password != ADMIN_PASS:
        return jsonify({"ok": False, "error": "Sai tài khoản hoặc mật khẩu"}), 401

    token = issue_token(username)
    return jsonify({"ok": True, "token": token})


@app.post("/api/logout")
def api_logout():
    return jsonify({"ok": True})


@app.post("/api/admin/train_ai")
def api_train_ai():
    chk = firebase_required()
    if chk:
        return chk

    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    limit = data.get("limit", 3000)
    try:
        limit = int(limit)
    except Exception:
        limit = 3000

    rows = read_history(limit)
    res = train_ai(rows)
    code = 200 if res.get("ok") else 400
    return jsonify(res), code


@app.get("/api/admin/export_excel")
def api_export_excel():
    chk = firebase_required()
    if chk:
        return chk

    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    limit = request.args.get("limit", "500")
    try:
        limit = int(limit)
    except Exception:
        limit = 500

    rows = read_history(limit)

    wb = Workbook()
    ws = wb.active
    ws.title = "history"

    ws.append(["timestamp", "smoke", "temperature", "humidity", "status"])
    for r in rows:
        ws.append([
            int(r.get("timestamp", 0)),
            float(r.get("smoke", 0)),
            float(r.get("temperature", 0)),
            float(r.get("humidity", 0)),
            str(r.get("status", STATUS_SAFE))
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
    chk = firebase_required()
    if chk:
        return chk

    if not require_admin():
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    delete_history()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)

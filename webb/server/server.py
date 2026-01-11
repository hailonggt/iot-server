import os
import io
import json
import time
import math
import random
import secrets
import functools

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

import firebase_admin
from firebase_admin import credentials, db

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

"""
  Flask server cloud ready

  Điểm chỉnh
  1) Firebase key có thể lấy từ biến môi trường FIREBASE_SERVICE_ACCOUNT_JSON
     Nếu không có thì đọc file serviceAccountKey.json đặt cùng folder
  2) Thêm DEVICE_KEY để khóa endpoint /api/sensor nếu muốn
  3) Thời gian hiển thị chuyển sang web tự format theo timezone máy người xem
  4) Tối ưu poll: API nhanh gọn, không nhồi nhiều dữ liệu thừa
"""

app = Flask(__name__)
@app.route("/")
def home():
    return "IoT Server is running"
cors_origins = os.getenv("CORS_ORIGINS", "*")
CORS(app, resources={r"/api/*": {"origins": cors_origins}})

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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


def _now_ts() -> int:
    return int(time.time())


def _issue_token() -> str:
    token = secrets.token_urlsafe(32)
    TOKENS[token] = _now_ts() + TOKEN_TTL_SEC
    return token


def _clean_expired_tokens():
    now = _now_ts()
    expired = [t for t, exp in TOKENS.items() if exp <= now]
    for t in expired:
        TOKENS.pop(t, None)


def _get_bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return ""
    parts = auth.split()
    if len(parts) != 2:
        return ""
    if parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def auth_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        _clean_expired_tokens()
        token = _get_bearer_token()
        if not token:
            return jsonify({"ok": False, "error": "Missing token"}), 401
        exp = TOKENS.get(token)
        if not exp:
            return jsonify({"ok": False, "error": "Invalid token"}), 401
        if exp <= _now_ts():
            TOKENS.pop(token, None)
            return jsonify({"ok": False, "error": "Token expired"}), 401
        return fn(*args, **kwargs)
    return wrapper


def fb_ref(path: str):
    return db.reference(path)


def init_firebase():
    if firebase_admin._apps:
        return

    env_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    if env_json:
        cred_dict = json.loads(env_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})
        return

    key_path = r"D:\keys\serviceAccountKey.json"
    cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})



def bootstrap_ai_data(n=400):
    data = []
    for _ in range(n):
        ventilation = random.uniform(0.3, 0.9)

        temperature = random.gauss(30, 3)
        humidity = random.gauss(55, 10)
        humidity = max(20, min(90, humidity))

        smoke = random.gauss(150, 50)
        smoke += (1 - ventilation) * 400
        smoke += (temperature - 30) * 8
        smoke = max(20, int(smoke))

        data.append({
            "smoke": smoke,
            "temperature": round(temperature, 1),
            "humidity": round(humidity, 1),
            "ventilation": round(ventilation, 2),
            "timestamp": _now_ts()
        })
    return data


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

    smokes = [s.get("smoke", 0) for s in AI_SAMPLES]
    temps = [s.get("temperature", 0.0) for s in AI_SAMPLES]
    hums = [s.get("humidity", 0.0) for s in AI_SAMPLES]

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


def build_payload_from_request(data: dict) -> dict:
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


def compute_online(last_ts: int, timeout_sec: int = 25) -> bool:
    if not last_ts:
        return False
    return (_now_ts() - int(last_ts)) <= timeout_sec


def require_device_key_if_set():
    if not DEVICE_KEY:
        return None
    got = request.headers.get("X-Device-Key", "").strip()
    if not got or not secrets.compare_digest(got, DEVICE_KEY):
        return jsonify({"ok": False, "error": "Invalid device key"}), 401
    return None


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "server_time": _now_ts()})


@app.post("/api/sensor")
def post_sensor():
    reject = require_device_key_if_set()
    if reject is not None:
        return reject

    data = request.get_json(silent=True) or {}
    sample = build_payload_from_request(data)

    status, level = ai_evaluate(sample)
    payload = {**sample, "status": status, "level": level}

    AI_SAMPLES.append(sample)
    if len(AI_SAMPLES) > MAX_AI_SAMPLES:
        AI_SAMPLES.pop(0)

    try:
        fb_ref("sensor/current").set(payload)
        fb_ref("sensor/history").push(payload)
    except Exception as e:
        return jsonify({"ok": False, "error": "Firebase write failed", "detail": str(e)}), 500

    return jsonify({"ok": True, "status": status, "level": level})


@app.get("/api/current")
def get_current():
    cur = fb_ref("sensor/current").get() or {}
    last_ts = int(cur.get("timestamp", 0) or 0)

    cur["online"] = compute_online(last_ts)
    return jsonify(cur)


@app.get("/api/history")
def get_history():
    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20
    limit = max(1, min(200, limit))

    snap = (
        fb_ref("sensor/history")
        .order_by_child("timestamp")
        .limit_to_last(limit)
        .get()
        or {}
    )

    items = []
    for key, val in snap.items():
        if isinstance(val, dict):
            val["_key"] = key
            items.append(val)

    items.sort(key=lambda x: int(x.get("timestamp", 0) or 0), reverse=True)
    return jsonify({"ok": True, "items": items})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    if secrets.compare_digest(username, ADMIN_USER) and secrets.compare_digest(password, ADMIN_PASS):
        token = _issue_token()
        return jsonify({"ok": True, "token": token, "expires_in": TOKEN_TTL_SEC})

    return jsonify({"ok": False, "error": "Sai tài khoản hoặc mật khẩu"}), 401


@app.post("/api/logout")
@auth_required
def logout():
    token = _get_bearer_token()
    TOKENS.pop(token, None)
    return jsonify({"ok": True})


def _excel_style_header(ws, headers):
    header_fill = PatternFill("solid", fgColor="1F2937")
    header_font = Font(color="FFFFFF", bold=True)
    center = Alignment(horizontal="center", vertical="center")

    ws.append(headers)
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center

    ws.freeze_panes = "A2"


def _excel_autosize(ws, max_width=40):
    for col_cells in ws.columns:
        length = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            val = "" if cell.value is None else str(cell.value)
            length = max(length, len(val))
        ws.column_dimensions[col_letter].width = min(max_width, length + 2)


@app.get("/api/admin/export_excel")
@auth_required
def export_excel():
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    limit = max(1, min(5000, limit))

    snap = (
        fb_ref("sensor/history")
        .order_by_child("timestamp")
        .limit_to_last(limit)
        .get()
        or {}
    )

    items = []
    for key, val in snap.items():
        if isinstance(val, dict):
            val["_key"] = key
            items.append(val)

    items.sort(key=lambda x: int(x.get("timestamp", 0) or 0), reverse=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "History"

    headers = [
        "Timestamp",
        "Khói MQ2",
        "Nhiệt độ (C)",
        "Độ ẩm (%)",
        "Độ thoáng khí",
        "Đánh giá",
        "Level",
        "Firebase Key",
    ]
    _excel_style_header(ws, headers)

    for it in items:
        ts = int(it.get("timestamp", 0) or 0)
        ws.append([
            ts,
            it.get("smoke", ""),
            it.get("temperature", ""),
            it.get("humidity", ""),
            it.get("ventilation", ""),
            it.get("status", ""),
            it.get("level", ""),
            it.get("_key", ""),
        ])

    _excel_autosize(ws)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"iot_history_{_now_ts()}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.post("/api/admin/delete_history")
@auth_required
def delete_history():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip()

    history_ref = fb_ref("sensor/history")

    if mode == "all":
        history_ref.set(None)
        return jsonify({"ok": True, "deleted": "all"})

    if mode == "older_than":
        ts_cut = int(data.get("timestamp", 0) or 0)
        if ts_cut <= 0:
            return jsonify({"ok": False, "error": "timestamp không hợp lệ"}), 400

        snap = (
            history_ref
            .order_by_child("timestamp")
            .end_at(ts_cut)
            .get()
            or {}
        )

        count = 0
        for key in list(snap.keys()):
            history_ref.child(key).delete()
            count += 1

        return jsonify({"ok": True, "deleted": count, "mode": "older_than", "timestamp": ts_cut})

    return jsonify({"ok": False, "error": "mode không hợp lệ"}), 400


@app.post("/api/admin/train_ai")
@auth_required
def train_ai():
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit", 1500))
    except Exception:
        limit = 1500

    limit = max(50, min(MAX_AI_SAMPLES, limit))

    snap = (
        fb_ref("sensor/history")
        .order_by_child("timestamp")
        .limit_to_last(limit)
        .get()
        or {}
    )

    items = []
    for _, val in snap.items():
        if isinstance(val, dict):
            items.append({
                "smoke": int(val.get("smoke", 0) or 0),
                "temperature": float(val.get("temperature", 0) or 0),
                "humidity": float(val.get("humidity", 0) or 0),
                "ventilation": float(val.get("ventilation", DEFAULT_VENT) or DEFAULT_VENT),
                "timestamp": int(val.get("timestamp", 0) or 0),
            })

    if len(items) < 80:
        items.extend(bootstrap_ai_data(n=200))

    AI_SAMPLES.clear()
    AI_SAMPLES.extend(items[-MAX_AI_SAMPLES:])

    return jsonify({"ok": True, "trained_samples": len(AI_SAMPLES)})


def main():
    init_firebase()

    if not AI_SAMPLES:
        AI_SAMPLES.extend(bootstrap_ai_data())

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()

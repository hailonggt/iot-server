#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <DHT.h>
#include <IRremote.hpp>
#include <WiFiClientSecure.h>

/*
  ESP32 firmware bản ổn định cho demo internet

  Điểm chính
  1) Vẫn giữ ngưỡng cứng trên ESP32 để phản ứng tức thời kể cả mất mạng
  2) Gửi dữ liệu lên server qua HTTPS nhưng dùng WiFiClientSecure setInsecure để khỏi vướng cert
  3) Giảm tần suất POST để tránh timeout
  4) Nếu DHT lỗi vẫn quyết định level dựa trên khói
*/

// =========================
// 1) WIFI CONFIG
// =========================
const char* ssid     = "iphone";
const char* password = "tun123456";

// =========================
// 2) SERVER CONFIG
// =========================
// Nếu bạn deploy Render domain như dưới là đúng
// HTTPS sẽ ổn định hơn HTTP vì Render thường redirect, ESP32 dễ kẹt redirect
const char* serverUrl = "https://iot-baochay.onrender.com/api/sensor";

// Nếu bạn muốn khóa thiết bị, đặt DEVICE_KEY giống trong server env DEVICE_KEY
// Nếu không dùng, để rỗng ""
const char* DEVICE_KEY = "";

// =========================
// 3) MQ2 CONFIG
// =========================
constexpr int MQ2_AO_PIN = 34;

// =========================
// 4) DHT11 CONFIG
// =========================
#define DHTPIN 27
#define DHTTYPE DHT11
DHT dht(DHTPIN, DHTTYPE);

// =========================
// 5) IR SENDER CONFIG
// =========================
#define IR_SEND_PIN 18

static const uint16_t IR_ADDR = 0xEF00;
static const uint8_t  IR_CMD_OFF    = 0x02;
static const uint8_t  IR_CMD_ON     = 0x03;
static const uint8_t  IR_CMD_RED    = 0x04;
static const uint8_t  IR_CMD_GREEN  = 0x05;
static const uint8_t  IR_CMD_YELLOW = 0x08;

// =========================
// 6) NGƯỠNG CỨNG TRÊN ESP32
// =========================
const int   SMOKE_SAFE_MAX = 300;
const int   SMOKE_WARN_MAX = 700;
const float TEMP_DANGER    = 55.0f;
const int   HYST           = 20;

// =========================
// 7) CHU KỲ CHẠY
// =========================
// SENSOR đọc nhanh để đổi đèn kịp
// POST gửi thưa để tránh timeout khi lên internet
const uint32_t SENSOR_PERIOD_MS = 2000;
const uint32_t POST_PERIOD_MS   = 10000;
const uint32_t WIFI_CHECK_MS    = 12000;

// =========================
// 8) TRẠNG THÁI
// =========================
enum Level : uint8_t { SAFE = 0, WARN = 1, DANGER = 2 };
Level lastLevel = SAFE;

uint8_t  lastIrCmdSent = 0;
uint32_t lastIrSendMs  = 0;

int   lastSmoke = 0;
float lastTemp  = NAN;
float lastHum   = NAN;

uint32_t tLastSensor = 0;
uint32_t tLastPost   = 0;
uint32_t tLastWifi   = 0;

// =========================
// 9) ĐỌC MQ2 CÓ LỌC NHIỄU
// =========================
int readMQ2Filtered() {
  long sum = 0;
  const int samples = 20;

  for (int i = 0; i < samples; i++) {
    sum += analogRead(MQ2_AO_PIN);
    delay(5);
  }
  return (int)(sum / samples);
}

// =========================
// 10) GỬI IR NEC
// =========================
void sendIR_NEC(uint16_t addr, uint8_t cmd) {
  // Chống gửi quá dày
  if (millis() - lastIrSendMs < 350) return;

  // Trùng lệnh vừa gửi thì bỏ qua
  if (cmd == lastIrCmdSent) return;

  IrSender.sendNEC(addr, cmd, 0);

  lastIrCmdSent = cmd;
  lastIrSendMs  = millis();

  Serial.print("IR SEND addr=0x");
  Serial.print(addr, HEX);
  Serial.print(" cmd=0x");
  Serial.println(cmd, HEX);
}

// =========================
// 11) QUYẾT ĐỊNH LEVEL
// =========================
Level decideLevel(int smoke, float temp, Level cur) {
  // Nếu nhiệt hợp lệ và quá cao thì ưu tiên nguy hiểm
  if (!isnan(temp) && temp >= TEMP_DANGER) return DANGER;

  if (cur == DANGER) {
    if (smoke < (SMOKE_WARN_MAX - HYST)) return WARN;
    return DANGER;
  }

  if (cur == WARN) {
    if (smoke >= (SMOKE_WARN_MAX + HYST)) return DANGER;
    if (smoke < (SMOKE_SAFE_MAX - HYST)) return SAFE;
    return WARN;
  }

  if (smoke >= (SMOKE_WARN_MAX + HYST)) return DANGER;
  if (smoke >= (SMOKE_SAFE_MAX + HYST)) return WARN;
  return SAFE;
}

uint8_t levelToIrCmd(Level lv) {
  if (lv == SAFE) return IR_CMD_GREEN;
  if (lv == WARN) return IR_CMD_YELLOW;
  return IR_CMD_RED;
}

// =========================
// 12) WIFI CONNECT
// =========================
bool connectWiFi(uint32_t timeoutMs = 15000) {
  if (WiFi.status() == WL_CONNECTED) return true;

  Serial.println("Connecting WiFi...");
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - start) < timeoutMs) {
    delay(300);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi OK IP: ");
    Serial.println(WiFi.localIP());
    return true;
  }

  Serial.println("WiFi timeout");
  return false;
}

// =========================
// 13) POST JSON LÊN SERVER
// =========================
void postToServer(int smoke, float temp, float hum) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Skip POST WiFi down");
    return;
  }

  // HTTPS client
  WiFiClientSecure client;
  client.setInsecure();          // bỏ kiểm tra cert để khỏi kẹt
  client.setTimeout(15000);      // ms

  HTTPClient http;
  http.setTimeout(15000);        // ms
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!http.begin(client, serverUrl)) {
    Serial.println("HTTP begin failed");
    return;
  }

  http.addHeader("Content-Type", "application/json");

  // Nếu dùng khóa thiết bị thì gửi header
  if (DEVICE_KEY && DEVICE_KEY[0] != '\0') {
    http.addHeader("X-Device-Key", DEVICE_KEY);
  }

  // JSON gọn nhẹ
  String body = "{";
  body += "\"smoke\":" + String(smoke) + ",";
  body += "\"temperature\":" + String(temp, 1) + ",";
  body += "\"humidity\":" + String(hum, 1);
  body += "}";

  int httpCode = http.POST(body);

  Serial.print("POST smoke=");
  Serial.print(smoke);
  Serial.print(" temp=");
  Serial.print(temp, 1);
  Serial.print(" hum=");
  Serial.print(hum, 1);
  Serial.print(" http=");
  Serial.println(httpCode);

  if (httpCode < 0) {
    Serial.print("HTTP err: ");
    Serial.println(http.errorToString(httpCode).c_str());
  }

  http.end();
}

// =========================
// 14) SETUP
// =========================
void setup() {
  Serial.begin(115200);
  delay(600);

  Serial.println("ESP32 start");

  dht.begin();

  // IRremote 4.x
  IrSender.begin(IR_SEND_PIN);

  // Bật thiết bị IR trước
  sendIR_NEC(IR_ADDR, IR_CMD_ON);
  delay(300);

  lastLevel = SAFE;
  sendIR_NEC(IR_ADDR, IR_CMD_GREEN);

  connectWiFi();

  tLastSensor = millis();
  tLastPost   = millis();
  tLastWifi   = millis();
}

// =========================
// 15) LOOP
// =========================
void loop() {
  uint32_t now = millis();

  // A) Kiểm tra WiFi theo chu kỳ
  if (now - tLastWifi >= WIFI_CHECK_MS) {
    tLastWifi = now;
    if (WiFi.status() != WL_CONNECTED) connectWiFi();
  }

  // B) Đọc cảm biến và đổi đèn
  if (now - tLastSensor >= SENSOR_PERIOD_MS) {
    tLastSensor = now;

    lastSmoke = readMQ2Filtered();

    float t = dht.readTemperature();
    float h = dht.readHumidity();

    // Lưu giá trị nếu hợp lệ
    if (!isnan(t)) lastTemp = t;
    if (!isnan(h)) lastHum  = h;

    if (isnan(t) || isnan(h)) {
      Serial.println("DHT read fail keep last valid");
    }

    // Quan trọng: vẫn quyết định level dựa khói, temp nếu có
    Level nextLevel = decideLevel(lastSmoke, lastTemp, lastLevel);
    if (nextLevel != lastLevel) {
      lastLevel = nextLevel;
      sendIR_NEC(IR_ADDR, levelToIrCmd(lastLevel));
    }
  }

  // C) POST theo chu kỳ thưa hơn
  if (now - tLastPost >= POST_PERIOD_MS) {
    tLastPost = now;

    // Nếu DHT chưa có giá trị hợp lệ thì vẫn gửi khói, còn temp hum gửi 0
    float sendT = isnan(lastTemp) ? 0.0f : lastTemp;
    float sendH = isnan(lastHum)  ? 0.0f : lastHum;

    postToServer(lastSmoke, sendT, sendH);
  }
}

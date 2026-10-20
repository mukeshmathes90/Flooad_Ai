/*
 * flood_monitor.ino
 * ---------------------------------------------------------------------------
 * Flood Prediction & Early Warning System - ESP32 sensor node firmware.
 *
 * Flow:
 *   HC-SR04 + rain sensor + analogue water sensor + PIR
 *        -> ESP32 Wi-Fi (mobile hotspot)
 *        -> HTTP POST to the computer running FastAPI:
 *             http://<COMPUTER_HOTSPOT_IP>:8000/api/sensors/data
 *
 * Component / pin map (matches the existing prototype, do not change casually):
 *   Rain analogue input .......... GPIO 35  (ADC1, input only)
 *   Water analogue input ......... GPIO 34  (ADC1, input only)
 *   Ultrasonic TRIG .............. GPIO 5
 *   Ultrasonic ECHO .............. GPIO 18  (*LEVEL SHIFT / DIVIDER REQUIRED*)
 *   PIR .......................... GPIO 27
 *   Green LED .................... GPIO 14
 *   Yellow LED ................... GPIO 12
 *   Red LED ...................... GPIO 13
 *   Buzzer ....................... GPIO 25
 *   OLED I2C SDA ................. GPIO 21
 *   OLED I2C SCL ................. GPIO 22   (address 0x3C)
 *   Serial ....................... 115200
 *
 * SAFETY NOTE:
 *   The HC-SR04 ECHO pin outputs 5 V. Feeding it directly into GPIO 18 will
 *   eventually damage the ESP32. Use a level shifter or a 1k/2k voltage divider
 *   so ECHO never exceeds 3.3 V.
 *
 * LIBRARIES (Arduino IDE Library Manager):
 *   - Adafruit SSD1306
 *   - Adafruit GFX Library
 *
 * CONFIGURE Wi-Fi / backend in "wifi_config.h" (see that file).
 * Never commit your real Wi-Fi password to a public repository.
 */

#include <WiFi.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#include "wifi_config.h"   // Wi-Fi SSID/password + backend host/port + calibration

// ---------------------------------------------------------------------------
// Pull mode: the backend can also PULL from this ESP32 instead of being pushed
// to. Enter this device's IP (shown on the OLED as "Net") into the dashboard's
// "ESP32 address" box. The web server listens on port 80.
// ---------------------------------------------------------------------------
WebServer server(80);

// Forward declarations (Arduino IDE auto-generates prototypes, but keeping these
// explicit avoids ordering surprises between buildJson() and isoNow()).
String isoNow();

// --------------------------------------------------------------------------- //
// Pins
// --------------------------------------------------------------------------- //
#define PIN_RAIN_ADC      35
#define PIN_WATER_ADC     34
#define PIN_TRIG          5
#define PIN_ECHO          18
#define PIN_PIR           27
#define PIN_LED_GREEN     14
#define PIN_LED_YELLOW    12
#define PIN_LED_RED       13
#define PIN_BUZZER        25

#define OLED_SDA          21
#define OLED_SCL          22
#define OLED_ADDR         0x3C
#define SCREEN_WIDTH      128
#define SCREEN_HEIGHT     64

// --------------------------------------------------------------------------- //
// Risk thresholds (MUST match backend/backend/config.py - prototype values!)
// --------------------------------------------------------------------------- //
#define THRESHOLD_WARNING_CM   30.0
#define THRESHOLD_HIGH_CM      45.0
#define THRESHOLD_CRITICAL_CM  65.0

// Rain ADC calibration (12-bit). Relative percentage only, not mm/hour.
#define RAIN_ADC_DRY      400
#define RAIN_ADC_WET      3500

#define REPORT_INTERVAL_MS  5000UL   // send a reading every 5 s
#define SENSOR_INTERVAL_MS  500UL    // read sensors twice a second
#define HTTP_TIMEOUT_MS     2500     // do not block the loop for longer than this
#define WIFI_RETRY_MS       10000UL

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, -1);

// --------------------------------------------------------------------------- //
// State
// --------------------------------------------------------------------------- //
unsigned long lastSensorMs = 0;
unsigned long lastReportMs = 0;
unsigned long lastWifiAttemptMs = 0;

float gWaterLevel = 0.0;      // cm
float gDistance = -1.0;       // cm, -1 = invalid / timeout
int   gRainAdc = 0;
float gRainPct = 0.0;
int   gWaterAdc = 0;
int   gPir = 0;
bool  gDistanceValid = false;

String gRisk = "normal";
bool gBackendOk = false;
unsigned long gLastBackendOkMs = 0;
bool gWebServerStarted = false;

// --------------------------------------------------------------------------- //
// Setup
// --------------------------------------------------------------------------- //
void setup() {
  Serial.begin(115200);
  delay(200);

  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_LED_GREEN, OUTPUT);
  pinMode(PIN_LED_YELLOW, OUTPUT);
  pinMode(PIN_LED_RED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_TRIG, LOW);
  setIndicators("normal");
  noTone(PIN_BUZZER);

  Wire.begin(OLED_SDA, OLED_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("[OLED] init failed (check wiring/address 0x3C)");
  } else {
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println("Flood Monitor booting");
    display.println(WIFI_SSID);
    display.display();
  }

  connectWifi();
}

// --------------------------------------------------------------------------- //
// Wi-Fi
// --------------------------------------------------------------------------- //
void connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.printf("[WiFi] connecting to %s ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  lastWifiAttemptMs = millis();
}

// --------------------------------------------------------------------------- //
// Sensors
// --------------------------------------------------------------------------- //
float readDistanceCm() {
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(3);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);

  // 25 ms timeout -> ~4 m max range. 0 == no echo (never treat as 0 cm water!)
  unsigned long duration = pulseIn(PIN_ECHO, HIGH, 25000UL);
  if (duration == 0) return -1.0;
  float cm = duration * 0.0343f / 2.0f;
  if (cm < 2.0f || cm > 400.0f) return -1.0;
  return cm;
}

void readSensors() {
  gRainAdc = analogRead(PIN_RAIN_ADC);
  gWaterAdc = analogRead(PIN_WATER_ADC);
  gPir = digitalRead(PIN_PIR) ? 1 : 0;

  // Rain: calibrated RELATIVE percentage (not a physical mm/hour measurement).
  long span = RAIN_ADC_WET - RAIN_ADC_DRY;
  float pct = (float)(gRainAdc - RAIN_ADC_DRY) / (float)span * 100.0f;
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  gRainPct = pct;

  float d = readDistanceCm();
  if (d < 0) {
    gDistanceValid = false;             // keep last known level, flag invalid
    Serial.println("[HC-SR04] timeout/invalid reading");
  } else {
    gDistanceValid = true;
    gDistance = d;
    float level = (float)REFERENCE_HEIGHT_CM - d;
    if (level < 0) level = 0;
    gWaterLevel = level;
  }

  // Local risk classification (same thresholds as backend).
  if (!gDistanceValid) {
    gRisk = "unknown";
  } else if (gWaterLevel >= THRESHOLD_CRITICAL_CM) {
    gRisk = "critical";
  } else if (gWaterLevel >= THRESHOLD_HIGH_CM) {
    gRisk = "high";
  } else if (gWaterLevel >= THRESHOLD_WARNING_CM) {
    gRisk = "warning";
  } else {
    gRisk = "normal";
  }

  setIndicators(gRisk);
}

void setIndicators(const String &risk) {
  digitalWrite(PIN_LED_GREEN,  risk == "normal"   ? HIGH : LOW);
  digitalWrite(PIN_LED_YELLOW, risk == "warning"  ? HIGH : LOW);
  digitalWrite(PIN_LED_RED,    (risk == "high" || risk == "critical") ? HIGH : LOW);
  if (risk == "critical") {
    tone(PIN_BUZZER, 2200);
  } else if (risk == "high") {
    tone(PIN_BUZZER, 1500, 150);   // short chirp
  } else {
    noTone(PIN_BUZZER);
  }
}

// --------------------------------------------------------------------------- //
// Display
// --------------------------------------------------------------------------- //
void updateDisplay() {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);

  display.setCursor(0, 0);
  display.print("Water: ");
  if (gDistanceValid) { display.print(gWaterLevel, 1); display.println(" cm"); }
  else { display.println("-- (no echo)"); }

  display.setCursor(0, 12);
  display.print("Dist : ");
  if (gDistanceValid) { display.print(gDistance, 1); display.println(" cm"); }
  else { display.println("-- cm"); }

  display.setCursor(0, 24);
  display.print("Rain : ");
  display.print(gRainAdc);
  display.print(" (");
  display.print((int)gRainPct);
  display.println("%)");

  display.setCursor(0, 36);
  display.print("Risk : ");
  display.println(gRisk);

  display.setCursor(0, 48);
  display.print("Net  : ");
  display.print(WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : String("offline"));
  display.setCursor(0, 56);
  display.print("Srv  : ");
  display.print(gBackendOk ? "ok" : "no reply");
  display.display();
}

// --------------------------------------------------------------------------- //
// Sensor JSON (shared by POST mode and the pull-mode web server)
// --------------------------------------------------------------------------- //
String buildJson() {
  String json = "{";
  json += "\"device_id\":\"" DEVICE_ID "\",";
  json += "\"source\":\"esp32\",";
  json += "\"firmware\":\"flood_monitor/1.0\",";
  json += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  if (gDistanceValid) {
    json += "\"water_level_cm\":" + String(gWaterLevel, 2) + ",";
    json += "\"ultrasonic_distance_cm\":" + String(gDistance, 2) + ",";
  } else {
    // Omit invalid measurements rather than sending a false 0 cm water level.
    json += "\"ultrasonic_distance_cm\":null,";
  }
  json += "\"rainfall_reading\":" + String(gRainAdc) + ",";
  json += "\"rainfall_intensity\":" + String(gRainPct, 1) + ",";
  json += "\"water_sensor_raw\":" + String(gWaterAdc) + ",";
  json += "\"pir_motion\":" + String(gPir) + ",";
  json += "\"risk\":\"" + gRisk + "\",";
  json += "\"timestamp\":\"" + isoNow() + "\"";
  json += "}";
  return json;
}

// --------------------------------------------------------------------------- //
// Pull-mode web server handlers
// --------------------------------------------------------------------------- //
void handleReadings() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", buildJson());
}

void handleNotFound() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(404, "text/plain", "Use /api/readings (or /data) on this ESP32.");
}

void startWebServer() {
  server.on("/", HTTP_GET, handleReadings);
  server.on("/data", HTTP_GET, handleReadings);
  server.on("/api/readings", HTTP_GET, handleReadings);
  server.onNotFound(handleNotFound);
  server.begin();
}

// --------------------------------------------------------------------------- //
// HTTP POST (non-blocking with a short timeout)
// --------------------------------------------------------------------------- //
bool postReading() {
  if (WiFi.status() != WL_CONNECTED) return false;

  WiFiClient client;
  client.setTimeout(HTTP_TIMEOUT_MS / 1000);
  if (!client.connect(BACKEND_HOST, BACKEND_PORT)) {
    Serial.println("[HTTP] connect failed");
    return false;
  }
  client.setNoDelay(true);

  String json = buildJson();
  client.print("Host: "); client.print(BACKEND_HOST); client.print("\r\n");
  client.print("Content-Type: application/json\r\n");
  client.print("Content-Length: "); client.print(json.length()); client.print("\r\n");
  client.print("Connection: close\r\n\r\n");
  client.print(json);

  unsigned long start = millis();
  while (client.connected() && !client.available() && (millis() - start) < HTTP_TIMEOUT_MS) {
    delay(10);
  }

  String statusLine = client.readStringUntil('\n');
  client.stop();

  bool ok = statusLine.indexOf("200") > 0 || statusLine.indexOf("422") > 0;
  if (ok) {
    gLastBackendOkMs = millis();
    Serial.print("[HTTP] "); Serial.println(statusLine);
  } else {
    Serial.print("[HTTP] failed: "); Serial.println(statusLine);
  }
  return ok;
}

String isoNow() {
  // NTP is optional; if time is not synced we send an empty-ish timestamp and the
  // backend records its own server time anyway.
  time_t now = time(nullptr);
  if (now < 1600000000) return "";   // not synced
  struct tm tmv;
  gmtime_r(&now, &tmv);
  char buf[32];
  strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmv);
  return String(buf);
}

// --------------------------------------------------------------------------- //
// Loop
// --------------------------------------------------------------------------- //
void loop() {
  unsigned long nowMs = millis();

  if (WiFi.status() != WL_CONNECTED && (nowMs - lastWifiAttemptMs) > WIFI_RETRY_MS) {
    connectWifi();
  }

  // Pull mode: once Wi-Fi is up, serve /api/readings for the backend to fetch.
  if (WiFi.status() == WL_CONNECTED && !gWebServerStarted) {
    startWebServer();
    gWebServerStarted = true;
    Serial.print("[Web] serving at http://");
    Serial.print(WiFi.localIP());
    Serial.println("/api/readings");
  }
  if (gWebServerStarted) {
    server.handleClient();
  }

  if ((nowMs - lastSensorMs) >= SENSOR_INTERVAL_MS) {
    lastSensorMs = nowMs;
    readSensors();
    updateDisplay();
  }

  if (WiFi.status() == WL_CONNECTED && (nowMs - lastReportMs) >= REPORT_INTERVAL_MS) {
    lastReportMs = nowMs;
    if (gDistanceValid) {                 // never report a reading with no valid level
      gBackendOk = postReading();
    }
  }

  delay(10);   // cooperative yield - keeps the loop responsive
}

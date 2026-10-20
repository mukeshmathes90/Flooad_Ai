# Flood Prediction and Early Warning System

An end-to-end IoT / machine-learning project that reads water level and rainfall
from an ESP32 sensor node, stores the measurements, forecasts future water levels
with a trained model, **validates every prediction before it is shown**, classifies
flood risk, raises alerts, and displays everything on a live web dashboard.

```
 Physical sensors (HC-SR04, rain, water ADC, PIR)
        │
        ▼
 ESP32  ──(Wi-Fi / mobile hotspot)──►  HTTP POST  ──►  FastAPI backend (0.0.0.0:8000)
                                                            │
                                             validate ──────┤
                                                            ▼
                                                        SQLite DB
                                                            │
                                             ML model ──────┤ (features identical to training)
                                                            ▼
                                          prediction VALIDATION  (reject / constrain / accept)
                                                            │
                                          risk engine (current + forecast, deduped alerts)
                                                            │
                                        ┌───────────────────┴───────────────────┐
                                        ▼                                       ▼
                                  Web dashboard                          ESP32 LED / buzzer
                                  (live + history)                       (local warning)
```

> **Note on origin of this tree:** the working folder provided for this task was
> empty, so the complete project was implemented from scratch following the target
> architecture. See **“What has been tested”** for the honest verification status.

---

## 1. What is included

| Layer        | Files                                                        |
|--------------|--------------------------------------------------------------|
| Backend      | `backend/main.py`, `database.py`, `schemas.py`, `config.py`  |
| API routes   | `backend/api/{sensor,prediction,alert,dashboard}_routes.py`  |
| Services     | `backend/services/{sensor,prediction,alert,validation,test}_service.py` |
| Machine learning | `ml/features.py`, `ml/train_model.py`, `ml/predict.py`, `ml/model/` |
| Data         | `data/historical_data.csv`, `data/sample_sensor_data.csv`, `data/generate_sample_data.py` |
| Frontend     | `frontend/index.html`, `frontend/style.css`, `frontend/dashboard.js` |
| Firmware     | `hardware/esp32/flood_monitor.ino`, `hardware/esp32/wifi_config.h` |
| Tests        | `tests/test_api.py`, `tests/test_validation.py`, `tests/conftest.py` |
| Launchers    | `run_project.bat`, `train_model.bat`, `STOP.bat`, `tools/local_ip.py`, `tools/serve_backend.bat` |
| Database     | `flood_system.db` (created automatically on first run)      |

---

## 2. Units and measurement meaning (important)

The system never silently mixes units. Every reading means the following:

| Field                   | Unit / meaning                                                   |
|-------------------------|------------------------------------------------------------------|
| `water_level_cm`        | centimetres of water **above the sensor reference point**        |
| `ultrasonic_distance_cm`| HC-SR04 distance from sensor down to the water surface (cm)      |
| `rainfall_reading`      | **raw 12-bit ADC** value of the rain sensor (0–4095)             |
| `rainfall_intensity`    | **calibrated relative percentage** (0–100). *Not* mm/hour unless the sensor has been field-calibrated |
| `water_sensor_raw`      | raw 12-bit ADC value of the analogue water input (0–4095)        |
| `pir_motion`            | 0/1 presence — **informational only, never flood evidence**      |

Water level is computed as:

```
water_level_cm = REFERENCE_HEIGHT_CM − ultrasonic_distance_cm
```

`REFERENCE_HEIGHT_CM` is configurable (default **100 cm**) in:
* backend → `backend/config.py` (`REFERENCE_HEIGHT_CM`)
* firmware → `hardware/esp32/wifi_config.h`

**An ultrasonic timeout is never treated as 0 cm.** If the sensor returns no echo
the reading is rejected (firmware simply does not send a water level; the backend
returns HTTP 422 if it receives an impossible value).

---

## 3. Requirements

* Windows 10/11 (the launcher is a `.bat` file), or any OS to run the backend manually.
* **Python 3.10 – 3.13** (this build was verified on CPython **3.13.2**).
* An ESP32 board, HC-SR04, rain sensor, analogue water sensor, PIR, OLED (SSD1306 0x3C),
  3 LEDs, buzzer.
* A mobile phone hotspot both the PC and the ESP32 join.

Python dependencies are in `requirements.txt` (`fastapi`, `uvicorn`, `pydantic`,
`numpy`, `scikit-learn`, `joblib`, `httpx`, `pytest`). They are installed
automatically into a project-local `.venv`.

---

## 4. Quick start (recommended)

1. Double-click **`run_project.bat`** in the project root.
2. The first run will:
   * create `.venv`, install dependencies,
   * train the ML model if `ml/model/flood_model.pkl` is missing (an existing model is
     **never** retrained automatically — use `train_model.bat` for that),
   * detect this computer's IPv4 address on the hotspot,
   * start FastAPI on `0.0.0.0:8000` in its own window *(`tools\serve_backend.bat`)*,
   * wait for `/health`, then open the dashboard in your browser.
3. Read the printed **ESP32 BACKEND URL**, e.g. `http://192.168.43.100:8000/api/sensors/data`.
   Put that IP into `hardware/esp32/wifi_config.h` (`BACKEND_HOST`).

The launcher is idempotent: if a healthy backend is already listening on port 8000 it is
**reused** instead of starting a second copy. If port 8000 is held by a *different*
program, the launcher stops and tells you so — it never kills an unrelated process.

To retrain the model later, double-click **`train_model.bat`**.
To stop, double-click **`STOP.bat`** (it stops **only** this project's `uvicorn
backend.main:app` processes) or just close the "Flood Backend" window.

### Manual start (any OS)

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m ml.train_model
.venv\Scripts\python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Dashboard: `http://localhost:8000` · Health: `http://localhost:8000/health` ·
API docs: `http://localhost:8000/docs`.

---

## 5. Connecting the computer and the ESP32 to the same hotspot

1. Turn on the **mobile hotspot** on your phone.
2. Connect the **Windows computer** to that hotspot (Wi-Fi settings).
3. Connect the **ESP32** to the same hotspot (via `WIFI_SSID` / `WIFI_PASSWORD`
   in `wifi_config.h`).
4. Find the computer's IP **on that network**:

   ```bat
   .venv\Scripts\python tools\local_ip.py
   ```

   or check `ipconfig` → *Wireless LAN adapter Wi-Fi* → **IPv4 Address**.
   Typical Android-hotspot range is `192.168.43.x` or `192.168.137.x`.

5. Put that address in `wifi_config.h`:

   ```c
   #define WIFI_SSID       "YOUR_HOTSPOT_NAME"
   #define WIFI_PASSWORD   "YOUR_HOTSPOT_PASSWORD"
   #define BACKEND_HOST    "192.168.43.100"   // <-- the PC's hotspot IPv4
   #define BACKEND_PORT    8000
   ```

> ❗ **Never use `127.0.0.1` for `BACKEND_HOST`.** `127.0.0.1` means “this device”,
> so the ESP32 would try to reach itself. The PC browser may use `localhost`, but
> the ESP32 must use the PC's hotspot-network IPv4 address.

> 🔥 **Windows Firewall** may prompt to allow Python. Allow it on **Private**
> networks, or add an inbound rule for TCP **8000**. The project never disables
> the firewall automatically.

---

## 6. Uploading the firmware

1. Install the Arduino IDE + ESP32 board package.
2. **Library Manager → install:** *Adafruit SSD1306*, *Adafruit GFX Library*.
3. Open `hardware/esp32/flood_monitor.ino`.
4. Edit `hardware/esp32/wifi_config.h` (SSID, password, backend IP).
5. Select your ESP32 board + port, then **Upload**.
6. Open **Serial Monitor** at **115200** baud to watch Wi-Fi and HTTP logs.

### Pin map (preserved from the prototype)

| Function | GPIO | | Function | GPIO |
|----------|------|-|----------|------|
| Rain ADC | 35 | | Green LED | 14 |
| Water ADC| 34 | | Yellow LED| 12 |
| TRIG     | 5  | | Red LED   | 13 |
| ECHO     | 18 | | Buzzer    | 25 |
| PIR      | 27 | | OLED SDA  | 21 |
| OLED SCL | 22 | | OLED addr | 0x3C |

> ⚠️ **Protect GPIO 18.** The HC-SR04 ECHO pin is a **5 V** output. Use a level
> shifter or a 1 kΩ / 2 kΩ voltage divider so ECHO never exceeds 3.3 V.

### Local warning behaviour

| State    | Green | Yellow | Red | Buzzer        |
|----------|:-----:|:------:|:---:|---------------|
| normal   | ●     |        |     | off           |
| warning  |       | ●      |     | off           |
| high     |       |        | ●   | short chirp   |
| critical |       |        | ●   | continuous    |
| unknown (no valid echo) | | | | off |

The OLED shows water level (cm), ultrasonic distance (cm), rainfall raw + relative %,
risk state, and network / backend status.

---

## 7. Sensor data format (ESP32 → backend)

`POST http://<PC_HOTSPOT_IP>:8000/api/sensors/data`

```json
{
  "device_id": "esp32-flood-01",
  "source": "esp32",
  "firmware": "flood_monitor/1.0",
  "water_level_cm": 23.4,
  "ultrasonic_distance_cm": 76.6,
  "rainfall_reading": 1520,
  "rainfall_intensity": 34.8,
  "water_sensor_raw": 960,
  "pir_motion": 0,
  "timestamp": "2026-06-01T12:00:00Z"
}
```

The backend also accepts a payload with **only** `ultrasonic_distance_cm` and derives
the water level from the configured reference height. Malformed or physically
impossible readings receive **HTTP 422** with the reasons, and are stored flagged
(`valid = 0`) for diagnostics — never as a fake “normal” zero reading.

### ESP32 pull mode (optional, no code needed)

By default the ESP32 **pushes** to your PC. As an alternative the firmware also
runs a tiny web server, and the backend can **pull** from it:

1. Connect the laptop and the ESP32 to the **same hotspot / Wi-Fi**.
2. Read the ESP32's IP from its OLED (the `Net` line), e.g. `192.168.43.55`.
3. In the dashboard go to **System Status → ESP32 connection**, type that IP and
   press **Save** (or **Test** first to check reachability).
4. The backend then fetches `http://<ESP_IP>:80/api/readings` every 5 seconds and
   feeds it through the normal pipeline (validation → DB → ML → risk → email).

The address is persisted in the database, survives restarts, and is shown (with
connection status) on the dashboard. Leave the box empty if you prefer the default
push mode. Clearing it stops the polling. `POST /api/esp/config`, `POST /api/esp/test`
and `GET /api/esp/status` expose the same functionality over the API.

> Both modes work with **no internet** — only the shared local network matters.
> Do not enable push *and* pull at the same time, or each reading will be recorded
> twice.

---

## 8. Dashboard

Open `http://localhost:8000` (the launcher does this for you). The dashboard is a
single-page app with a sidebar navigation and a **light theme** (navy header/sidebar,
white surfaces, teal accents, and the standard risk palette: green / amber / orange /
red). It polls `/api/dashboard/summary` every 3 s (auto-refresh can be paused) and is
organised into these sections:

* **Overview** — a prominent risk banner (overall risk with the *current measured* and
  *forecast* risks shown separately), KPI cards (water level, rainfall, ultrasonic
  distance, rate of rise, predicted peak, risk status, device connectivity, active
  alerts), the water-level history chart with threshold lines, a rainfall chart and a
  risk-distribution doughnut computed from the recent readings.
* **Live Monitoring** — device connectivity / freshness (live / stale / offline /
  no-data, with age), the current reading (raw + derived), the thresholds, and the
  recent sensor-reading table.
* **Predictions** — the forecast chart (measured history, accepted forecast, raw model
  output) and the per-horizon table: horizon, raw value, accepted value, validation
  status, risk, and the **reason** for any rejection.
* **Historical Data** — a time-range + row filter over the stored readings.
* **Alerts** — filterable alert history (risk, origin, rows) with the remote-notification
  status.
* **Model Performance** — MAE / RMSE / R² summary cards, an error-by-horizon chart, the
  full validation/test metrics table, and the training metadata.
* **System Status** — backend/database/model health, DB row counts, runtime config, and
  the software **TEST** controls.

Missing or partial data is always shown as `—`; a stale reading is labelled stale and is
never presented as live, and a rejected forecast is never shown as a valid value. If the
CDN cannot be reached, charts are disabled but the tables and live data still update.

### The `TEST NORMAL DATA` button (and friends)

The buttons **TEST NORMAL DATA**, **TEST WARNING**, **TEST HIGH-RISK**,
**TEST CRITICAL** inject a clearly-labelled scenario (`source = "test"`) through the
**real** pipeline: validation → database → ML prediction → prediction validation →
risk engine → dashboard.

* The risk is **computed**, not forced. NORMAL only comes out normal because the
  injected normal-condition measurements genuinely satisfy the thresholds.
* Every button also sends a **demo email** so you can verify the alert layout and
  SMTP connectivity end-to-end (see "Sending a test email" below).
* Test alerts are stored and shown but are tagged `skipped_test` — **no SMS is sent
  and no hardware is actuated**.
* **RESET TO LIVE** deletes only `source='test'` rows (live data untouched) and
  returns the dashboard to the live ESP32 stream. The `LIVE`/`TEST` toggles switch views.

**LIVE and TEST are fully isolated.** Test readings are processed by a *separate*
risk state machine (`alert_state_test`) from the live one (`alert_state`). Pressing
**TEST CRITICAL** therefore advances only the test state — it can never make the live
system believe a flood has started, so returning to live data afterwards does **not**
fire a bogus `RECOVERY` email. A sticky **TEST MODE** banner is shown across the
dashboard whenever you are on test data, so simulated readings can never be mistaken
for a live flood.

---

## 9. Machine learning (training + evaluation)

```bat
train_model.bat                      :: uses data\historical_data.csv
.venv\Scripts\python -m ml.train_model --input my_real_data.csv
.venv\Scripts\python -m ml.train_model --generate   :: regenerate synthetic demo data
```

Pipeline: clean → chronological split (70/15/15) → leak-free sliding-window features
→ `MultiOutputRegressor(RandomForestRegressor)` for horizons **+2, +4, +6, +8, +10 min**
→ metrics on the untouched validation/test sets → saves model + metadata + report.

Outputs:
* `ml/model/flood_model.pkl`
* `ml/model/feature_meta.json` (feature names/order — checked at inference time)
* `ml/model/evaluation_report.json` (MAE, RMSE, R² per horizon, data source)

### About the bundled data

`data/historical_data.csv` is a demo dataset produced by
`data/generate_sample_data.py`. It exists **only** so the training/prediction
pipeline is runnable and testable.

To get meaningful results, supply historical data with the same columns via
`--input` (or by replacing `data/historical_data.csv`).

### Features (identical in training and inference — `ml/features.py`)

`water_level_cm`, lags (1,2,3,5), `wl_diff1`, `wl_rate` (least-squares slope),
`wl_mean5`, `wl_mean10`, `wl_std10`, `rainfall_reading`, `rainfall_mean5`,
`rainfall_intensity`, `water_sensor_raw`, `hour_sin`, `hour_cos`. No future information
is used. If `feature_meta.json` disagrees with the runtime feature list, inference
**fails loudly** instead of feeding mis-ordered features to the model.

---

## 10. Prediction validation (safety layer)

`backend/services/validation_service.py` sits **between** the model and the dashboard:

1. reject non-finite / missing outputs;
2. reject “impossible jumps” (deviates from the latest measurement by more than
   `MAX_PREDICTION_DEVIATION_CM`, or implies a rate above `MAX_REASONABLE_RATE_CM_PER_MIN`);
3. reject values far outside the physical range;
4. **constrain** mildly out-of-range values to the physical band and label them
   `constrained` (the raw value is always kept separately);
5. otherwise `accepted`.

The dashboard receives **raw value, accepted value, validation status and reason** for
every horizon. A rejected/unavailable prediction **cannot** raise the risk, and the
API never substitutes a fabricated “valid” value. If the model is missing, forecasts
are reported as `unavailable` (an optional, clearly-labelled persistence fallback can
be enabled with `FLOOD_ALLOW_FALLBACK=true`, and it never drives risk).

Current-level sensor alarms and ML forecasts are **two independent signals** and are
displayed separately; a forecast never overrides a genuine measured alarm.

---

## 11. Risk thresholds and alerts

Configurable in `backend/config.py` (prototype values — **calibrate them**):

| Risk     | Water level        |
|----------|--------------------|
| normal   | < 30 cm            |
| warning  | 30 – 45 cm         |
| high     | 45 – 65 cm         |
| critical | ≥ 65 cm            |
| extreme (documented overflow reference) | ≥ 80 cm |

A fast rise (`RAPID_RISE_WARNING_CM_PER_MIN` / `RAPID_RISE_HIGH_CM_PER_MIN`) can
escalate the risk even below the warning level. The **same thresholds** are mirrored
in the firmware and exposed via `GET /api/alerts/thresholds` so backend, dashboard and
ESP32 always agree.

Alerts are logged in the `alerts` table with **duplicate suppression**
(`ALERT_DEDUPE_WINDOW_SECONDS`).

### SMS / remote notifications (optional)

Remote delivery is **disabled by default** and reported honestly as
`not_configured`. To enable, set environment variables before launching:

```bat
set FLOOD_SMS_PROVIDER=twilio
set TWILIO_ACCOUNT_SID=...
set TWILIO_AUTH_TOKEN=...
set TWILIO_FROM_NUMBER=+1...
set FLOOD_SMS_TO=+91...
```

or a generic webhook:

```bat
set FLOOD_SMS_PROVIDER=http
set FLOOD_SMS_HTTP_URL=https://your-webhook
set FLOOD_SMS_HTTP_TOKEN=...
set FLOOD_SMS_TO=+91...
```

`notification_status` is `not_configured`, `sent`, `failed: …`, or `skipped_test`.
**“sent” means the provider accepted the HTTP request — it does not guarantee
handset delivery, and a test alert is never reported as a real SMS.**

### Email alerts (Gmail SMTP)

Email alerts are sent by the backend (never by the ESP32) **only when an
established risk category genuinely changes**. Nothing is emailed on startup, on
the first sensor reading, on ESP32 reconnection, or for repeated readings in the
same category.

**Configuration** — put the sender address and Gmail App Password in `config.txt`
at the project root (both are read on start, and `config.txt` is git-ignored so it
is never committed). Two formats are accepted:

```
# format A: plain lines (sender on line 1, app password on line 2)
you@gmail.com
abcd efgh ijkl mnop
```
```
# format B: key=value
EMAIL_SENDER=you@gmail.com
EMAIL_APP_PASSWORD=abcdefghijklmnop
```

Environment variables override the file: `EMAIL_SENDER`, `EMAIL_APP_PASSWORD`,
`EMAIL_SMTP_HOST` (default `smtp.gmail.com`), `EMAIL_SMTP_PORT` (default `465`),
`EMAIL_ENABLED`, `EMAIL_MOCK`. The App Password is **never** printed to the
terminal, logs, dashboard or API responses (only a masked sender such as
`fl****om` is shown). If credentials are missing/invalid the app starts normally
and reports email as `NOT CONFIGURED`.

**Multiple recipients.** Add one or more addresses to `config.txt`; separate
them with commas. If you add none, alerts default to the sender address itself,
so single-mailbox setups work out of the box.

```
EMAIL_RECIPIENTS=familymember@example.com,official@example.com
```

or, using plain lines (line 3 onwards are treated as recipients):

```
you@gmail.com
abcd efgh ijkl mnop
familymember@example.com, official@example.com
```

The environment variable `EMAIL_RECIPIENTS` overrides the file.

**If email fails with `534 WebLoginRequired`:** Gmail has blocked the login and
is asking you to verify the account. The code is working — the credential/account
needs attention. Fix it by signing in to the Gmail account in a web browser once,
confirming 2-Step Verification is ON, then creating a fresh App Password at
https://myaccount.google.com/apppasswords and putting it in `config.txt`.

**Transition rules** (thresholds come from the existing validated risk engine,
never invented here):

| Situation | Email? |
|---|---|
| App start / first reading / repeated same category | No |
| normal → warning / high / critical | Yes |
| warning → high / critical | Yes |
| high → critical | Yes |
| critical → high / warning (downgrade) | Yes (updated status) |
| any alert state → normal | Yes (recovery) |
| invalid / stale reading alone | No |
| test scenarios (`source='test'`) | No automatic alert (recorded as `skipped_test`) |
| **TEST button pressed** | Yes — an explicit **`[TEST]` demo email** only |

### Sending a test email

To confirm SMTP is working and to preview the alert layout, use either of:

* **System Status → Software test controls** — every `TEST ...` button sends a demo.
* **Alerts → Email notifications → Send test email** — sends one on demand, with a
  risk level of your choice, without injecting any scenario data.

Both paths call `POST /api/email/send-test?scenario=warning` and produce a real
delivery that is unmistakably tagged:

```
Subject: [Flood Alert][TEST] Warning — Demo only, no real flood
```

A demo email is **never** confused with a real alert:

* The `[TEST]` tag appears in the subject and a banner appears in the body.
* It is stored in `email_notifications` with `is_test = 1`.
* It does **not** advance the live risk state, so it can never cause a spurious
  follow-up `RECOVERY` email.

Test-scenario *readings* still never email on their own (`skipped_test`) — the
demo is sent only by the explicit button press, via a `force_send` path that is
used nowhere else in the system.

The last established valid risk is persisted in the `alert_state` table, so
restarts and ESP32 reconnections never resend an alert. Notification attempts are
recorded in `email_notifications` (status, error, attempts) and a failed SMTP send
is retried a bounded number of times without blocking sensor processing,
predictions, storage or the dashboard.

> Honest status: automated tests use a mock SMTP client and **no real email was
> sent** during the suite. A live end-to-end send requires valid credentials and
> is only performed on explicit request.

---

## 12. API reference

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | service / DB / model status |
| POST | `/api/sensors/data` | ingest a reading (200 valid, 422 invalid) |
| GET  | `/api/sensors/latest` | latest reading + device status |
| GET  | `/api/sensors/recent` | recent readings (`limit`, `source`) |
| GET  | `/api/sensors/status` | connectivity / freshness |
| GET  | `/api/predictions/latest` | latest validated forecast batch |
| GET  | `/api/predictions/history` | prediction history |
| GET  | `/api/predictions/model` | model metadata + evaluation metrics |
| POST | `/api/predictions/run` | run a prediction cycle on demand |
| GET  | `/api/alerts/history` | alert history |
| GET  | `/api/alerts/thresholds` | thresholds shared by all components |
| GET  | `/api/alerts/notification-status` | whether SMS is configured |
| GET  | `/api/email/status` | email configuration + latest delivery status |
| GET  | `/api/email/history` | recent email notification attempts |
| GET  | `/api/esp/config` | configured ESP32 address (pull mode) |
| POST | `/api/esp/config` | set/clear the ESP32 address to pull from |
| POST | `/api/esp/test` | check the ESP32 is reachable at an address |
| GET  | `/api/esp/status` | ESP32 pull-mode status (last success/error) |
| GET  | `/api/dashboard/summary` | all dashboard data in one call |
| POST | `/api/test/scenario` | run a test scenario (`normal`/`warning`/`high`/`critical`) |
| POST | `/api/test/reset` | delete `source='test'` rows only |

---

## 13. What has been tested (honest status)

**Verified in this environment (CPython 3.13.2, Windows):**

* Dependency installation into `.venv`.
* Database auto-creation and all CRUD paths.
* FastAPI startup, `/health`, and the dashboard HTML being served.
* `POST /api/sensors/data` with a valid reading → stored, risk `normal`.
* `POST /api/sensors/data` with an impossible reading → `HTTP 422`, stored flagged, never coerced to zero.
* Distance → water-level derivation.
* Model training on the synthetic dataset, saving model + metadata + report.
* Model loading and a full prediction batch with per-horizon validation (`accepted`).
* Test scenarios `normal`/`warning`/`high`/`critical` producing the matching risk.
* `/api/test/reset` deleting only `source='test'` rows.
* Dashboard summary aggregation and model-metrics endpoint.
* **Launcher (`run_project.bat`)**: Python detection, venv/dependency checks, model
  loadability check, IPv4 detection, port-reuse detection, starting the backend via
  `tools\serve_backend.bat`, the `/health` wait, and serving the redesigned dashboard.
* **`STOP.bat`** stopping only this project's `uvicorn backend.main:app` processes
  (unrelated processes that merely use port 8000 are left untouched).
* The redesigned single-page dashboard being served (`/`, `/style.css`, `/dashboard.js`).
* Unit tests for validation, risk classification and feature engineering — **30 tests pass** (`pytest tests`).
* **No real SMS was sent** (provider not configured) and this is reflected in the logs.

**Not tested (requires hardware / external services — please verify on site):**

* Real ESP32 ↔ hotspot ↔ PC HTTP communication and DHCP address acquisition.
* Actual HC-SR04 / rain / water / PIR / OLED / LED / buzzer behaviour, and the level shifter on GPIO 18.
* Real flood-prediction accuracy (the bundled data is synthetic; no genuine flood dataset was available).
* Real SMS delivery (no provider credentials were supplied).

---

## 14. Troubleshooting

| Symptom | What to check |
|---------|---------------|
| Dashboard shows “Cannot reach backend” | Is the “Flood Backend” window still open? Visit `/health`. |
| ESP32 cannot connect | Same hotspot? Correct `BACKEND_HOST` (PC IP, **not** 127.0.0.1)? Firewall inbound TCP 8000? Hotspot “client isolation” off? Server bound to `0.0.0.0`? |
| `POST` returns 422 | Reading physically impossible (level outside 0–100 cm, distance 0/out of range, ADC out of 0–4095). See `errors` in the response. |
| Forecasts show “unavailable” | No model file — run `train_model.bat`. |
| Water level always 0 / wrong | Recalibrate `REFERENCE_HEIGHT_CM` on both backend and firmware. |
| Level jumps to flood on sensor removal | Expected to be **rejected**, not a flood — check the `invalid_reason` rows. |
| Charts missing | Chart.js is loaded from a CDN; with no internet the charts are disabled but data still updates. |

---

## 15. Project structure

```
flood_system/
├── backend/
│   ├── main.py  database.py  schemas.py  config.py
│   ├── api/      sensor_routes.py  prediction_routes.py  alert_routes.py  dashboard_routes.py
│   └── services/ sensor_service.py  prediction_service.py  alert_service.py
│                 validation_service.py  test_service.py
├── ml/
│   ├── features.py  train_model.py  predict.py
│   └── model/       flood_model.pkl  feature_meta.json  evaluation_report.json
├── data/
│   ├── historical_data.csv  sample_sensor_data.csv  generate_sample_data.py
├── frontend/
│   └── index.html  style.css  dashboard.js
├── hardware/esp32/
│   └── flood_monitor.ino  wifi_config.h
├── tests/          conftest.py  test_api.py  test_validation.py
├── tools/          local_ip.py  serve_backend.bat
├── flood_system.db            (created at runtime)
├── requirements.txt
├── run_project.bat  train_model.bat  STOP.bat
└── README.md
```
#   F l o o a d _ A i  
 
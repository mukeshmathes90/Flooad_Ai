/*
 * wifi_config.h - ESP32 flood node configuration.
 * ---------------------------------------------------------------------------
 * EDIT THE VALUES BELOW for your own installation, then upload the sketch.
 *
 * These are PLACEHOLDERS on purpose so no private credentials are published.
 * DO NOT commit your real Wi-Fi password to a public repository.
 *
 * BACKEND_HOST must be the computer's IPv4 address ON THE MOBILE HOTSPOT
 * NETWORK - NOT 127.0.0.1 and NOT the ESP32's own address.
 * run_project.bat prints the correct value (look for "ESP32 BACKEND URL").
 */
#ifndef WIFI_CONFIG_H
#define WIFI_CONFIG_H

// ---- Network --------------------------------------------------------------
#define WIFI_SSID       "YOUR_HOTSPOT_NAME"
#define WIFI_PASSWORD   "YOUR_HOTSPOT_PASSWORD"

// ---- Backend (the computer running FastAPI) -------------------------------
#define BACKEND_HOST    "192.168.43.100"   // <-- replace with the PC's hotspot IPv4
#define BACKEND_PORT    8000

// ---- Identity / calibration ----------------------------------------------
#define DEVICE_ID           "esp32-flood-01"
// Distance (cm) from the ultrasonic sensor down to the reference zero water
// level. Used as: water_level_cm = REFERENCE_HEIGHT_CM - ultrasonic_distance_cm.
// Must match REFERENCE_HEIGHT_CM used by the backend (default 100).
#define REFERENCE_HEIGHT_CM 100.0

#endif  // WIFI_CONFIG_H

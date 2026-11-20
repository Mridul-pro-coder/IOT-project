/*
 * ============================================================================
 *  AirSense — ESP32 Firmware
 *  IoT-based personal environmental monitoring with local + MQTT alerts
 *
 *  Hardware target : ESP32-WROOM-32 DevKit V1 (30-pin, USB-C), Arduino IDE
 *  Sensor          : MQ gas sensor (AO -> GPIO34 via voltage divider)
 *  Actuator        : Buzzer (signal -> GPIO25)
 *  Serial          : USB UART @ 115200 baud
 *
 *  SAFETY NOTE
 *  -----------
 *  The ESP32 GPIOs are 3.3 V and NOT 5 V tolerant. Do NOT connect the MQ
 *  sensor's AO pin directly to GPIO34. Route it through a suitable voltage
 *  divider (e.g. two resistors dividing 5 V down to < 3.3 V). This firmware
 *  ships with SIMULATED MODE ON by default so the whole stack (MQTT, backend,
 *  dashboard) can be developed and demonstrated before the physical sensor
 *  is wired safely.
 *
 *  THE READINGS ON THIS DEVICE ARE RAW, UNCALIBRATED ADC COUNTS. They are
 *  NOT gas concentration (ppm), NOT PM2.5, and NOT an AQI value. Do not
 *  present them as calibrated environmental measurements.
 *
 *  Libraries required (Arduino IDE -> Library manager):
 *    - "ArduinoJson"  v7.x  by Benoît Tiffreaux — JSON encoding/decoding
 *        (v6 will NOT compile: v6 uses StaticJsonDocument / containsKey)
 *    - "PubSubClient" by Nick O'Leary — MQTT
 *    - "WiFi"        bundled with the ESP32 Arduino core
 *
 *  Board: "ESP32 Dev Module" in the Arduino IDE. USB CDC On Boot = Enabled
 *  if you need Serial over the native USB port on the 30-pin DevKit.
 *
 *  Credentials: fill in the placeholders below (or keep them in a private,
 *  uncommitted file). Never commit real Wi-Fi / MQTT secrets.
 * ============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

/* --------------------------------------------------------------------------
 *  1. EDIT ME — Device configuration
 * ------------------------------------------------------------------------ */
#define WIFI_SSID        ""            // your Wi-Fi name
#define WIFI_PASSWORD    ""            // your Wi-Fi password
#define MQTT_BROKER     "192.168.1.100" // IP/hostname of Mosquitto broker
#define MQTT_PORT        1883
#define MQTT_USER        ""            // optional; leave "" if broker is open
#define MQTT_PASSWORD    ""            // optional

#define DEVICE_ID        "airsense-esp32-01"
#define TOPIC_PREFIX     "airsense/group9/"
#define TOPIC_TELEMETRY  TOPIC_PREFIX "telemetry"
#define TOPIC_STATUS     TOPIC_PREFIX "status"
#define TOPIC_ALERTS     TOPIC_PREFIX "alerts"
#define TOPIC_CONFIG     TOPIC_PREFIX "config"
#define TOPIC_LWT        TOPIC_PREFIX "lastwill"
// Shared multi-user buzzer policy: the backend publishes a team decision
// (any PRESENT user at HIGH -> on). The device still keeps its own local
// safety fallback below, so this channel going away never silences a real
// local HIGH.
#define TOPIC_BUZZER_CMD TOPIC_PREFIX "cmd/buzzer"

// Serial / debug
#define BAUD             115200

/* --------------------------------------------------------------------------
 *  2. Pins & hardware
 *  ------------------------------------------------------------------------ */
#define SENSOR_PIN       34           // ADC-only pin (AO of gas sensor via divider)
#define BUZZER_PIN       25          // buzzer signal
#define BUZZER_ACTIVE_HIGH true      // set false if your buzzer/module is active-LOW
#define ADC_SAMPLES      20          // median-of-N raw samples per cycle

/* --------------------------------------------------------------------------
 *  3. Operating modes & defaults
 * ------------------------------------------------------------------------
 *  SENSOR_MODE_SIMULATED = true  -> use the on-board simulator (safe demo)
 *                                   telemetry is tagged "simulation": true
 *  SENSOR_MODE_SIMULATED = false -> read the real MQ sensor ADC
 *  Set to false only after the voltage divider is confirmed safe.
 * ------------------------------------------------------------------------ */
#define SENSOR_MODE_SIMULATED  true

// Thresholds are in the SAME uncalibrated ADC units as the sensor readings.
// Tune these to your observed baseline (e.g. ~76-77 at rest).
float g_moderateThreshold = 100.0f;   // filtered >= this        -> MODERATE
float g_highThreshold     = 150.0f;   // filtered >= this        -> HIGH (buzzer)
float g_hysteresis        = 5.0f;     // band around thresholds to debounce
int   g_debounceSamples   = 3;        // consecutive samples before a level change
float g_ewmaAlpha         = 0.30f;    // EWMA smoothing (0.1 = slow, 0.5 = fast)
bool  g_buzzerEnabled     = true;      // false = warnings computed but buzzer silent

// Timing (non-blocking, millis-based)
unsigned long g_publishIntervalMs = 2000; // telemetry cadence
unsigned long g_reconnectDelayMs  = 2000;

// ---------------------------------------------------------------------------
// 4. Runtime state
// ---------------------------------------------------------------------------
WiFiClient  espClient;
PubSubClient mqtt(espClient);

float        ewma = 0.0f;          // filtered value
int          sensorInit = -1;      // EWMA seed (first sample)
char         currentLevel[8] = "LOW";
int          levelDebounce = 0;    // consecutive samples in candidate level
char        pendingLevel[8] = "LOW";
unsigned long lastPublishMs = 0;
unsigned long lastAlertMs   = 0;
unsigned long lastReconnectMs = 0;
unsigned long lastStatusMs  = 0;
bool         lastBuzzerState = false;
bool         lastMqttUp     = false;

// For the simulator: a slow sine plus periodic ramp-up events so the demo
// crosses the HIGH threshold on purpose and the predictive/buzzer path runs.
float g_simPhase = 0.0f;

// ---------------------------------------------------------------------------
// Shared multi-user buzzer policy state.
// The backend publishes a team decision (any present user at HIGH). The
// device ORs it with its own local HIGH detection:
//   buzzer = g_buzzerEnabled && (localHIGH || teamHIGH_fresh)
// The local path always runs, so a dropped backend/Wi-Fi link can NEVER
// silence a real local HIGH — the team command only adds to it.
// ---------------------------------------------------------------------------
bool            g_teamBuzzerOn = false;
unsigned long   g_teamBuzzerTs = 0;       // millis() of last team command
#define TEAM_CMD_STALE_MS 15000           // team command older than this is ignored

void setup() {
  Serial.begin(BAUD);
  delay(200);
  Serial.println();
  Serial.println("=====================================================");
  Serial.println(" AirSense ESP32 firmware starting");
  Serial.println("=====================================================");

  // Buzzer output
  pinMode(BUZZER_PIN, OUTPUT);
  setBuzzer(false);

  // Sensor ADC. GPIO34 is ADC1 channel 5. Read at 12-bit resolution so the
  // full 0-4095 range is available. If your baseline reads ~76, your setup is
  // producing a small signal; that is fine — thresholds are in these units.
  analogReadResolution(12); // use the full 12-bit range (default is often 12 on
                             // the ESP32 Arduino core, but set it explicitly)
  analogRead(SENSOR_PIN);    // warm up the ADC

  connectWiFi();

  // Seed EWMA once we have a first reading (or sim value)
  ewma = readSensorOnce();

  configureMQTT();
}

/* --------------------------------------------------------------------------
 *  Wi-Fi: connect, then keep the link alive with reconnect logic
 *  ------------------------------------------------------------------------ */
void connectWiFi() {
  Serial.printf("[WiFi] connecting to \"%s\" ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(DEVICE_ID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.printf("[WiFi] connected  IP=%s  RSSI=%d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
  } else {
    Serial.println();
    Serial.println("[WiFi] FAILED to connect. Will keep retrying. "
                   "Local buzzer still works offline.");
  }
}

void maintainWiFi() {
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastTry = 0;
    if (millis() - lastTry > g_reconnectDelayMs) {
      lastTry = millis();
      Serial.println("[WiFi] link lost -> reconnecting...");
      WiFi.reconnect();
    }
  }
}

/* --------------------------------------------------------------------------
 *  MQTT: connect with Last Will, subscribe to config, reconnect on loss
 *  ------------------------------------------------------------------------ */
void configureMQTT() {
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  // LWT is published by the broker if we disconnect ungracefully.
  String lwt = "{\"device_id\":\"" + String(DEVICE_ID) +
               "\",\"state\":\"offline\"," +
               "\"ts\":" + String(millis()) + "}";
  mqtt.setWill(TOPIC_LWT, lwt.c_str());
  mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASSWORD);
  if (mqtt.connected()) {
    // Announce we are online
    String up = "{\"device_id\":\"" + String(DEVICE_ID) +
                "\",\"state\":\"online\"," +
                "\"ts\":" + String(millis()) + "}";
    mqtt.publish(TOPIC_STATUS, up.c_str());
    // Subscribe for dashboard -> device config and the shared buzzer policy
    mqtt.subscribe(TOPIC_CONFIG);
    mqtt.subscribe(TOPIC_CONFIG + "/+/cmd");
    mqtt.subscribe(TOPIC_BUZZER_CMD);
    lastMqttUp = true;
    Serial.println("[MQTT] connected and online status published");
  } else {
    lastMqttUp = false;
    Serial.println("[MQTT] connection failed; will retry");
  }
}

void mqttOnMessage(char *topic, byte *payload, unsigned int length) {
  String msg((char *)payload, length);
  // Shared multi-user buzzer policy from the backend
  if (strncmp(topic, TOPIC_BUZZER_CMD, strlen(TOPIC_BUZZER_CMD)) == 0) {
    handleBuzzerCommand(msg.c_str());
    return;
  }
  // Config messages (thresholds etc.)
  if (strncmp(topic, TOPIC_CONFIG, strlen(TOPIC_CONFIG)) != 0) return;
  handleConfigMessage(msg.c_str());
}

void maintainMQTT() {
  if (!mqtt.connected()) {
    if (millis() - lastReconnectMs > g_reconnectDelayMs) {
      lastReconnectMs = millis();
      configureMQTT();
    }
  }
  // Publish status change when connection state flips
  if (mqtt.connected() != lastMqttUp) {
    lastMqttUp = mqtt.connected();
    String st = "{\"device_id\":\"" + String(DEVICE_ID) +
                "\",\"state\":\"" + String(lastMqttUp ? "online" : "offline") +
                "\",\"ts\":" + String(millis()) + "}";
    mqtt.publish(TOPIC_STATUS, st.c_str());
  }
}

/* --------------------------------------------------------------------------
 *  Config message handler (from dashboard). Invalid configs are REJECTED.
 *  Expected JSON:
 *  {
 *    "moderate_threshold": 100.0,
 *    "high_threshold": 150.0,
 *    "hysteresis": 5.0,
 *    "ewma_alpha": 0.3,
 *    "buzzer_enabled": true,
 *    "publish_interval_ms": 2000
 *  }
 *  All fields are optional; any absent field keeps its current value.
 *  Rejection rules:
 *    - moderate < high
 *    - thresholds within [0, 4095]
 *    - hysteresis >= 0 and < 0.5 * (high - moderate)
 *    - ewma_alpha in (0, 1]
 *    - publish_interval_ms in [500, 30000]
 *  On reject, we log a fault and keep old values.
 * ------------------------------------------------------------------------ */
void handleConfigMessage(const char *json) {
  JsonDocument doc;
  deserializeJson(doc, json);
  if (doc.isNull()) { rejectConfig(json, "malformed JSON"); return; }

  float mod = g_moderateThreshold, high = g_highThreshold;
  float hyp = g_hysteresis, alpha = g_ewmaAlpha;
  int   pub = (int)g_publishIntervalMs;
  bool  buz = g_buzzerEnabled;
  bool  hasAny = false;

  if (doc.contains("moderate_threshold")) { mod   = doc["moderate_threshold"].as<float>(); hasAny = true; }
  if (doc.contains("high_threshold"))     { high  = doc["high_threshold"].as<float>();     hasAny = true; }
  if (doc.contains("hysteresis"))         { hyp   = doc["hysteresis"].as<float>();         hasAny = true; }
  if (doc.contains("ewma_alpha"))         { alpha = doc["ewma_alpha"].as<float>();         hasAny = true; }
  if (doc.contains("buzzer_enabled"))     { buz   = doc["buzzer_enabled"].as<bool>();      hasAny = true; }
  if (doc.contains("publish_interval_ms")){ pub   = doc["publish_interval_ms"].as<int>();  hasAny = true; }
  (void)hasAny; // every branch above is independently validated below

  // Validation
  if (mod < 0 || high < 0 || mod > 4095 || high > 4095) { rejectConfig(json, "thresholds out of range"); return; }
  if (high - mod <= 0) { rejectConfig(json, "high must be > moderate"); return; }
  if (hyp < 0 || hyp > 0.5 * (high - mod)) { rejectConfig(json, "hysteresis out of range"); return; }
  if (alpha <= 0.0f || alpha > 1.0f) { rejectConfig(json, "ewma_alpha must be in (0,1]"); return; }
  if (pub < 500 || pub > 30000) { rejectConfig(json, "publish_interval_ms out of range"); return; }

  g_moderateThreshold = mod;
  g_highThreshold = high;
  g_hysteresis = hyp;
  g_ewmaAlpha = alpha;
  g_buzzerEnabled = buz;
  g_publishIntervalMs = pub;

  Serial.printf("[CONFIG] applied: mod=%.1f high=%.1f hyp=%.1f alpha=%.2f pub=%dms buz=%d\n",
                mod, high, hyp, alpha, pub, buz ? 1 : 0);
  // Confirm to the broker so the dashboard knows the config took effect.
  String ack = "{\"device_id\":\"" + String(DEVICE_ID) +
              "\",\"event\":\"config_applied\"," +
              "\"moderate_threshold\":" + String(mod, 2) + "," +
              "\"high_threshold\":" + String(high, 2) + "," +
              "\"ts\":" + String(millis()) + "}";
  mqtt.publish(TOPIC_CONFIG, ack.c_str());
}

void rejectConfig(const char *json, const char *reason) {
  Serial.printf("[CONFIG] REJECTED: %s (raw: %s)\n", reason, json);
  String rej = "{\"device_id\":\"" + String(DEVICE_ID) +
               "\",\"event\":\"config_rejected\"," +
               "\"reason\":\"" + String(reason) +
               "\",\"ts\":" + String(millis()) + "}";
  mqtt.publish(TOPIC_CONFIG, rej.c_str());
}

/* --------------------------------------------------------------------------
 *  Shared multi-user buzzer policy (from the backend).
 *  Command JSON: {"command":"on"|"off","reasons":[...],"shared_policy":true}
 *  The command only matters while MQTT is up AND fresh; a stale link falls
 *  back to local-only detection automatically (see updateBuzzerPolicy()).
 * ------------------------------------------------------------------------ */
void handleBuzzerCommand(const char *json) {
  JsonDocument doc;
  deserializeJson(doc, json);
  if (doc.isNull() || !doc.contains("command")) {
    Serial.println("[BUZZER-CMD] ignored malformed command");
    return;
  }
  g_teamBuzzerOn = (doc["command"].as<String>() == "on");
  g_teamBuzzerTs = millis();
  int n = doc["reasons"].size();
  Serial.printf("[BUZZER-CMD] team policy -> %s (%d user reason(s))\n",
                g_teamBuzzerOn ? "ON" : "OFF", n);
  for (int i = 0; i < n; i++) Serial.print("    - ");
  for (int i = 0; i < n; i++) {
    Serial.print(doc["reasons"][i].as<String>().c_str());
    if (i + 1 < n) Serial.print(", ");
  }
  if (n > 0) Serial.println();
}

// OR of the local safety fallback and the (fresh, connected) team command.
bool updateBuzzerPolicy() {
  bool localHigh = (strcmp(currentLevel, "HIGH") == 0);
  bool teamFresh = mqtt.connected() &&
                   (millis() - g_teamBuzzerTs < TEAM_CMD_STALE_MS);
  bool on = g_buzzerEnabled && (localHigh || (teamFresh && g_teamBuzzerOn));
  setBuzzer(on);
  return on;
}

/* --------------------------------------------------------------------------
 *  Sensor read: real ADC (median of N) or simulator.
 *  Always returns a value in the same ADC units; caller tags simulation.
 * ------------------------------------------------------------------------ */
float readSensorOnce() {
  if (SENSOR_MODE_SIMULATED) return readSimulated();
  // Real hardware: median of N samples rejects glitches.
  int buf[ADC_SAMPLES];
  for (int i = 0; i < ADC_SAMPLES; i++) {
    buf[i] = analogRead(SENSOR_PIN);
    delayMicroseconds(100); // let the sensor settle a bit
  }
  // simple insertion sort for median (N is small)
  for (int i = 0; i < ADC_SAMPLES; i++)
    for (int j = i + 1; j < ADC_SAMPLES; j++)
      if (buf[j] < buf[i]) { int t = buf[i]; buf[i] = buf[j]; buf[j] = t; }
  int median = (ADC_SAMPLES % 2) ? buf[ADC_SAMPLES / 2]
                                 : (buf[ADC_SAMPLES / 2 - 1] + buf[ADC_SAMPLES / 2]) / 2;
  return (float)median;
}

float readSimulated() {
  // Slowly varying base + a periodic ramp-up that will cross the HIGH
  // threshold so the demo reliably exercises the alert/buzzer path.
  static unsigned long simStart = millis();
  unsigned long t = (millis() - simStart) / 1000;   // seconds
  // A 30-second cycle: baseline ~80, rising to ~200 near the top of the cycle.
  float cycle = (t % 30.0f);
  float base  = 78.0f;
  float amp   = 130.0f;
  float val   = base + amp * 0.5f * (1.0f - cos(2.0f * 3.14159265f * cycle / 30.0f));
  // small sensor-like noise
  val += (random(0, 100) / 100.0f - 0.5f) * 4.0f;
  return val;
}

// EWMA update: ewma = alpha * x + (1 - alpha) * ewma
void updateEwma(float sample) {
  if (sensorInit < 0) {
    ewma = sample;
    sensorInit = 0;
  } else {
    ewma = g_ewmaAlpha * sample + (1.0f - g_ewmaAlpha) * ewma;
  }
}

/* --------------------------------------------------------------------------
 *  Exposure classification with hysteresis + debounce.
 *  - To ENTER HIGH:   filtered >= high - hysteresis  (buzzer arms here)
 *  - To LEAVE  HIGH:  filtered <  high - hysteresis*2
 *  - To ENTER MODERATE: filtered >= moderate - hysteresis
 *  - To LEAVE MODERATE:  filtered <  moderate - hysteresis
 *  A level only commits after g_debounceSamples consecutive readings.
 * ------------------------------------------------------------------------ */
void updateExposureLevel(float filtered) {
  char candidate[8];

  if (strcmp(currentLevel, "HIGH") == 0) {
    if (filtered < (g_highThreshold - g_hysteresis * 2.0f)) {
      snprintf(candidate, sizeof(candidate), "MODERATE");
    } else {
      snprintf(candidate, sizeof(candidate), "HIGH");
    }
  } else if (strcmp(currentLevel, "MODERATE") == 0) {
    if (filtered >= (g_highThreshold - g_hysteresis)) {
      snprintf(candidate, sizeof(candidate), "HIGH");
    } else if (filtered < (g_moderateThreshold - g_hysteresis)) {
      snprintf(candidate, sizeof(candidate), "LOW");
    } else {
      snprintf(candidate, sizeof(candidate), "MODERATE");
    }
  } else { // LOW
    if (filtered >= (g_highThreshold - g_hysteresis)) {
      snprintf(candidate, sizeof(candidate), "HIGH");
    } else if (filtered >= (g_moderateThreshold - g_hysteresis)) {
      snprintf(candidate, sizeof(candidate), "MODERATE");
    } else {
      snprintf(candidate, sizeof(candidate), "LOW");
    }
  }

  if (strcmp(candidate, pendingLevel) == 0) {
    levelDebounce++;
  } else {
    levelDebounce = 1;
    snprintf(pendingLevel, sizeof(pendingLevel), "%s", candidate);
  }

  if (levelDebounce >= g_debounceSamples && strcmp(pendingLevel, currentLevel) != 0) {
    char prev[8];
    snprintf(prev, sizeof(prev), "%s", currentLevel);
    snprintf(currentLevel, sizeof(currentLevel), "%s", pendingLevel);
    levelDebounce = 0;
    onLevelChange(prev, currentLevel, filtered);
  }
}

void onLevelChange(const char *prev, const char *now, float filtered) {
  Serial.printf("[EXPOSURE] %s -> %s (filtered=%.1f)\n", prev, now, filtered);
  // Local offline alert + buzzer decision
  if (strcmp(now, "HIGH") == 0) {
    localAlert("HIGH exposure level reached");
  } else if (strcmp(prev, "HIGH") == 0) {
    localAlert("HIGH exposure cleared");
  }
}

void localAlert(const char *msg) {
  Serial.printf("[ALERT] %s\n", msg);
  unsigned long now = millis();
  if (now - lastAlertMs < 3000) return; // throttle spam
  lastAlertMs = now;

  if (mqtt.connected()) {
    JsonDocument d; // ArduinoJson v7 auto-sizes; use StaticJsonDocument in v6
    d["device_id"] = DEVICE_ID;
    d["event"]     = "exposure";
    d["level"]     = currentLevel;
    d["message"]   = msg;
    d["filtered_adc"] = ewma;
    d["simulation"]  = SENSOR_MODE_SIMULATED;
    d["ts"]          = (long)now;
    char buf[280];
    serializeJson(d, buf);
    mqtt.publish(TOPIC_ALERTS, buf);
  }
}

/* --------------------------------------------------------------------------
 *  Buzzer control — respects ACTIVE_HIGH.
 *  If your buzzer needs more than ~20 mA, add a transistor; keep GPIO25
 *  driving the base (with a resistor) — this code is unchanged.
 *  NOTE: the global enable (g_buzzerEnabled) is applied by
 *  updateBuzzerPolicy(), not here, so the pin setter stays a pure function
 *  of the requested state.
 * ------------------------------------------------------------------------ */
void setBuzzer(bool on) {
  int pinState = BUZZER_ACTIVE_HIGH ? (on ? HIGH : LOW)
                                    : (on ? LOW : HIGH);
  if (pinState != lastBuzzerState) {
    digitalWrite(BUZZER_PIN, pinState);
    lastBuzzerState = pinState;
  }
}

/* --------------------------------------------------------------------------
 *  Telemetry JSON publish
 * ------------------------------------------------------------------------ */
void publishTelemetry(float raw) {
  JsonDocument d; // ArduinoJson v7 auto-sizes
  d["device_id"]     = DEVICE_ID;
  d["ts_device"]     = (long)millis();
  d["raw_adc"]       = (int)raw;
  d["filtered_adc"]  = ewma;
  d["simulation"]    = SENSOR_MODE_SIMULATED;
  d["exposure"]      = currentLevel;
  d["buzzer"]        = lastBuzzerState && g_buzzerEnabled;
  d["wifi_rssi"]     = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : -99;
  d["mqtt_connected"]= mqtt.connected();
  // Sensor fault: real mode, reading pinned to a rail => likely open/short.
  bool fault = false;
  if (!SENSOR_MODE_SIMULATED) {
    int r = analogRead(SENSOR_PIN);
    fault = (r <= 0) || (r >= 4095);
  }
  d["sensor_fault"]  = fault;
  d["uptime_s"]      = (unsigned long)(millis() / 1000);

  char buf[560];
  size_t n = serializeJson(d, buf);
  if (mqtt.publish(TOPIC_TELEMETRY, buf, n)) {
    Serial.printf("[TELEMETRY] raw=%d filtered=%.1f level=%s sim=%d\n",
                  (int)raw, ewma, currentLevel, SENSOR_MODE_SIMULATED);
  }
}

/* --------------------------------------------------------------------------
 *  Main loop — fully non-blocking.
 *  Cadence:
 *    - every 50 ms   : sensor sample + EWMA + exposure + buzzer
 *    - every g_publishIntervalMs : MQTT telemetry publish
 *    - continuous    : WiFi/MQTT keepalive, reconnect
 * ------------------------------------------------------------------------ */
void loop() {
  static unsigned long lastSampleMs = 0;

  // 1) Sample + filter + classify at a fixed cadence.
  if (millis() - lastSampleMs >= 50) {
    lastSampleMs = millis();
    float sample = readSensorOnce();
    updateEwma(sample);
    updateExposureLevel(ewma);
    // Drive the shared buzzer: local HIGH (always active, offline-safe)
    // OR the fresh team policy from the backend (any present user HIGH).
    updateBuzzerPolicy();
  }

  // 2) MQTT telemetry on its own (slower) cadence.
  if (millis() - lastPublishMs >= g_publishIntervalMs) {
    lastPublishMs = millis();
    if (mqtt.connected()) {
      // Re-read raw for the telemetry payload (in sim mode this is a fresh sim value)
      publishTelemetry(readSensorOnce());
    }
  }

  // 3) Link maintenance.
  maintainWiFi();
  maintainMQTT();
}

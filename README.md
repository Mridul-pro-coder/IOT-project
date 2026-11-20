# 🌫️ AirSense

**Air Quality-Linked Personalized Asthma Risk Prediction & Early Warning System**

A complete IoT pipeline: an ESP32 reads a gas sensor, publishes over MQTT, a Python backend stores it in SQLite and evaluates every user's personal risk thresholds, and a Streamlit dashboard shows live monitoring, predictive alerts, and multi-user risk — all in real time.

> ⚠️ **This is a university IoT prototype, not a certified medical device.**
> The MQ gas sensor outputs **raw, uncalibrated ADC counts** — they are **not** ppm, **not** PM2.5, and **not** AQI.
> Predictions are **environmental trend forecasts** (time-to-threshold crossing), not clinical asthma predictions.
> All personalization is **rule-based thresholds** today — no ML model is trained or used.

---

## Table of contents

1. [What you get](#1-what-you-get)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Prerequisites](#3-prerequisites)
4. [Clone the repo](#4-clone-the-repo)
5. [Set up the Python backend](#5-set-up-the-python-backend)
6. [Set up Mosquitto (MQTT broker)](#6-set-up-mosquitto-mqtt-broker)
7. [Flash the ESP32 firmware](#7-flash-the-esp32-firmware)
8. [Run the full system](#8-run-the-full-system)
9. [Using the dashboard](#9-using-the-dashboard)
10. [Demo checklist](#10-demo-checklist)
11. [Troubleshooting](#11-troubleshooting)
12. [Project structure](#12-project-structure)
13. [Tests](#13-tests)
14. [Hardware notes & safety](#14-hardware-notes--safety)

---

## 1 · What you get

| Component | Role |
|---|---|
| **ESP32 firmware** | Reads the gas sensor, applies median-of-N + EWMA filtering, classifies exposure, drives a local buzzer, and publishes telemetry over MQTT. |
| **Mosquitto broker** | Receives messages from the ESP32 and the dashboard; distributes them to the backend. |
| **Python MQTT service** | Subscribes to all topics, validates every message, writes telemetry to SQLite, evaluates **every present user's personal thresholds** (different users can see different bands from the same reading), manages deduplicated alert episodes, and publishes shared buzzer commands. |
| **Streamlit dashboard** | 10 views — live charts, predictive forecasts, per-user risk, exposure history with CSV export, symptom logs linked to sensor windows, device config push, and sensor provenance. |
| **SQLite database** | Created automatically at `data/airsense.db`. Stores readings, user profiles, alert episodes, symptom logs, and calibration metadata. |

---

## 2 · Architecture at a glance

```
   ┌──────────────────┐        ┌──────────────────────────────────┐
   │ MQ gas sensor    │  AO →  │  ESP32 (Arduino C++)              │
   │ + shared buzzer  │        │  median-N + EWMA + hysteresis     │
   └──────────────────┘        │  local offline buzzer (HIGH)      │
                                │  Wi-Fi → MQTT (Mosquitto)         │
                                └──────────────┬───────────────────┘
      GPIO25  ◄─────── buzzer   team buzzer OR: local HIGH always arms;
                                               backend policy ORs on top
                                │        │
                                │   ┌────┴──────────────────┐
                                │   │ Mosquitto (broker)    │
                                │   └────┬──────────────────┘
                                │        │
                                │   ┌────┴──────────────────┐
                                │   │ Python MQTT service   │
                                │   │ (Paho MQTT + SQLite)  │
                                │   │                       │
                                │   │ • stores telemetry    │
                                │   │ • evaluates per-user  │
                                │   │   risk bands          │
                                │   │ • opens/touches/closes│
                                │   │   deduplicated alerts │
                                │   │ • publishes shared    │
                                │   │   buzzer command      │
                                │   └────┬──────────────────┘
                                │        │
                                │   ┌────┴──────────────────┐
                                │   │ SQLite                │
                                │   │ data/airsense.db      │
                                │   └──────────────────────┘
                                │        │ reads
                                │   ┌────┴──────────────────┐
                                │   │ Streamlit dashboard    │
                                │   │ (10 views + Plotly)    │
                                │   └──────────────────────┘
```

**Two independent alert paths, by design:**
1. **Local (always works)** — the ESP32 classifies its own reading and arms the buzzer on HIGH, even with Wi-Fi off.
2. **Shared team policy (needs network)** — the backend evaluates one reading against every present user's thresholds; if *any* present user is HIGH, it commands the buzzer on. The ESP32 ORs the two; the team command can only add to the local alert, never replace it.

---

## 3 · Prerequisites

| Tool | Minimum version | How to get it |
|---|---|---|
| **Python** | 3.9 (3.10+ recommended) | [python.org/downloads](https://www.python.org/downloads/) — check "Add to PATH" |
| **Arduino IDE** | 2.x | [arduino.cc/en/software](https://www.arduino.cc/en/software) |
| **ESP32 board support** | espressif v2.x | Boards Manager in Arduino IDE → search `esp32` |
| **Arduino libraries** | ArduinoJson v7, PubSubClient | Library Manager in Arduino IDE |
| **Mosquitto** | any recent | [mosquitto.org/download](https://mosquitto.org/download/) (Windows zip) |
| **Git** | any | [git-scm.com](https://git-scm.com/) |
| **Windows Firewall exception** | — | Allow Mosquitto through on private networks |

---

## 4 · Clone the repo

```bash
git clone https://github.com/<your-org>/AirSense.git
cd AirSense
```

If your team uses a different URL, use that. The `main` branch is the default.

---

## 5 · Set up the Python backend

### 5.1 Create a virtual environment

```bash
# from the AirSense/ folder
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Windows Command Prompt
.venv\Scripts\activate.bat
```

You should see `(.venv)` appear at the start of your prompt.

### 5.2 Install dependencies

```bash
pip install -r requirements.txt
```

> **Important:** `paho-mqtt` is pinned to **< 2.0** on purpose.
> Paho 2.x (released 2024) changed the `Client` constructor and callback
> signatures — this codebase uses the 1.6-style API and will crash at
> startup with 2.x. Do not `pip install --upgrade paho-mqtt`.

### 5.3 Create your `.env` file

```bash
copy .env.example .env   # Windows Command Prompt
# or
cp .env.example .env     # PowerShell / Git Bash
```

Edit `.env` and set at least:

```
MQTT_HOST=localhost
MQTT_PORT=1883
```

Everything else has sensible defaults. The `.env` file is gitignored — your credentials never leave your machine.

---

## 6 · Set up Mosquitto (MQTT broker)

### 6.1 Install

1. Download the Windows zip from <https://mosquitto.org/download/>
2. Unzip to `C:\mosquitto` (or anywhere you like)
3. Add `C:\mosquitto` to your system `PATH`:
   - **PowerShell (admin):** `[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\mosquitto", "User")`
   - Restart your terminal after this.

### 6.2 Verify installation

```bash
mosquitto --version
# Expected output, e.g.: mosquitto version 2.0.18
```

### 6.3 (Optional) Test that the broker starts and accepts connections

```bash
# Terminal A:
mosquitto -c broker\mosquitto.conf

# Terminal B (quick smoke test — close after you see "hello"):
mosquitto_pub -h localhost -t airsense/group9/ping -m hello

# Terminal C:
mosquitto_sub -h localhost -t airsense/group9/ping
# Should print: hello
```

Close Terminal B and C. Keep Terminal A open — the broker stays running there for the rest of the demo. If it's already running as a Windows service, skip starting it again.

---

## 7 · Flash the ESP32 firmware

### 7.1 Prerequisites in Arduino IDE

1. Open **Arduino IDE** → **File → Preferences** → Additional Boards Manager URLs → paste:
   ```
   https://dl.espressif.com/dl/package_esp32_index.json
   ```
2. **Tools → Board → Boards Manager** → search **"ESP32 by Espressif Systems"** → Install.
3. **Sketch → Include Library → Manage Libraries** → install:
   - **ArduinoJson** by Benoit Blanchon — **version 7.x** (v6 will not compile — the code uses `StaticJsonDocument` and `serializeJson()` from v7)
   - **PubSubClient** by Nick O'Leary

### 7.2 Configure the firmware

Open `firmware/esp32_airsense/esp32_airsense.ino` in Arduino IDE.

Find the **EDIT THESE** block near the top of the file and fill in:

```cpp
// ── Wi-Fi ──────────────────────────────────────────
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ── MQTT ───────────────────────────────────────────
const char* DEVICE_ID     = "group9-esp32-01";     // any unique name
const char* MQTT_BROKER   = "192.168.1.50";        // your LAPTOP's LAN IP
                                                      // (NOT localhost or 127.0.0.1)
const uint16_t MQTT_PORT  = 1883;
```

**How to find your laptop's LAN IP (Windows):**
```powershell
ipconfig
```
Look for "IPv4 Address" on your active Wi-Fi/Ethernet adapter (e.g. `192.168.1.50`). The ESP32 is a separate device on the same network — it cannot reach `localhost`.

### 7.3 Keep simulated mode ON for now

In the same block:
```cpp
#define SENSOR_MODE_SIMULATED true
```

Leave this `true` until you have confirmed the voltage divider is safe (see §14). Simulated mode means the firmware generates synthetic sensor readings so the whole pipeline can be proven without real hardware signals.

### 7.4 Upload

1. **Tools → Board → ESP32 Arduino → ESP32 Dev Module**
2. **Tools → Port → (your COM port, e.g. COM3)**
3. Click **Upload**.
4. After upload finishes, open **Tools → Serial Monitor** and set the baud rate to **115200**.

You should see:

```
[BOOT] AirSense firmware starting
[WiFi] connected  IP=192.168.1.42
[MQTT] connected and online status published
[TELEMETRY] raw=72 filtered=72.0 level=LOW sim=1
```

If you see this, the ESP32 is live. Leave the serial monitor open.

---

## 8 · Run the full system

You need **four things running** (four separate terminals).

### Terminal 1 — Mosquitto broker

```bash
mosquitto -c broker\mosquitto.conf
```

Verify: no errors, it says it is listening on port 1883.

### Terminal 2 — Python MQTT service

```bash
# Make sure your venv is activated: you should see (.venv) in the prompt
py -m backend.mqtt_service
```

Verify output:
```
[mqtt] connected to localhost:1883; subscribing to airsense/group9/#
[mqtt] service running. Ctrl+C to stop.
```

This service keeps retrying if the broker is unreachable, so if you see connection retries, the broker isn't running yet or `MQTT_HOST` is wrong.

### Terminal 3 — Streamlit dashboard

```bash
streamlit run dashboard/app.py
```

A browser tab opens at `http://localhost:8501`. The **Overview** page should show 🔴 offline and "no data yet" — correct, the ESP32 hasn't published yet.

### Terminal 4 — ESP32 serial monitor

Already open from §7.4. Verify you see `[TELEMETRY]` lines every 2 seconds.

### Terminal 5 (optional) — Live topic watcher

```bash
mosquitto_sub -h localhost -p 1883 -t "airsense/group9/#" -v
```

Every JSON message from the ESP32 and the backend's buzzer commands streams here. Leave it open during the demo.

---

## 9 · Using the dashboard

The sidebar has **10 views**:

| View | What you see |
|---|---|
| **Overview** | Current reading, device online/offline status, buzzer state, Wi-Fi signal, and a per-user risk strip for present users. |
| **Team Risk** | Every present user's risk band side by side (HIGH = red, MODERATE = orange, LOW = yellow, CLEAR = green), shared buzzer policy state, and personalized alert history. |
| **Live Monitoring** | Filtered vs raw ADC readings over time, threshold lines, and the exposure level as a color band. |
| **Predictive Analytics** | Rolling linear-regression trend, time-to-threshold forecast, and a comparison of fixed vs predictive vs personalized early-warning signals. |
| **Personalized Profiles** | Named threshold sets (e.g. Sensitive, Standard, Relaxed) and the EWMA smoothing factor. |
| **Exposure History** | Hourly look-back chart, total time in each band, warning events, and a **CSV export** button. |
| **Symptom Feedback** | Free-form symptom/irritation log (unlinked to a sensor window). |
| **User Symptom Logs** | Per-user symptom entries **linked to a sensor window** — each log stores the count/min/mean/max of readings over the look-back period, plus whether the data was simulated or real. |
| **Users** | Create / edit / delete users with their personal LOW-MODERATE-HIGH thresholds and hysteresis, and mark them present or absent. |
| **Device Settings** | Device status, push threshold config to the ESP32 over MQTT (validated before sending), sensor provenance (real vs simulated), and personalization model status. |

### Quick multi-user demo

1. Go to **Users** → create two users: *Aarav (sensitive)* with thresholds LOW=80, MODERATE=100, HIGH=130, and *Priya (standard)* with LOW=100, MODERATE=120, HIGH=180.
2. Mark both **present**.
3. Go to **Team Risk** — both show "not evaluated yet" because no reading has been processed.
4. Wait for the simulated readings to climb (the firmware ramps every 30 s). When the filtered value crosses 130, Aarav's card turns 🔴 red and shows HIGH, while Priya may still be MODERATE or LOW — same reading, different thresholds, different result.
5. Check **Exposure History** → **Download CSV** for the presentation.
6. Go to **User Symptom Logs** → log a symptom for Aarav with a 15-minute look-back — it stores the sensor window's statistics alongside the note.

---

## 10 · Demo checklist

Run through all 8 to confirm the system is fully operational:

- [ ] **ESP32 publishing** — Terminal 4 shows `[TELEMETRY]` every ~2 s, Terminal 5 shows the JSON topic stream.
- [ ] **Dashboard live** — **Live Monitoring** chart moves without manual refresh.
- [ ] **Local buzzer** — simulated ramp crosses HIGH → ESP32 buzzer sounds.
- [ ] **Shared buzzer** — a HIGH reading for any present user → backend sends buzzer command → dashboard **Team Risk** shows "Buzzer ON".
- [ ] **Different thresholds = different warnings** — same reading, two users, different bands on **Team Risk**.
- [ ] **Reconnect** — unplug the ESP32 (or stop Mosquitto), then restore. Device reconnects, data resumes, LWT disconnection is logged.
- [ ] **Offline local alert** — stop the Python service (Ctrl+C Terminal 2). The ESP32 still buzzes on its own HIGH (team policy is dead, local fallback is alive).
- [ ] **CSV export** — **Exposure History** → Download CSV opens in Excel.

---

## 11 · Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `mosquitto` says "address in use" | Port 1883 is taken. Start with a different port: `mosquitto -c broker\mosquitto.conf -p 1884`, then set `MQTT_PORT=1884` in `.env` and update the ESP32's `MQTT_PORT` to match. |
| Service prints connection retries | Broker is not running, or `MQTT_HOST` is wrong. The Python service uses `localhost`; the ESP32 uses your **laptop's LAN IP** — they are different. |
| ESP32 Wi-Fi connects but MQTT fails | `MQTT_BROKER` is set to `localhost` or `127.0.0.1` in the firmware — it must be the laptop's LAN IP. Also check Windows Firewall: allow Mosquitto through on private networks. |
| Serial monitor shows garbage | Open it *after* the upload finishes — some boards reset when the serial port opens. Confirm baud rate is 115200. |
| Arduino build: `'StaticJsonDocument' was not declared` | You have **ArduinoJson v6**. Remove it from your libraries and install **v7.x** (Library Manager → search "ArduinoJson" → select the latest). |
| Service crashes: `TypeError: ... paho ...` | Paho 2.x got installed. Run `pip install "paho-mqtt<2.0"` and retry. |
| Dashboard shows "no data yet" after everything is running | The ESP32 isn't publishing. Check Terminal 5 — do `telemetry` lines appear? If not, check the serial monitor for `[MQTT] connected` and `[WiFi] connected`. |
| Team Risk cards say "not evaluated yet" | Normal if no telemetry has arrived yet, or if no user is marked present. This means *"nothing evaluated yet"* — it is **not** an "all clear" indication. |
| Buzzer never sounds | Simulated readings cycle on a ~30 s ramp; wait for one. Or the team command failed — check the ESP32 serial monitor for `[BUZZER-CMD]` lines. |
| SQLite "database is locked" | Should not happen (WAL mode, short-lived connections). If it does, a stale process has the DB open — stop all terminals and restart. |

---

## 12 · Project structure

```
AirSense/
├── firmware/
│   └── esp32_airsense/
│       └── esp32_airsense.ino          # ESP32 Arduino firmware
├── broker/
│   └── mosquitto.conf                  # Mosquitto broker config
├── backend/
│   ├── __init__.py
│   ├── config.py                       # env-driven settings
│   ├── database.py                     # SQLite schema + CRUD (WAL mode)
│   ├── migrations.py                   # incremental schema migrations
│   ├── mqtt_service.py                 # Paho subscriber, validators, publishers
│   ├── risk_engine.py                  # per-user band classification (CLEAR/LOW/MODERATE/HIGH)
│   ├── evaluation.py                   # one reading → all present users + alerts + buzzer
│   ├── analytics.py                    # rolling regression, time-to-threshold
│   ├── ml_adapter.py                   # modular seam for a FUTURE model
│   └── main.py
├── dashboard/
│   └── app.py                          # Streamlit + Plotly (10 views)
├── tests/
│   ├── test_analytics.py
│   ├── test_database.py
│   ├── test_validation.py
│   ├── test_multi_user.py              # one reading → different warnings per user
│   └── test_ml_adapter.py
├── data/
│   └── airsense.db                     # created at runtime (gitignored)
├── .env.example                        # copy to .env and fill in
├── requirements.txt                    # Python deps (paho<2)
├── .gitignore
└── README.md                           # you are here
```

---

## 13 · Tests

No broker or hardware needed. All tests run against an in-memory SQLite database.

```bash
# from the AirSense/ folder, venv activated
py -m pytest tests -q
```

Expected output: **37 passed**.

| Test file | What it covers |
|---|---|
| `test_database.py` | Schema creation, telemetry CRUD, user CRUD, migrations, WAL mode |
| `test_validation.py` | Device config guard rails (rejects invalid thresholds) |
| `test_analytics.py` | Trend/forecast with insufficient-data and noisy-data safeguards |
| `test_multi_user.py` | **Key proof:** one reading → HIGH for one user, MODERATE for another. Plus alert dedup, presence gating, hysteresis stability, shared-buzzer policy, and sensor provenance. |
| `test_ml_adapter.py` | The rule-based predictor is NOT described as trained or transfer-learning. The reserved `user_calibration` table will not produce a false "learned model" claim. |

---

## 14 · Hardware notes & safety

### Wiring

| ESP32 pin | Connection | Notes |
|---|---|---|
| GPIO34 (ADC-only) | MQ sensor AO via **voltage divider** | GPIO34 is **3.3 V ADC only**. Do **not** connect a 5 V sensor output directly. Use a divider (e.g. 2 kΩ + 3.3 kΩ) to keep the pin signal **< 3.3 V**. |
| GPIO25 | Buzzer | Active-high, direct-drive confirmed for typical 5 V buzzers. If the buzzer draws > ~20 mA, add an NPN transistor between GPIO25 and the buzzer. |
| 5 V (VIN) | MQ sensor VCC | Per the module's own specification. |
| GND | MQ sensor GND + buzzer GND | Common ground required. |

### Voltage divider math

Your preliminary baseline is ~76–77 ADC counts with the sensor at rest. With a 2 kΩ / 3.3 kΩ divider on a 5 V rail, the division ratio is ~0.625 — a 5 V signal becomes ~3.125 V at the pin. This converts to ~389 ADC counts at 5 V (4095 × 3.125 / 3.3). Your observed 76–77 counts likely reflects the sensor's actual output voltage in clean air, divided down. **Retune thresholds after wiring** — set the baseline in simulated mode first, then switch to real and recalibrate.

### Simulated mode

The firmware ships with `SENSOR_MODE_SIMULATED true`. The service and dashboard treat simulated and real data the same way — the only difference is the `simulation: 1` flag in every JSON message, and the database logs the data source in `sensor_config` for provenance. Flip the flag to `false` in the firmware only after the voltage divider is confirmed safe with a multimeter.

### Calibration

There is **no calibration**. The raw ADC values are never converted to ppm, PM2.5, or AQI. The dashboard, the firmware, and this README all state that explicitly.

---

## 15 · Configuration reference

### Backend `.env`

All variables are optional. Copy `.env.example` to `.env` in the project root.

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | Broker host (Python side) |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` / `MQTT_PASSWORD` | *(empty)* | Set both if the broker requires authentication |
| `MQTT_TOPIC_PREFIX` | `airsense/group9/` | MQTT topic namespace |
| `DEVICE_OFFLINE_TTL` | `30` | Seconds before the dashboard considers the device offline |
| `DEFAULT_MODERATE` | `100` | Device-standard MODERATE threshold (uncalibrated ADC) |
| `DEFAULT_HIGH` | `150` | Device-standard HIGH threshold (uncalibrated ADC) |
| `AIRSENSE_DATA_DIR` | `./data` | Directory for the SQLite database |
| `AIRSENSE_DB` | `./data/airsense.db` | Full path to the database file |

### Firmware edit block (`esp32_airsense.ino`, top of file)

| Variable | Description |
|---|---|
| `WIFI_SSID` | Your Wi-Fi network name |
| `WIFI_PASSWORD` | Your Wi-Fi password |
| `DEVICE_ID` | Unique device name (e.g. `group9-esp32-01`) |
| `MQTT_BROKER` | Your **laptop's LAN IP** (not `localhost`) |
| `MQTT_PORT` | Broker port (usually 1883) |
| `SENSOR_MODE_SIMULATED` | `true` = synthetic readings, `false` = real sensor |

### MQTT topics

| Topic | Direction | Content |
|---|---|---|
| `airsense/group9/telemetry` | ESP32 → backend | JSON: raw_adc, filtered_adc, exposure level, buzzer state, simulation flag, uptime |
| `airsense/group9/status` | ESP32 → backend | Online/offline status updates |
| `airsense/group9/alerts` | ESP32 → backend | Exposure level change events (band transitions) |
| `airsense/group9/config` | Dashboard → ESP32 | Threshold config (validated before sending; device rejects invalid values) |
| `airsense/group9/cmd/buzzer` | Backend → ESP32 | Shared team buzzer policy command |
| `airsense/group9/lwt` | Broker → backend | Last Will and Testament — fires when the ESP32 disconnects unexpectedly |

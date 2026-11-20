# 🌫️ AirSense

**Air Quality-Linked Personalized Asthma Risk Prediction & Early Warning System**
— an IoT-based environmental monitor with local + networked alerts.

> ⚠️ **Scope & honesty.** This is a university IoT prototype, **not** a medical
> device. The MQ gas sensor produces **raw, uncalibrated ADC counts** — they are
> **not** ppm, **not** PM2.5, and **not** an AQI value. Predictions are
> **environmental trend forecasts** (time-to-threshold), not clinical asthma
> predictions. The system distinguishes *simulated* readings from *real* ones,
> and every UI surface says so.

---

## Architecture

```
   ┌────────────────┐      ┌──────────────┐      ┌─────────────────────┐
   │ MQ gas sensor  │ AO → │    ESP32     │ Wi-Fi │      Mosquitto     │
   │ + buzzer       │      │ (Arduino C++)├──────┤      (broker)      │
   └────────────────┘      └──────┬───────┘ MQTT └─────────┬─────────┘
                                   │                        │
                                   │ local: buzzer + alerts │
                                   │                        ▼
                                   │             ┌──────────────────────┐
                                   │             │  Python MQTT service │
                                   │             │  (Paho + SQLite WAL) │
                                   │             └──────────┬───────────┘
                                   │                        │ reads/writes
                                   │                        ▼
                                   │             ┌──────────────────────┐
                                   │             │ SQLite data/airsense.db│
                                   │             └──────────┬───────────┘
                                   │                        │ reads (WAL)
                                   │                        ▼
                                   │             ┌──────────────────────┐
                                   └────────────┤ Streamlit dashboard  │
                                            (device also              │ + Plotly
                                             publishes to broker)     └──────────────────────┘
```

- **ESP32** samples the sensor (or a simulator), applies median-of-N + EWMA,
  classifies LOW/MODERATE/HIGH with hysteresis, drives the buzzer locally, and
  publishes JSON telemetry over MQTT. It keeps alerting **offline**.
- **Mosquitto** brokers MQTT with a **Last Will** so a dropped device is
  detected.
- **Python service** subscribes, validates, and writes to **SQLite (WAL)**; it
  also pushes validated config back to the device.
- **Streamlit dashboard** reads the same SQLite file and Plotly-rendered views:
  Overview, Live Monitoring, Predictive Analytics, Personalized Profiles,
  Exposure History, Symptom Feedback, Device Settings.

---

## Repository layout

```
AirSense/
├── firmware/esp32_airsense/esp32_airsense.ino   # ESP32 Arduino firmware
├── broker/mosquitto.conf                        # Mosquitto config
├── backend/
│   ├── __init__.py
│   ├── config.py         # env-driven settings + thresholds
│   ├── database.py       # SQLite schema + read/write helpers
│   ├── mqtt_service.py   # Paho subscriber, validator, config publisher
│   ├── analytics.py      # rolling regression, threshold-crossing forecasts
│   └── main.py
├── dashboard/app.py       # Streamlit + Plotly app
├── tests/                 # pytest unit tests (analytics + db + validation)
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 1 · Prerequisites (Windows)

1. **Python 3.10+** — `py -0` to list, `py -3.11` to use a specific one.
2. **Visual Studio** — *not* required (that's for C++ builds); the ESP32
   firmware uses **Arduino IDE** only.
3. **Mosquitto** for Windows — download the Windows build from
   <https://mosquitto.org/download/> and unzip. You need `mosquitto.exe`,
   `mosquitto_pub.exe`, `mosquitto_sub.exe`. Add them to your `PATH` or
   refer to them by full path.
4. **Arduino IDE** — install the **ESP32 board** ("Arduino by espressif") via
   Boards Manager. Add libraries: **ArduinoJson** (v7.x) and **PubSubClient**.
5. **ESP32 DevKit** connected over USB.

> Create a venv and install Python deps:
> ```powershell
> py -3.11 -m venv .venv
> .venv\Scripts\Activate.ps1
> pip install -r requirements.txt
> ```

---

## 2 · Configure the broker

```powershell
# Start Mosquitto (foreground) in one terminal:
mosquitto -c broker\mosquitto.conf
```

Confirm it's listening:
```powershell
mosquitto_sub -h localhost -p 1883 -t "airsense/group9/#" -v
```
*(It prints nothing until a device publishes. Leave it open to watch.)*

---

## 3 · Flash & run the ESP32

1. In Arduino IDE, open `firmware\esp32_airsense\esp32_airsense.ino`.
2. Set **Board = ESP32 Dev Module**.
3. Fill in `WIFI_SSID`, `WIFI_PASSWORD`, and `MQTT_BROKER`
   (the laptop's LAN IP — see §5 for how to find it). **Do not commit real
   secrets.**
4. **Leave `SENSOR_MODE_SIMULATED` as `true`** until the voltage divider is
   confirmed safe. Simulated mode lets you demo the full pipeline with no
   hardware risk.
5. Select the correct COM port, click **Upload**, then open the **Serial
   Monitor at 115200 baud**.

You should see `[WiFi] connected`, `[MQTT] connected and online status
published`, and periodic `[TELEMETRY]` lines.

---

## 4 · Run the Python service + dashboard

Two terminals (the dashboard and service are separate processes):

```powershell
# Terminal A — MQTT subscriber / writer:
py -m backend.mqtt_service

# Terminal B — Streamlit UI:
streamlit run dashboard/app.py
```

The browser opens `http://localhost:8501`. Watch **Live Monitoring** update as
the ESP32 publishes. Push a threshold change from **Device Settings** → the
serial monitor on the ESP32 prints `[CONFIG] applied`.

---

## 5 · How to verify it works (per phase)

### Phase 1 — Firmware + MQTT
- Serial monitor shows `connected` for Wi-Fi and MQTT.
- In a third terminal:
  ```powershell
  mosquitto_sub -h localhost -p 1883 -t "airsense/group9/telemetry" -v
  ```
  You should see JSON telemetry every ~2 s. The **Last Will** fires when you
  unplug the ESP32 (watch the `lastwill` topic).

### Phase 2 — Python + SQLite
- With the ESP32 publishing, check `data\airsense.db`:
  ```powershell
  python -c "import sqlite3; c=sqlite3.connect('data/airsense.db'); print(c.execute('select count(*) from telemetry').fetchone())"
  ```
  The count climbs as messages arrive.

### Phase 3 — Dashboard
- The **Live Monitoring** chart moves without manual refresh.
- **Device** shows 🟢 online; **Buzzer** flips when the (simulated) reading
  crosses HIGH.

### Phase 4 — Prediction / profiles / exposure
- **Predictive Analytics** shows a slope, a time-to-HIGH estimate, and the
  *fixed vs predictive vs personalized* comparison.
- Create a profile in **Personalized Profiles**, then watch the personalized
  signal fire earlier for the sensitive profile.

### Phase 5 — Integration / reconnect
- Yank the ESP32's USB (or drop Wi-Fi): the dashboard flips to 🔴 offline, the
  broker publishes the LWT, SQLite records the last status.
- Reconnect: Wi-Fi + MQTT auto-reconnect, the LWT "online" is republished, and
  data resumes.

---

## 6 · Safety notes (hardware)

- **GPIO34 (ADC) is 3.3 V.** Do **not** wire the MQ sensor's 5 V `AO` output
  straight to it. Use a voltage divider that keeps the signal **< 3.3 V** at the
  ESP32 pin, or use a module with a 3.3 V / logic-level `AO`.
- **Buzzer on GPIO25:** confirmed **active-HIGH, direct GPIO drive** — the
  firmware default (`BUZZER_ACTIVE_HIGH = true`, buzzer signal on GPIO25)
  already matches; if the module turns out to be active-low, flip that single
  flag. If the buzzer draws > ~20 mA, add an NPN transistor (or a small
  buzzer module with its own driver) so the GPIO only drives the base — the
  code is unchanged in that case.
- **Broker:** confirmed `localhost:1883` for the Python side. The ESP32,
  however, is a separate machine on the Wi-Fi, so it must target the
  **laptop's LAN IP** (`ipconfig` → IPv4 under the Wi-Fi adapter), never
  `localhost`.
- The firmware **defaults to simulated mode** precisely so you can prove the
  software before touching the real sensor. Flip it to `false` only when the
  divider/driver is verified.

---

## 7 · Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `mosquitto_sub` sees nothing | Broker not running, or wrong host/port. Confirm with `mosquitto -c ...` and the exact IP. |
| ESP32 Wi-Fi connects but MQTT doesn't | Wrong `MQTT_BROKER`. Use the laptop's **LAN IP**, not `localhost` — the ESP32 can't resolve your laptop by `localhost` over Wi-Fi. |
| Serial prints garbage at 115200 | Open the Serial Monitor **after** upload completes; some boards auto-reset on open. |
| `arduino` build: unknown type `StaticJsonDocument` | You have **ArduinoJson v6**. Install **v7.x** (v7 uses `JsonDocument`). |
| `Streamlit` can't import `backend` | Run with `streamlit run dashboard/app.py` from the repo root (the app injects the root into `sys.path`). |
| DB count frozen in Phase 2 | Python service not running, or it's failing to reach the broker. Check Terminal A. |
| Predictions say "insufficient data" | Expected early on. Needs ≥ 8 stable samples and a decent fit. Not a bug. |
| Buzzer doesn't sound in sim mode | It should — HIGH in simulated mode drives it. Check `BUZZER_ACTIVE_HIGH` and that the module is powered. |

> **Finding the laptop's IP (for `MQTT_BROKER`):** `ipconfig` in PowerShell;
> use the **IPv4** under your Wi-Fi adapter (typically `192.168.x.x`).

---

## 8 · Demo checklist

1. Start Mosquitto → service → dashboard.
2. ESP32 (simulated) streaming; Live chart moving.
3. Watch the simulated ramp cross **HIGH**: buzzer sounds, `alerts` fires,
   history records the event.
4. Switch **Sensitive** profile → its personalized signal trips earlier than
   the Standard one.
5. Kill Wi-Fi on the ESP32 (or unplug) → dashboard goes offline, LWT logged.
6. Restore → auto-reconnect, data resumes.
7. Export **Exposure History** to CSV.
8. Log a **symptom**, see it plotted against sensor time.

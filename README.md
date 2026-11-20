# 🌫️ AirSense

**Air Quality-Linked Personalized Asthma Risk Prediction & Early Warning System** — an IoT environmental monitor with a physical ESP32 sensor, MQTT, a Python backend, and a multi-user personalized risk dashboard.

> ⚠️ **Scope & honesty.** This is a university IoT prototype, **not** a medical device. The MQ gas sensor produces **raw, uncalibrated ADC counts** — they are **not** ppm, **not** PM2.5, and **not** AQI. Predictions are **environmental trend forecasts** (time-to-threshold), not clinical asthma predictions. All personalization today is **rule-based thresholds**, not a learned model. The UI says all of this in the places that matter.

---

## 1 · What the system does

```
   ┌────────────────┐      ┌──────────────────────────┐      ┌──────────────┐
   │ MQ gas sensor  │ AO → │        ESP32 (Arduino)    │ Wi-Fi │   Mosquitto │
   │ + shared buzzer├─────┤ median-N + EWMA + hysteresis├───→ │  (broker)   │
   └────────────────┘      │ local offline buzzer       │      └──────┬─────┘
                            └───────────────────────────┘             │
   Buzzer policy:  local HIGH always arms the buzzer (offline-safe);  │
                   backend's shared team command ORs onto it.        │
                                                                     │
                                                        ┌────────────┴───────────┐
                                                        │  Python MQTT service   │
                                                        │  (Paho + SQLite, WAL) │
                                                        │  - stores telemetry   │
                                                        │  - evaluates every    │
                                                        │    PRESENT user's     │
                                                        │    personal thresholds│
                                                        │  - opens/touches/closes│
                                                        │    deduplicated alerts│
                                                        │  - publishes shared   │
                                                        │    buzzer command     │
                                                        └────────────┬───────────┘
                                                                     │
                                                        ┌────────────┴───────────┐
                                                        │  SQLite data/airsense.db │
                                                        └────────────┬───────────┘
                                                                     │ reads
                                                        ┌────────────┴───────────┐
                                                        │  Streamlit dashboard    │
                                                        │  (10 views + Plotly)    │
                                                        └─────────────────────────┘
```

Two independent alert paths, by design:

1. **Device-local (always works):** the ESP32 classifies its own reading and arms the buzzer on HIGH even with Wi-Fi off. This is the safety fallback.
2. **Shared team policy (network):** the backend evaluates one reading against every present user's thresholds; if *any* present user is HIGH, it commands the buzzer on. The device **ORs** the two — the team command can only add to the local alert, never replace it.

---

## 2 · Repository layout

```
.
├── firmware/esp32_airsense/esp32_airsense.ino  # ESP32 Arduino firmware
├── broker/mosquitto.conf                        # Mosquitto broker config
├── backend/
│   ├── config.py        # env-driven settings (broker host/port, thresholds)
│   ├── database.py      # SQLite schema + read/write helpers (WAL)
│   ├── migrations.py    # incremental schema migrations (never rewrites data)
│   ├── mqtt_service.py  # Paho subscriber, validators, config + buzzer publisher
│   ├── risk_engine.py   # per-user band classification (CLEAR/LOW/MODERATE/HIGH)
│   ├── evaluation.py    # one reading -> all present users + alert dedup + buzzer
│   ├── analytics.py     # rolling regression, time-to-threshold forecasts
│   ├── ml_adapter.py    # modular seam for a FUTURE model (rule-based today)
│   └── main.py
├── dashboard/app.py     # Streamlit + Plotly app (10 views)
├── tests/               # pytest (37 tests)
├── requirements.txt     # Python deps (paho pinned to 1.x — see note)
├── .env.example         # copy to .env and fill in
└── data/airsense.db     # created at runtime (gitignored)
```

---

## 3 · One-time setup (Windows)

Do these once; steps 4–7 are for every run.

### 3.1 Python + dependencies

```powershell
# 1. Create a venv (Python 3.10+ recommended)
py -3.11 -m venv .venv

# 2. Activate it
.venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -r requirements.txt
```

> `requirements.txt` pins `paho-mqtt` to the **1.x line** deliberately.
> Paho 2.x changed the `Client` constructor and callback signatures and
> will crash this code at startup. Do not "upgrade" it.

### 3.2 Mosquitto (the MQTT broker)

Download the Windows build from <https://mosquitto.org/download/> and unzip
to e.g. `C:\mosquitto`. Add `C:\mosquitto` to your `PATH` (or use the full
path in the commands below). You need three tools: `mosquitto.exe`,
`mosquitto_pub.exe`, `mosquitto_sub.exe`.

No configuration is required to start — we ship a config for you in
`broker/mosquitto.conf`.

### 3.3 Arduino IDE + ESP32 firmware

1. Install **Arduino IDE**.
2. Boards Manager → search **"esp32" by espressif** → Install (or
   "ESP32 by Espressif Systems").
3. Library Manager → install **ArduinoJson** (v7.x — v6 will not compile)
   and **PubSubClient**.
4. Open `firmware\esp32_airsense\esp32_airsense.ino`.
5. Fill in the edit-me block at the top of the `.ino`:
   - `WIFI_SSID` / `WIFI_PASSWORD` — your Wi-Fi.
   - `MQTT_BROKER` — **your laptop's LAN IP** (find with `ipconfig` →
     IPv4 of your Wi-Fi adapter, usually `192.168.x.x`).
     ⚠️ The ESP32 is a separate machine: it **cannot** use `localhost`.
6. Leave `SENSOR_MODE_SIMULATED` as `true` until your voltage divider is
   confirmed safe (see §9 Safety).

### 3.4 Environment variables (optional but recommended)

```powershell
copy .env.example .env
# edit .env: at minimum MQTT_HOST (usually "localhost" on the laptop)
```

`.env` is gitignored. You can also set these as real Windows environment
variables instead — `backend/config.py` reads `os.environ` first.

---

## 4 · Run everything (4 terminals)

Run these in the order listed. Each box is one terminal.

### Terminal 1 — Mosquitto broker

```powershell
mosquitto -c broker\mosquitto.conf
```

**Verify:** no error output; the log says it is listening on port 1883.

Smoke test the broker works:

```powershell
# in a scratch terminal:
mosquitto_pub -h localhost -t "airsense/group9/ping" -m "hello"
mosquitto_sub -h localhost -t "airsense/group9/ping"
```

You should see `hello` appear. Then close that scratch terminal.

### Terminal 2 — Python MQTT service

```powershell
py -m backend.mqtt_service
```

**Verify:** it prints

```
[mqtt] connected to localhost:1883; subscribing to airsense/group9/#
[mqtt] service running. Ctrl+C to stop.
```

If it says "connection failed / retrying," the broker isn't running or
`MQTT_HOST`/`MQTT_PORT` are wrong. This service keeps retrying, so you can
fix the broker without restarting it.

### Terminal 3 — Streamlit dashboard

```powershell
streamlit run dashboard/app.py
```

**Verify:** browser opens `http://localhost:8501`. The **Overview** page
shows the device as 🔴 offline and "no data yet" — correct, no ESP32 is
publishing yet.

### Terminal 4 (optional) — live topic watch

```powershell
mosquitto_sub -h localhost -p 1883 -t "airsense/group9/#" -v
```

Leave this open; every JSON message from the ESP32 and the backend's buzzer
commands will stream by here.

### Terminal 5 — Flash & run the ESP32

In Arduino IDE: board **ESP32 Dev Module**, correct COM port, then **Upload**.
Open **Serial Monitor @ 115200** after the upload finishes.

**Verify (in order):**

```
[WiFi] connected  IP=...
[MQTT] connected and online status published
[TELEMETRY] raw=... filtered=... level=LOW sim=1
```

Within a few seconds:
- Terminal 4 shows the `telemetry` JSON every ~2 s.
- Terminal 2 prints `[person] HIGH: ...` lines when the simulated reading
  crosses a user's HIGH band, and `[buzzer] shared policy -> on` after it.
- The dashboard's **Overview** shows 🟢 online, a moving **Live Monitoring**
  chart, and the **Team Risk** strip filling in per-user bands.
- The ESP32's buzzer sounds when its own reading hits HIGH *or* when the
  team command arrives.

That's the whole system running. From here on, Ctrl+C any of the four
terminals and restart it — the schema and data persist in `data/airsense.db`.

---

## 5 · Using the multi-user dashboard

The 10 sidebar views:

| View | What it does |
|---|---|
| **Overview** | Device status, current reading, buzzer, and a live per-user risk strip. |
| **Team Risk** | Every present user's band side by side, shared-buzzer state, personalized alert history. |
| **Live Monitoring** | Filtered-vs-raw chart with threshold lines + level band; last 20 rows. |
| **Predictive Analytics** | Rolling-regression trend, time-to-HIGH estimate, fixed vs predictive vs personalized signals. |
| **Personalized Profiles** | The original named threshold sets (Sensitive/Standard/Relaxed); create more. |
| **Exposure History** | Hourly look-back chart, per-band duration, events, **CSV export**. |
| **Symptom Feedback** | The original free-form symptom log (unlinked). |
| **User Symptom Logs** | Per-user symptom entries **linked to a sensor window** (count/min/mean/max + real-vs-simulated source). |
| **Users** | Create / edit / delete users, their LOW-MODERATE-HIGH thresholds + hysteresis, and **mark present/absent**. |
| **Device Settings** | Device status, push device config over MQTT, sensor provenance, model status. |

### Worked example

1. **Users** → mark *Aarav (sensitive)* **present** and *Standard user* **absent**.
2. **Team Risk** now shows only Aarav. When the simulated reading climbs
   past 130 (his HIGH band), his card turns red, a 🚨 open alert appears,
   and the shared-buzzer box says **ON**.
3. Keep the reading above his band: the alert row is *touched*, not
   duplicated (check **Alert history** — one `HIGH` episode, moving
   `last_seen`).
4. Reading falls below 120 (his HIGH − 2·hysteresis): the alert **closes**
   with a `closed_at` timestamp, buzzer command goes `off`.
5. **User Symptom Logs** → log "mild cough" for Aarav with a 15-minute
   look-back; the stored log carries the window's mean/max and whether it
   was simulated data.
6. **Exposure History** → **Download CSV** for the presentation.

### Why "not evaluated yet"

Per-user risk rows are only written when the MQTT service processes a real
telemetry message. Before the first message, Team Risk cards say
*"not evaluated yet"* — deliberately, so a blank state is never mistaken
for an "all clear."

---

## 6 · How the personalization actually works

- One ESP32 reading (the `filtered_adc` value) is broadcast to
  `evaluation.evaluate_reading()` on every telemetry message.
- It classifies **each present user** against that user's own
  LOW/MODERATE/HIGH thresholds with hysteresis (entry at `threshold − h`,
  exit at `threshold − 2h`, one band change per reading), so the *same*
  reading can be HIGH for one user and MODERATE for another. That is
  requirement 12, and `tests/test_multi_user.py::test_same_reading_different_warnings_per_user`
  proves it.
- Alerts are **episodes**, not rows-per-reading: entering MODERATE/HIGH
  opens one row; staying there touches it; dropping below exits it. No
  notification spam during an ongoing event.
- The **shared buzzer command** goes to `airsense/group9/cmd/buzzer`. The
  ESP32 ORs it with its own local HIGH detection and ignores the team
  command if it is > 15 s old or MQTT is down — the local fallback is
  always authoritative for safety.
- **ML**: `backend/ml_adapter.py` defines a `Personalizer` protocol and a
  `RuleBasedPredictor` (the only active one today). The `user_calibration`
  table is reserved for a future trained model. Until a trained model and
  its evaluation evidence exist on disk, the system *refuses* to describe
  itself as learned or as transfer learning.

---

## 7 · Automated tests

```powershell
py -m pytest tests -q
```

37 tests, no broker or hardware needed:

- `test_analytics.py` — trend/forecast safeguards
- `test_database.py` — schema + round-trips
- `test_validation.py` — device-config guard rails
- `test_multi_user.py` — one reading → different warnings per user,
  alert dedup, presence gating, hysteresis, shared-buzzer policy,
  provenance, symptom-window stats
- `test_ml_adapter.py` — the "no transfer learning" guarantee

---

## 8 · Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `mosquitto` won't start, "address in use" | Port 1883 taken. `mosquitto -c broker\mosquitto.conf -p 1884`, then set `MQTT_PORT=1884` in `.env` and the ESP32's `MQTT_PORT`. |
| Service prints connection retries forever | Broker down, or `MQTT_HOST` wrong. The service is `localhost`; the ESP32 is your laptop's **LAN IP**. |
| ESP32 Wi-Fi connects but MQTT fails | `MQTT_BROKER` set to `localhost` or `127.0.0.1` on the device — it must be the laptop's LAN IP. Also check Windows Firewall: allow Mosquitto through on private networks. |
| Serial monitor shows garbage | Open it *after* the upload completes; some boards reset on open. Set 115200 exactly. |
| Firmware build error `unknown type 'StaticJsonDocument'` | You have ArduinoJson v6. Install **v7.x**. |
| `TypeError ... paho` at service start | Paho 2.x got installed. `pip install "paho-mqtt<2"` (see §3.1 note). |
| Dashboard shows "no data yet" after all 4 terminals run | The ESP32 isn't publishing yet. Check Terminal 4 — do `telemetry` lines appear? If not, check its serial monitor for `[MQTT] connected`. |
| Team Risk cards say "not evaluated yet" | No telemetry has been processed yet, or no user is marked present. Either is fine — it is *not* an "all clear." |
| Buzzer never sounds in sim mode | Simulated readings cross HIGH on a 30 s cycle; wait for one. Or the team policy OR failed: check `[BUZZER-CMD]` lines on the serial monitor. |
| SQLite "locked" errors | Should not happen (WAL mode, short-lived connections). If you see them, a stale process has the DB open — close all streamlit/service terminals and restart. |

---

## 9 · Hardware safety notes

- **GPIO34 is 3.3 V and ADC-only.** Do **not** wire the MQ sensor's 5 V
  `AO` straight to it. Use a voltage divider that keeps the signal
  **< 3.3 V** at the pin (e.g. 2 kΩ + 3.3 kΩ gives ≈ 0.625× — a 5 V rail
  reading would be halved, which also explains why your observed ~76–77
  counts are a small slice of the 0–4095 range). Re-tune thresholds to
  your *divided* baseline, not the module's undivided one.
- **Buzzer on GPIO25** — confirmed active-high, direct drive. If it draws
  > ~20 mA, put an NPN transistor between the GPIO and the buzzer; the
  firmware's `BUZZER_ACTIVE_HIGH` flag stays as-is.
- **Simulated mode is the default** (`SENSOR_MODE_SIMULATED true`) so the
  whole pipeline can be proven before the real sensor is wired. Flip it to
  `false` only after the divider is verified with a multimeter.
- **Calibration:** none. Never report these numbers as ppm, PM2.5, or AQI.

---

## 10 · Demo-day checklist (the 8-point demonstration)

With the 4 terminals + ESP32 running (§4):

1. ☐ ESP32 telemetry visible in Terminal 4 (`telemetry` topic, every ~2 s).
2. ☐ Dashboard **Live Monitoring** chart moves without manual refresh.
3. ☐ Simulated ramp crosses a user's HIGH → buzzer sounds + 🚨 on
   **Team Risk** + `[buzzer] shared policy -> on` in Terminal 2.
4. ☐ Two users with different thresholds: same reading → different bands
   (show it in **Team Risk**).
5. ☐ Unplug the ESP32's Wi-Fi (or its power) → dashboard flips 🔴,
   `lastwill` logged. Reconnect → auto-reconnects, data resumes.
6. ☐ Local-only proof: with the *backend service stopped* (Ctrl+C
   Terminal 2), the ESP32 still buzzes on its own HIGH — the shared policy
   is dead but the local fallback is not.
7. ☐ **Exposure History** → CSV export works.
8. ☐ **User Symptom Logs** → log a symptom, see its stored sensor window
   (mean/min/max + source) against the chart.

---

## 11 · Configuration reference

`.env` (or real env vars) — all optional, shown with defaults:

| Variable | Default | Meaning |
|---|---|---|
| `MQTT_HOST` | `localhost` | Broker host for the Python side |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` / `MQTT_PASSWORD` | *(empty)* | Set both if the broker has auth |
| `MQTT_TOPIC_PREFIX` | `airsense/group9/` | Topic namespace |
| `DEVICE_OFFLINE_TTL` | `30` | Seconds before the dashboard calls the device offline |
| `DEFAULT_MODERATE` / `DEFAULT_HIGH` | `100` / `150` | Device-standard thresholds (uncalibrated ADC) |
| `AIRSENSE_DATA_DIR` / `AIRSENSE_DB` | `./data` / `./data/airsense.db` | Where SQLite lives |

Firmware edit-me block (`esp32_airsense.ino` top): `WIFI_SSID`,
`WIFI_PASSWORD`, `MQTT_BROKER` (laptop LAN IP), `DEVICE_ID`, and the
`SENSOR_MODE_SIMULATED` switch.

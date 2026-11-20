"""
AirSense — interactive web dashboard (Streamlit + Plotly).

Reads the SQLite database written by `backend.mqtt_service` and renders:
  Overview · Live Monitoring · Predictive Analytics · Personalized Profiles
  Exposure History · Symptom Feedback · Device Settings

The dashboard auto-refreshes to show live device data. Run:

    streamlit run dashboard/app.py
(or: streamlit run dashboard)

It communicates with the real backend / ESP32 through the shared database,
and can push device configuration back over MQTT.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# --- make the backend package importable regardless of launch dir ---------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import analytics, config, database  # noqa: E402

# Guarantee the schema exists before any read path runs (init_db is idempotent).
database.init_db()


def _global_css() -> str:
    return """
    <style>
    .stMetric { background:#f6f8fb; padding:14px; border-radius:12px;
                border:1px solid #e6ebf2; }
    h1 { font-weight:700; }
    </style>
    """


st.set_page_config(page_title="AirSense", page_icon="🌫️",
                   layout="wide", initial_sidebar_state="expanded")
st.markdown(_global_css(), unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Data access (cached for a few seconds so the UI stays snappy while the
# dashboard auto-refreshes)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3, show_spinner=False)
def load_telemetry(limit: int = 1000) -> pd.DataFrame:
    rows = database.get_recent_telemetry(limit=limit)
    return pd.DataFrame(rows)


@st.cache_data(ttl=3, show_spinner=False)
def load_profiles() -> list[dict]:
    database.init_db()
    return database.get_profiles()


@st.cache_data(ttl=5, show_spinner=False)
def load_events(limit: int = 200) -> pd.DataFrame:
    return pd.DataFrame(database.get_events(limit=limit))


@st.cache_data(ttl=5, show_spinner=False)
def load_symptoms(limit: int = 200) -> pd.DataFrame:
    return pd.DataFrame(database.get_symptoms(limit=limit))


@st.cache_data(ttl=3, show_spinner=False)
def load_device_status() -> list[dict]:
    return database.get_device_status()


@st.cache_data(ttl=3, show_spinner=False)
def load_users() -> list[dict]:
    database.init_db()
    return database.get_users()


@st.cache_data(ttl=3, show_spinner=False)
def load_team_risk() -> list[dict]:
    return database.get_user_risk()


@st.cache_data(ttl=4, show_spinner=False)
def load_open_alerts() -> list[dict]:
    return database.get_open_alerts()


@st.cache_data(ttl=5, show_spinner=False)
def load_alert_history(limit: int = 100) -> pd.DataFrame:
    return pd.DataFrame(database.get_alert_history(limit=limit))


@st.cache_data(ttl=5, show_spinner=False)
def load_symptom_logs(limit: int = 100) -> pd.DataFrame:
    return pd.DataFrame(database.get_symptom_logs(limit=limit))


@st.cache_data(ttl=10, show_spinner=False)
def load_sensor_config(limit: int = 20) -> pd.DataFrame:
    return pd.DataFrame(database.get_sensor_config(limit=limit))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def device_online() -> tuple[bool, dict]:
    """A device counts as online if it's been seen within the TTL window."""
    statuses = load_device_status()
    now = datetime.now(timezone.utc).timestamp()
    for s in statuses:
        try:
            last = datetime.fromisoformat(s["last_seen"]).timestamp()
            if now - last <= config.DEVICE_OFFLINE_TTL_SECONDS:
                return True, s
        except (KeyError, ValueError):
            continue
    # Fall back to the freshest telemetry row.
    tel = load_telemetry(limit=1)
    if len(tel):
        last = tel.iloc[-1].get("ts_received")
        try:
            ts = datetime.fromisoformat(last).timestamp()
            if now - ts <= config.DEVICE_OFFLINE_TTL_SECONDS:
                return True, tel.iloc[-1].to_dict()
        except (TypeError, ValueError):
            pass
    return False, {}


def parse_df(df: pd.DataFrame) -> pd.DataFrame:
    """Add a parsed UTC time column from ISO ts_received for plotting."""
    if df.empty:
        return df
    df = df.copy()
    df["time"] = pd.to_datetime(df["ts_received"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Page renderers
# ---------------------------------------------------------------------------
def page_overview():
    online, latest = device_online()
    tel = parse_df(load_telemetry(limit=1))
    if len(tel):
        last = tel.iloc[-1]
        val = float(last.get("filtered_adc") or 0)
        level = str(last.get("exposure") or "LOW")
        sim = bool(last.get("simulation"))
        buzzer = bool(last.get("buzzer"))
        rssi = last.get("wifi_rssi")
        updated = str(last.get("time"))
    else:
        val, level, sim, buzzer, rssi, updated = 0.0, "—", None, False, None, "no data yet"

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Current reading", f"{val:.1f}  adc",
              delta=("simulated" if sim else "real sensor") if sim is not None else "",
              delta_color="off")
    badge = {"HIGH": "🔴 HIGH", "MODERATE": "🟠 MODERATE", "LOW": "🟢 LOW"}.get(level, level)
    c2.metric("Exposure level", badge)
    c3.metric("Device", "🟢 online" if online else "🔴 offline",
              delta="last seen " + (str(latest.get("last_seen", ""))[:16].replace("T", " ")
                                   if online else "no contact"), delta_color="off")
    c4.metric("Buzzer", "ON" if buzzer else "OFF")
    c5.metric("Wi-Fi RSSI", f"{rssi} dBm" if rssi not in (None, -99) else "—")

    st.caption(f"Last updated: {updated}  ·  readings are uncalibrated ADC "
               f"counts, not ppm / PM2.5 / AQI.")

    if not online:
        st.warning("The device hasn't been seen recently. It may be offline or "
                   "Wi-Fi/MQTT disconnected. Local buzzer alerts on the ESP32 "
                   "still operate without a connection.")

    # Per-user risk strip (the same shared reading, evaluated per profile).
    users = load_users()
    risks = {r["user_id"]: r for r in load_team_risk()}
    present = [u for u in users if u.get("present")]
    if present:
        st.subheader("Team risk (present users)")
        cols = st.columns(len(present))
        for i, u in enumerate(present):
            lvl = (risks.get(u["id"]) or {}).get("level", "CLEAR")
            with cols[i]:
                st.markdown(
                    f"<div style='border:1px solid {_level_color(lvl)};"
                    f"border-radius:10px;padding:10px;text-align:center'>"
                    f"<div><b>{u['name']}</b></div>"
                    f"<div style='font-size:1.4rem'>"
                    f"<span style='color:{_level_color(lvl)}'>{lvl}</span>"
                    f"</div>"
                    f"<div>≥{u['high_threshold']:.0f} = HIGH</div></div>",
                    unsafe_allow_html=True)
    else:
        st.caption("No present users. Add people and mark them present on the "
                   "**Users** page to see per-user risk.")


def page_live():
    tel = parse_df(load_telemetry(limit=600))
    if tel.empty:
        st.info("No telemetry yet. Start the ESP32 and the MQTT service.")
        return

    # Thresholds from the most recent telemetry / device standard
    profs = load_profiles()
    std = next((p for p in profs if p["name"] == "Standard"), profs[0]) if profs else None
    mod_t = std["moderate_threshold"] if std else config.DEFAULT_MODERATE_THRESHOLD
    high_t = std["high_threshold"] if std else config.DEFAULT_HIGH_THRESHOLD

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        vertical_spacing=0.08,
                        subplot_titles=("Filtered vs raw ADC",
                                        "Exposure level (color)"))
    x = tel["time"]
    fig.add_trace(go.Scatter(x=x, y=tel["filtered_adc"], name="filtered",
                             line=dict(color="#4f8cff", width=2.5)), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=tel["raw_adc"], name="raw",
                             line=dict(color="#9fb3d1", width=1, dash="dot"),
                             opacity=0.7), 1, 1)
    fig.add_hline(y=mod_t, line=dict(color="#f5a524", dash="dash"),
                  annotation_text=f"MODERATE {mod_t:.0f}", row=1, col=1)
    fig.add_hline(y=high_t, line=dict(color="#ff5c5c", dash="dash"),
                  annotation_text=f"HIGH {high_t:.0f}", row=1, col=1)
    # level as a stepped band
    fig.add_trace(go.Scatter(x=x, y=tel["exposure"].astype("category"),
                             name="level", line=dict(width=2, shape="s")), 2, 1)
    fig.update_yaxes(categoryorder=["LOW", "MODERATE", "HIGH"],
                     tickvals=["LOW", "MODERATE", "HIGH"], row=2, col=1)
    fig.update_layout(height=560, legend=dict(orientation="h", y=1.08))
    st.plotly_chart(fig, use_container_width=True)

    # recent history table (newest first)
    hist = tel.tail(20)[["time", "raw_adc", "filtered_adc", "exposure",
                          "buzzer", "simulation"]].copy()
    hist["time"] = hist["time"].dt.strftime("%H:%M:%S")
    st.subheader("Recent readings")
    st.dataframe(hist.iloc[::-1], use_container_width=True, height=320)


def page_predictive():
    tel = parse_df(load_telemetry(limit=600))
    if tel.empty:
        st.info("Not enough telemetry to predict. Wait for more readings.")
        return

    profs = load_profiles()
    sel = st.selectbox("Predict for profile",
                       [p["name"] for p in profs], index=1)
    profile = next(p for p in profs if p["name"] == sel)
    std = next((p for p in profs if p["name"] == "Standard"), profs[0])

    tsec = np.arange(len(tel), dtype=float) * 2.0   # approx 2 s cadence
    y = tel["filtered_adc"].astype(float).to_numpy()
    pred = analytics.predict_threshold_crossing(
        tsec, y, profile["high_threshold"], "HIGH",
        horizon_seconds=float(profile.get("warning_lead_seconds",
                                          analytics.HORIZON_SECONDS)))

    m1, m2, m3 = st.columns(3)
    m1.metric("Reading now", f"{pred.current:.1f}")
    m2.metric("Slope", f"{pred.slope_per_second:+.3f} /s")
    if pred.seconds_to_cross is not None:
        m3.metric(f"Est. time to HIGH ({profile['high_threshold']:.0f})",
                  f"{pred.seconds_to_cross:.0f} s")
    else:
        m3.metric("Est. time to HIGH", "not approaching")

    if pred.early_warning:
        st.error(f"EARLY WARNING: trend indicates a crossing of the HIGH "
                 f"threshold in about {pred.seconds_to_cross:.0f} s. "
                 f"(environmental forecast — not a health prediction)")
    elif not pred.available:
        st.info(f"No prediction: {pred.reason}")
    else:
        st.success(f"Stable: {pred.reason}")

    # actual vs predicted overlay
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=tel["time"], y=y, name="actual (filtered)",
                             line=dict(color="#4f8cff", width=2)))
    # project the fitted line forward to the horizon (extrapolate the x-axis
    # by seconds so the dashed forecast sits right after the last sample)
    tr = analytics.rolling_linear_regression(tsec, y)
    if tr.ok and len(tel):
        last_time = tel["time"].iloc[-1]
        fut_s = np.linspace(0.0, pred.horizon_seconds, 20)
        # tsec is in seconds relative to the last sample (slope_per_second),
        # so the forecast value at +fut_s seconds is:
        fut_y = tr.slope_per_second * (fut_s) + y[-1]
        fig.add_trace(go.Scatter(
            x=last_time + pd.to_timedelta(fut_s, unit="s"),
            y=fut_y, name="predicted (extrapolated)",
            line=dict(color="#ff5c5c", dash="dash", width=2)))
    fig.add_hline(y=profile["high_threshold"], line=dict(color="#ff5c5c",
                 dash="dot"), annotation_text="profile HIGH")
    fig.update_layout(height=380, title="Actual vs predicted readings",
                     legend=dict(orientation="h", y=1.1))
    st.plotly_chart(fig, use_container_width=True)

    # three-way warning comparison
    cmpres = analytics.compare_warnings(
        current=pred.current, t_values=tsec, y_values=y,
        device_standard={"high": std["high_threshold"],
                         "moderate": std["moderate_threshold"]},
        profile=profile)
    s = cmpres["signals"]
    b1, b2, b3 = st.columns(3)
    b1.metric("Fixed-threshold", "🚨 alert" if s["fixed"] else "quiet")
    b2.metric("Predictive early", "🚨 alert" if s["predictive"] else "quiet")
    b3.metric(f"Personalized ({sel})", "🚨 alert" if s["personalized"] else "quiet")
    st.caption("The three signals are environmental forecasts from the same "
               "data. They do not predict asthma outcomes.")


def page_profiles():
    profs = load_profiles()
    database.init_db()
    st.subheader("Sensitivity profiles")
    st.caption("Rule-based, manually configured thresholds in uncalibrated "
               "ADC units. Tune them to each user's baseline. These are not "
               "learned models.")
    for p in profs:
        st.markdown(f"**{p['name']}**  ·  MOD ≥ {p['moderate_threshold']:.0f}  "
                    f"HIGH ≥ {p['high_threshold']:.0f}  ·  hyst "
                    f"{p['hysteresis']:.0f}")
        if p.get("notes"):
            st.caption(p["notes"])

    st.divider()
    with st.form("new_profile"):
        st.subheader("Create / update a profile")
        name = st.text_input("Profile name", placeholder="e.g. Priya")
        col = st.columns(4)
        mod = col[0].number_input("MODERATE threshold", 50.0, 300.0,
                                  config.DEFAULT_MODERATE_THRESHOLD, 5.0)
        high = col[1].number_input("HIGH threshold", 100.0, 400.0,
                                   config.DEFAULT_HIGH_THRESHOLD, 5.0)
        hyp = col[2].number_input("Hysteresis", 0.0, 50.0,
                                  config.DEFAULT_HYSTERESIS, 1.0)
        lead = col[3].number_input("Warning lead (s)", 10.0, 120.0,
                                   float(analytics.HORIZON_SECONDS), 5.0)
        notes = st.text_input("Notes", "")
        ok = st.form_submit_button("Save profile")
        if ok:
            if high > mod:
                database.set_profile(name, mod, high, hyp, 0.3, notes)
                # also store the warning lead as a note-ish field
                st.success(f"Saved '{name}'.")
                load_profiles.clear()
            else:
                st.error("HIGH must be greater than MODERATE.")


def page_history():
    tel = parse_df(load_telemetry(limit=3000))
    if tel.empty:
        st.info("No history yet.")
        return
    span_hours = st.slider("Look-back window (hours)", 0, 24, 3, 1)
    now = datetime.now(timezone.utc)
    cutoff = now - pd.Timedelta(hours=span_hours)
    df = tel[tel["time"] >= cutoff].copy() if len(tel) else tel

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["time"], y=df["filtered_adc"],
                             name="filtered", mode="lines",
                             line=dict(color="#4f8cff")))
    fig.add_hline(y=config.DEFAULT_HIGH_THRESHOLD, line=dict(dash="dot"))
    fig.update_layout(height=360, title="Filtered readings over time")
    st.plotly_chart(fig, use_container_width=True)

    # exposure duration
    std = {p["name"]: p for p in load_profiles()}
    durations = analytics.compute_exposure_duration(
        [r for r in df.to_dict("records")],
        {"moderate": config.DEFAULT_MODERATE_THRESHOLD,
         "high": config.DEFAULT_HIGH_THRESHOLD},
        cadence_seconds=2.0)
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("LOW time", f"{durations['LOW'] / 60:.1f} min")
    d2.metric("MODERATE time", f"{durations['MODERATE'] / 60:.1f} min")
    d3.metric("HIGH time", f"{durations['HIGH'] / 60:.1f} min")
    d4.metric("Samples", f"{durations['sample_count']}")

    ev = load_events(limit=100)
    st.subheader("Recorded warning events")
    st.dataframe(ev, use_container_width=True, height=240)

    if not df.empty:
        csv = df[["time", "raw_adc", "filtered_adc", "exposure", "buzzer",
                  "simulation"]].to_csv(index=False)
        st.download_button("Download CSV", csv,
                           file_name="airsense_history.csv",
                           mime="text/csv")


def page_symptoms():
    profs = load_profiles()
    with st.form("symptom_form"):
        st.subheader("Record a symptom / irritation")
        profile = st.selectbox("Profile", [p["name"] for p in profs])
        text = st.text_area("What did you notice? (e.g. chest tightness, "
                            "coughing, eye irritation)")
        severity = st.select_slider("Severity",
                                    options=[1, 2, 3, 4, 5], value=3)
        if st.form_submit_button("Log symptom"):
            if text.strip():
                database.add_symptom(profile, text.strip(), int(severity))
                load_symptoms.clear()
                st.success("Symptom logged with a timestamp.")
            else:
                st.warning("Please describe the symptom.")

    sym = load_symptoms(limit=200)
    if sym.empty:
        st.info("No symptom logs yet.")
    else:
        st.subheader("Symptom log vs sensor readings")
        fig = go.Figure()
        tel = parse_df(load_telemetry(limit=2000))
        if len(tel):
            fig.add_trace(go.Scatter(x=tel["time"], y=tel["filtered_adc"],
                                     name="sensor (filtered)",
                                     mode="lines", opacity=0.4,
                                     line=dict(color="#4f8cff")))
        sym2 = sym.copy()
        sym2["time"] = pd.to_datetime(sym2["ts"], utc=True)
        fig.add_trace(go.Scatter(x=sym2["time"], y=sym2["severity"],
                                 mode="markers", name="symptom severity",
                                 marker=dict(size=10, color="#ff5c5c")))
        fig.update_layout(height=360)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(sym, use_container_width=True, height=240)
        st.caption("Symptom logs are self-reported and timestamped. They "
                   "can be correlated with sensor readings to explore "
                   "personal sensitivity, but this is observational, not "
                   "diagnostic.")


def _level_color(level: str) -> str:
    return {"HIGH": "#ff5c5c", "MODERATE": "#f5a524",
            "LOW": "#f4e04d", "CLEAR": "#4fbf8f"}.get(level, "#9fb3d1")


def page_team_risk():
    """Simultaneous per-user risk from the latest shared reading."""
    users = load_users()
    risks = {r["user_id"]: r for r in load_team_risk()}
    open_alerts = load_open_alerts()
    tel = parse_df(load_telemetry(limit=1))

    st.subheader("Live team risk")
    if not users:
        st.info("No users yet. Add people on the **Users** page first.")
        return

    if len(tel):
        last = tel.iloc[-1]
        st.caption(f"Latest shared reading: **{float(last.get('filtered_adc') or 0):.1f}** "
                   f"({ 'simulated' if last.get('simulation') else 'real sensor' } ADC) · "
                   f"{str(last.get('time'))}")
    else:
        st.caption("No readings yet — per-user risk updates as telemetry arrives.")

    cols = st.columns(min(len(users), 4))
    for i, u in enumerate(users):
        r = risks.get(u["id"], {})
        level = r.get("level") or "CLEAR"
        present = bool(u.get("present"))
        with cols[i % len(cols)]:
            card = ("🟢 " if present else "⚪ ") + u["name"]
            st.markdown(f"### {card}")
            st.markdown(f"Risk: **<span style='color:{_level_color(level)}'>"
                        f"{level.upper()}</span>**")
            st.write(f"Last value: {r.get('last_value', '—')}  ·  "
                    f"Source: {r.get('data_source', '—')}")
            open_for_user = [a for a in open_alerts if a["user_id"] == u["id"]]
            if open_for_user:
                st.markdown(f"🚨 Open alert: **{open_for_user[0]['level']}**")
            elif not present:
                st.caption("Absent — not evaluated.")

    # Shared buzzer policy state
    team_high = [u["name"] for u in users
                 if risks.get(u["id"], {}).get("level") == "HIGH"
                 and bool(u.get("present"))]
    st.divider()
    st.subheader("Shared buzzer policy")
    if team_high:
        st.error("🔊 Buzzer ON — team policy: " +
                 ", ".join(team_high) + " is/are at HIGH risk.")
    else:
        st.success("Buzzer OFF — no present user at HIGH. "
                   "(ESP32 still runs its independent local HIGH fallback "
                   "if Wi-Fi/backend is down.)")

    st.markdown(f"Open personalized alerts: **{len(open_alerts)}**")
    hist = load_alert_history(limit=100)
    if not hist.empty:
        st.subheader("Personalized alert history")
        st.dataframe(hist[["name", "level", "value", "data_source",
                           "started_at", "closed_at"]],
                     use_container_width=True, height=240)


def page_users():
    """Create / edit / delete users, their thresholds, and presence."""
    users = load_users()

    st.subheader("Users")
    if users:
        for u in users:
            present = "🟢 present" if u.get("present") else "⚪ absent"
            st.markdown(
                f"**{u['name']}** · {present} · "
                f"LOW≥{u['low_threshold']:.0f} "
                f"MOD≥{u['moderate_threshold']:.0f} "
                f"HIGH≥{u['high_threshold']:.0f} "
                f"(hyst {u['hysteresis']:.0f}) · "
                f"calibration: {u.get('calibration_status','uncalibrated')}")
    else:
        st.info("No users yet.")

    st.divider()
    with st.form("new_user"):
        st.subheader("Add a user")
        name = st.text_input("Name", placeholder="e.g. Priya")
        notes = st.text_input("Notes (optional)")
        if st.form_submit_button("Create user") and name.strip():
            uid = database.create_user(name, notes)
            load_users.clear()
            st.success(f"Created '{name}' with default thresholds (id {uid}).")

    st.divider()
    st.subheader("Edit a user")
    if not users:
        st.info("Nothing to edit yet.")
        return
    pick = st.selectbox("User", [f"{u['name']} (id {u['id']})"
                                  for u in users])
    sel = users[[f"{u['name']} (id {u['id']})" for u in users].index(pick)]
    uid = sel["id"]

    c = st.columns(4)
    low = c[0].number_input("LOW threshold", 0.0, 4095.0,
                            sel["low_threshold"], 1.0)
    mod = c[1].number_input("MODERATE threshold", 0.0, 4095.0,
                            sel["moderate_threshold"], 1.0)
    high = c[2].number_input("HIGH threshold", 0.0, 4095.0,
                             sel["high_threshold"], 1.0)
    hyp = c[3].number_input("Hysteresis", 0.0, 100.0, sel["hysteresis"], 1.0)
    c2 = st.columns(3)
    lead = c2[0].number_input("Warning lead (s)", 5.0, 120.0,
                              float(sel.get("warning_lead_s") or 30.0), 5.0)
    calib = c2[1].selectbox("Calibration status",
                           ["uncalibrated", "calibrated"])
    room = c2[2].text_input("Room", value=str(sel.get("room") or ""))

    err = _validate_user_thresholds(low, mod, high, hyp)
    if err:
        st.warning(err)
    if st.button("Save thresholds"):
        database.set_user_thresholds(uid, low, mod, high, hyp,
                                     float(lead), calib)
        database.set_presence(uid, bool(sel.get("present")), room or None)
        load_users.clear()
        st.success(f"Saved thresholds for '{sel['name']}'.")

    pc = st.columns(2)
    with pc[0]:
        if st.button("Mark present", key=f"p{uid}",
                     type=("secondary" if not sel.get("present") else "primary")):
            database.set_presence(uid, True, room or None)
            load_users.clear()
            st.toast(f"{sel['name']} marked present.")
    with pc[1]:
        if st.button("Mark absent", key=f"a{uid}"):
            database.set_presence(uid, False, room or None)
            load_users.clear()
            st.toast(f"{sel['name']} marked absent.")

    st.divider()
    with st.expander("Delete a user", expanded=False):
        target = st.selectbox("User to delete", [f"{u['name']} (id {u['id']})"
                                                 for u in users])
        if st.button("Delete", key="del", type="secondary"):
            database.delete_user(int(target.split("(id ")[1].rstrip(")")))
            load_users.clear()
            st.warning("User and all their data removed.")


def _validate_user_thresholds(low, mod, high, hyp) -> Optional[str]:
    """Sensible-ordering + hysteresis rules for a user's bands.
    Returns an error string, or None if valid."""
    try:
        low, mod, high, hyp = float(low), float(mod), float(high), float(hyp)
    except (TypeError, ValueError):
        return "All thresholds must be numeric."
    if not (0 <= low <= 4095 and 0 <= mod <= 4095 and 0 <= high <= 4095):
        return "Thresholds must be within [0, 4095] ADC range."
    if not (low < mod < high):
        return "Bands must be ordered: LOW < MODERATE < HIGH."
    if hyp < 0:
        return "Hysteresis must be >= 0."
    if hyp > 0.5 * (high - mod):
        return "Hysteresis is too large for the MOD->HIGH gap; it will " \
               "over-debounce the bands."
    return None


def page_user_symptoms():
    """Timestamped symptom logs linked to the sensor window they occurred in."""
    users = load_users()
    if not users:
        st.info("Add a user first on the **Users** page.")
        return

    with st.form("user_symptom"):
        st.subheader("Log a symptom (linked to recent sensor window)")
        uc = st.columns(3)
        uname = uc[0].selectbox("User", [f"{u['name']}" for u in users])
        sev = uc[1].select_slider("Severity", options=[1, 2, 3, 4, 5],
                                  value=3)
        window_min = uc[2].number_input("Sensor look-back window (min)",
                                        5.0, 600.0, 15.0, 5.0)
        text = st.text_area("What did they notice?",
                           placeholder="e.g. mild cough, eye irritation")
        submit = st.form_submit_button("Log symptom")
        if submit:
            u = next(u for u in users if u["name"] == uname)
            stats = database.sensor_window_stats(float(window_min))
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            ws = (now - timedelta(minutes=float(window_min))).isoformat()
            database.add_symptom_log(
                u["id"], int(sev), text.strip(),
                window_stats=stats, window_start=ws,
                window_end=now.isoformat(),
                note=f"looked back {float(window_min):.0f} min")
            load_symptom_logs.clear()
            st.success(f"Logged for {uname}. Sensor window: "
                      f"{stats.get('mean')} mean over {stats.get('count')} "
                      f"readings ({stats.get('source')}).")

    logs = load_symptom_logs(limit=100)
    if logs.empty:
        st.info("No symptom logs yet.")
    else:
        st.subheader("Symptom logs (linked sensor context)")
        show = logs[["name", "severity", "text", "window_start",
                     "window_end", "window_stats"]].copy()
        show["window_stats"] = show["window_stats"].map(
            lambda s: s or "{}")
        st.dataframe(show, use_container_width=True, height=260)
        st.caption("Each log stores the sensor-window statistics it was "
                   "recorded against (count/min/mean/max and whether that "
                   "window was real or simulated data). Observational, not "
                   "diagnostic.")


def page_settings():
    online, latest = device_online()
    st.subheader("Device status")
    st.write(f"**State:** {'🟢 online' if online else '🔴 offline'}  ·  "
             f"**Last seen:** {latest.get('last_seen', '—')}  ·  "
             f"**Simulated:** {'yes' if latest.get('simulation') else 'no'}")

    profs = load_profiles()
    std = next((p for p in profs if p["name"] == "Standard"),
               profs[0]) if profs else None
    if std is None:
        st.info("No profiles yet.")
        return

    st.subheader("Push device configuration over MQTT")
    st.caption("Validated before sending; the device rejects unsafe values "
               "on its own. Broker: "
               f"`{config.MQTT_HOST}:{config.MQTT_PORT}`.")
    c = st.columns(3)
    mod = c[0].number_input("MODERATE threshold", 0.0, 4095.0,
                            std["moderate_threshold"], 1.0)
    high = c[1].number_input("HIGH threshold", 0.0, 4095.0,
                             std["high_threshold"], 1.0)
    hyp = c[2].number_input("Hysteresis", 0.0, 100.0, std["hysteresis"], 1.0)
    c2 = st.columns(3)
    alpha = c2[0].number_input("EWMA alpha", 0.05, 1.0,
                               round(float(std["ewma_alpha"]), 2),
                               step=0.05, format="%.2f")
    pub = c2[1].number_input("Publish interval (ms)", 500.0, 30000.0,
                             2000.0, 500.0)
    buz = c2[2].checkbox("Enable buzzer", value=True)

    if st.button("Send config to device", type="primary"):
        try:
            cfg = {"moderate_threshold": mod, "high_threshold": high,
                   "hysteresis": hyp, "ewma_alpha": alpha,
                   "publish_interval_ms": int(pub), "buzzer_enabled": buz}
            errs = analytics_validate(cfg)
            if errs:
                st.error("Rejected: " + "; ".join(errs))
            else:
                code = publish_to_device(cfg)
                st.success(f"Config published (result {code}). The device "
                           f"will ACK on the config topic.")
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not publish: {exc}")

    st.subheader("MQTT settings guidance")
    st.code(f"""# Broker (Mosquitto) on this laptop
mqtt_host={config.MQTT_HOST}
mqtt_port={config.MQTT_PORT}
topic_prefix={config.TOPIC_PREFIX}

# Topics
  {config.TOPIC_TELEMETRY}   ESP32 -> backend   (JSON telemetry)
  {config.TOPIC_STATUS}      ESP32 -> backend   (online/offline)
  {config.TOPIC_ALERTS}      ESP32 -> backend   (exposure events)
  {config.TOPIC_CONFIG}      dashboard -> ESP32  (thresholds)
  {config.TOPIC_PREFIX}cmd/buzzer   backend -> ESP32 (shared team policy)
  {config.TOPIC_LWT}        broker LWT          (disconnection detection)
""")

    st.subheader("Sensor provenance (real vs simulated, calibration)")
    sc = load_sensor_config(limit=20)
    if sc.empty:
        st.info("No provenance recorded yet. A row is logged each time the "
                "data source (real/simulated) is observed by the backend.")
    else:
        st.dataframe(sc[["ts", "device_id", "data_source", "sensor_model",
                         "calibration_status", "raw_units"]],
                     use_container_width=True, height=200)
    st.caption("Readings are raw, uncalibrated ADC counts — never report as "
               "ppm / PM2.5 / AQI. Real and simulated datasets are kept "
               "separable via the `simulation` flag and these provenance rows.")

    st.divider()
    st.subheader("Personalization model status")
    from backend import ml_adapter
    p = ml_adapter.load_personalizer()
    meta = ml_adapter.describe_personalizer(p)
    st.write(f"**Active personalizer:** {meta['name']} — {meta['note']}")
    st.caption("Transfer learning / learned personalization is **not** in "
               "use. The `user_calibration` schema is reserved for a future "
               "model; until a trained model with evaluation evidence "
               "exists, all personalization is rule-based thresholds and is "
               "labelled as such.")


def analytics_validate(cfg: dict) -> list[str]:
    from backend.mqtt_service import validate_config
    return validate_config(cfg)


def publish_to_device(cfg: dict) -> int:
    """Publish config using a short-lived paho client (dashboard is a
    separate process from the long-running service)."""
    import paho.mqtt.client as paho
    client = paho.Client(client_id=config.MQTT_CLIENT_ID + "-dash")
    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)
    client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    import json
    info = client.publish(config.TOPIC_CONFIG, json.dumps(cfg), qos=0)
    info.wait_for_publish(timeout=5.0)
    client.disconnect()
    return info.rc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    st.markdown("## 🌫️ AirSense")
    st.caption("IoT environmental risk monitoring & early-warning dashboard "
               "— readings are uncalibrated ADC counts, not AQI or clinical "
               "values.")

    page = st.sidebar.radio(
        "View",
        ["Overview", "Team Risk", "Live Monitoring", "Predictive Analytics",
         "Personalized Profiles", "Exposure History",
         "Symptom Feedback", "User Symptom Logs", "Users",
         "Device Settings"])

    {"Overview": page_overview,
     "Team Risk": page_team_risk,
     "Live Monitoring": page_live,
     "Predictive Analytics": page_predictive,
     "Personalized Profiles": page_profiles,
     "Exposure History": page_history,
     "Symptom Feedback": page_symptoms,
     "User Symptom Logs": page_user_symptoms,
     "Users": page_users,
     "Device Settings": page_settings}[page]()

    # auto-refresh
    with st.expander("Auto-refresh", expanded=False):
        if st.button("Refresh now"):
            st.cache_data.clear()
            st.rerun()
    st.sidebar.caption("Auto-refresh: data cached ≤5 s. Poll the broker to "
                       "see live ESP32 updates.")


if __name__ == "__main__":
    main()

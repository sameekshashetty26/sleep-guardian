"""
Sleep Guardian — Live Dashboard Server
========================================
Runs a Flask server that:
  1. Receives real sensor data from the ESP32 (Wi-Fi POST /api/esp32 or USB serial)
  2. Processes it through the DSA pipeline
  3. Serves live readings to the browser (/api/live polling, /api/stream SSE)
  4. Saves completed sessions and generates a PDF report for each one
  5. Serves the dashboard at http://localhost:5000

There is no simulated data: every value shown comes from an ESP32 reading.

Usage:
    python server.py                  → ESP32 over Wi-Fi (POST /api/esp32)
    python server.py --hardware       → ESP32 over USB serial (115200 baud)
    python server.py --hardware --port COM3

ESP32 Wi-Fi JSON (POST /api/esp32):
    {"sound": 61.2, "pressure": 523.4, "weight_g": 523.4,
     "position": "TURNED LEFT", "accel_magnitude": 1.02, "roll": 8.5, "pitch": -2.1,
     "live_sound": "SOUND DETECTED", "sound_event": "SNORING",
     "snoring": true, "sleep_talking": false, "classification_id": 12,
     "classification_seconds_remaining": 35, "sound_events": 8, "vibration": true}

    live_sound        — immediate microphone state (QUIET / SOUND DETECTED)
    sound_event       — LAST COMPLETED 60-second classification on the ESP32
    classification_id — increases by one each time the ESP32 completes a window
    vibration         — controlled by the ESP32 only; the server just reports it
"""

import io
import json
import os
import sys
import time
import threading
from collections import deque
from flask import Flask, Response, send_file, jsonify, request

import main as pipeline              # pipeline.event_log etc. are replaced on every new session
from main import process_reading, generate_dashboard_data, connect_serial, read_serial_line

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_FILE = os.path.join(BASE_DIR, "dashboard_data.json")
HISTORY_FILE = os.path.join(BASE_DIR, "session_history.json")

SERIAL_BAUD = 115200                 # Serial.begin(115200) in the ESP32 sketch
STALE_AFTER_S = 5.0                  # no reading for this long → ESP32 shown as disconnected
AUTO_SAVE_AFTER_S = 30.0             # disconnected this long → session saved to History automatically
MIN_SESSION_S = 60                   # shorter sessions are not saved
WINDOW_S = 60                        # length of one ESP32 classification window

app = Flask(__name__)

latest_reading = {}
latest_actions = []
recent_actions = deque(maxlen=100)   # (seq, timestamp, text) so fast ESP32 posts never drop log lines
recent_readings = deque(maxlen=900)  # (seq, reading) — every real reading, for the live graph
action_seq = 0
reading_seq = 0
timestamp = 0
recording = False
last_saved = None                    # {"session_id", "duration_minutes", "auto"} of the newest save
input_mode = "esp32"                 # "esp32" (Wi-Fi) or "serial" — chosen at startup
data_source = "none"                 # "serial" | "esp32" once a reading has arrived
last_update = 0.0                    # wall-clock time of the last reading
last_processed = 0.0                 # wall-clock time the DSA pipeline last ran
lock = threading.RLock()



def new_session_stats():
    """Real per-session statistics, accumulated from readings the pipeline processed.
    Stored with the saved session so its report never needs invented values."""
    return {
        "start_time": None,
        "last_time": None,
        "sources": set(),
        "readings": 0,
        "sound_sum": 0.0,
        "sound_peak": None,
        "weight_sum": 0.0,
        "weight_count": 0,
        "vibration_count": 0,
        "classifications": {},       # sound_event → number of completed 60 s windows
        "position_counts": {},       # ESP32 position text → readings in that position
        "position_changes": 0,       # changes between consecutive ESP32 positions
        "last_position": None,
        "move_sum": 0.0,             # movement activity = |accel_magnitude - 1.0|
        "move_peak": None,
        "move_count": 0,
        "windows": [],               # every completed ESP32 window: {start, end, duration, event}
    }


session_stats = new_session_stats()

# Defaults for every field the ESP32 may send. Missing fields fall back to
# these so old payloads (sound/pressure/position/accel only) keep working.
ESP32_DEFAULTS = {
    "sound": 0.0,
    "pressure": 0.0,
    "weight_g": None,
    "position": "unknown",
    "accel_magnitude": 0.0,
    "roll": None,
    "pitch": None,
    "live_sound": "QUIET",
    "sound_event": "NO SIGNIFICANT SOUND",
    "snoring": False,
    "sleep_talking": False,
    "classification_seconds_remaining": None,
    "sound_events": 0,
    "vibration": False,
}
NUMERIC_FIELDS = ("sound", "pressure", "weight_g", "accel_magnitude", "roll", "pitch",
                  "classification_seconds_remaining", "sound_events", "classification_id")
BOOL_FIELDS = ("snoring", "sleep_talking", "vibration")

# Fields of a reading that the dashboard needs (live readouts + graph)
READING_FIELDS = ("sound", "pressure", "weight_g", "position", "accel_magnitude", "roll", "pitch",
                  "live_sound", "sound_event", "snoring", "sleep_talking", "classification_id",
                  "classification_seconds_remaining", "sound_events", "vibration")


def _to_number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _to_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def normalize_reading(raw):
    """Copy the reading, coerce types, and fill defaults for missing fields."""
    reading = dict(raw)
    for key in NUMERIC_FIELDS:
        if key in reading:
            reading[key] = _to_number(reading[key])
    for key in BOOL_FIELDS:
        if key in reading:
            reading[key] = _to_bool(reading[key])

    # weight_g and pressure are the same load-cell value; fill one from the other
    if reading.get("pressure") is None and reading.get("weight_g") is not None:
        reading["pressure"] = reading["weight_g"]
    if reading.get("weight_g") is None and reading.get("pressure") is not None:
        reading["weight_g"] = reading["pressure"]

    has_event = reading.get("sound_event") is not None
    for key, default in ESP32_DEFAULTS.items():
        if reading.get(key) is None:
            reading[key] = default

    reading["live_sound"] = str(reading["live_sound"]).strip().upper()
    reading["sound_event"] = str(reading["sound_event"]).strip().upper()
    # The snoring / sleep-talking flags follow the ESP32's classification text
    # exactly, so SNORING can never tick Sleep talking (or the other way round).
    if has_event:
        reading["snoring"] = reading["sound_event"] == "SNORING"
        reading["sleep_talking"] = reading["sound_event"] == "SLEEP TALKING"
    if reading["classification_seconds_remaining"] is not None:
        reading["classification_seconds_remaining"] = int(reading["classification_seconds_remaining"])
    if reading.get("classification_id") is not None:
        reading["classification_id"] = int(reading["classification_id"])
    reading["sound_events"] = int(reading["sound_events"] or 0)
    return reading


def _new_classification(prev, cur):
    """True when the ESP32 completed a 60 s window between two readings."""
    prev_id, cur_id = prev.get("classification_id"), cur.get("classification_id")
    if prev_id is not None and cur_id is not None:
        return cur_id > prev_id

    # Sketch without classification_id: the window countdown wraps from its
    # end back to ~59 s. Leaving the pillow also resets the countdown (and the
    # classification), so only count it while weight stays on the pillow.
    on_pillow = (prev.get("weight_g") or 0) > 0 and (cur.get("weight_g") or 0) > 0
    if not on_pillow:
        return False
    prev_rem = prev.get("classification_seconds_remaining")
    cur_rem = cur.get("classification_seconds_remaining")
    if prev_rem is not None and cur_rem is not None and prev_rem <= 10 and cur_rem >= prev_rem + 30:
        return True
    return cur["sound_event"] != prev.get("sound_event")


EVENT_LOG_TYPES = {"SNORING": "snoring", "SLEEP TALKING": "sleep_talking"}


def _classification_actions(prev, cur):
    """Log lines for ESP32-side events: completed classification, vibration on/off."""
    actions = []
    if not prev:
        return actions

    if _new_classification(prev, cur):
        label = cur["sound_event"]
        actions.append(f"60 s window classified as {label} "
                       f"({prev.get('sound_events', 0)} sound events)")
        if recording:
            pipeline.event_log.append(timestamp, EVENT_LOG_TYPES.get(label, "sound_classification"), label)
            counts = session_stats["classifications"]
            counts[label] = counts.get(label, 0) + 1
            start = max(0, timestamp - WINDOW_S)
            session_stats["windows"].append(
                {"start": start, "end": timestamp, "duration": timestamp - start, "event": label})

    if cur["vibration"] and not prev.get("vibration"):
        actions.append(f"Vibration motor ON after {cur['sound_event']}")
        if recording:
            pipeline.event_log.append(timestamp, "vibration_triggered",
                             f"ESP32 motor after {cur['sound_event']}")
            session_stats["vibration_count"] += 1
    elif not cur["vibration"] and prev.get("vibration"):
        actions.append("Vibration motor OFF")
    return actions


def _record_stats(reading, source, now):
    """Accumulate real session statistics for one processed reading."""
    s = session_stats
    if s["start_time"] is None:
        s["start_time"] = now
    s["last_time"] = now
    s["sources"].add(source)
    s["readings"] += 1
    s["sound_sum"] += reading["sound"]
    s["sound_peak"] = reading["sound"] if s["sound_peak"] is None else max(s["sound_peak"], reading["sound"])
    if reading.get("weight_g") is not None:
        s["weight_sum"] += reading["weight_g"]
        s["weight_count"] += 1


# Positions the ESP32 sends when the MPU was not read (nobody on the pillow / I2C failure)
NO_POSITION = ("", "UNKNOWN", "MPU ERROR")


def _record_motion(raw, reading):
    """
    Position and movement statistics from one ESP32 reading (call with lock held).
    Only readings where the ESP32 actually read the MPU count: someone on the
    pillow and a real position. Leaving the pillow does not count as a position
    change, like resetSleepData() on the ESP32.
    """
    s = session_stats
    pos = str(reading.get("position") or "").strip().upper()
    if (reading.get("weight_g") or 0) <= 0 or pos in NO_POSITION:
        s["last_position"] = None
        return
    s["position_counts"][pos] = s["position_counts"].get(pos, 0) + 1
    if s["last_position"] is not None and pos != s["last_position"]:
        s["position_changes"] += 1
    s["last_position"] = pos

    if raw.get("accel_magnitude") is not None:
        # At rest the MPU reads 1.0 (gravity only); the deviation is the movement
        move = abs(reading["accel_magnitude"] - 1.0)
        s["move_sum"] += move
        s["move_count"] += 1
        s["move_peak"] = move if s["move_peak"] is None else max(s["move_peak"], move)


def ingest_reading(raw, source):
    """
    Single entry point for every real reading (serial or ESP32 Wi-Fi).
    Live fields update immediately on every call; the DSA pipeline runs at
    most once per second because its windows assume 1 Hz samples.
    """
    global latest_reading, latest_actions, action_seq, reading_seq, timestamp
    global data_source, last_update, last_processed

    reading = normalize_reading(raw)
    now = time.time()

    with lock:
        # only compare against a recent previous reading from the same source
        same_stream = data_source == source and now - last_update < STALE_AFTER_S * 6
        actions = _classification_actions(latest_reading, reading) if same_stream else []

        if recording:
            _record_motion(raw, reading)

        if recording and now - last_processed >= 0.95:
            # classify_sound=False: snoring / sleep talking come from the ESP32 only
            actions += process_reading(timestamp, reading, classify_sound=False)
            _record_stats(reading, source, now)
            last_processed = now
            timestamp += 1

        reading["timestamp"] = timestamp
        latest_reading = reading
        data_source = source
        last_update = now
        reading_seq += 1
        sample = {k: reading.get(k) for k in READING_FIELDS}
        sample.update(seq=reading_seq, received_at=round(now, 3), source=source)
        recent_readings.append(sample)
        if actions:
            latest_actions = actions
            for text in actions:
                action_seq += 1
                recent_actions.append((action_seq, timestamp, text))
        else:
            latest_actions = []


def sensor_loop_hardware(port=None):
    """Read real ESP32 readings over USB serial, as fast as the ESP32 prints them."""
    while True:
        try:
            ser = connect_serial(port, baud=SERIAL_BAUD)
            time.sleep(2)
            ser.reset_input_buffer()
            print("  Live sensor recording started...\n")
            while True:
                # The sketch prints log lines too; read_serial_line keeps only its JSON lines.
                reading = read_serial_line(ser)
                if reading is not None:
                    ingest_reading(reading, "serial")
        except (Exception, SystemExit) as e:   # unplugged / port busy / no port: shown as disconnected
            print(f"  Serial error: {e}. Retrying in 3 s...")
            time.sleep(3)


def esp32_connected(now=None):
    """True while readings keep arriving (call with lock held)."""
    now = time.time() if now is None else now
    return bool(last_update) and now - last_update < STALE_AFTER_S


def session_seconds():
    """Wall-clock length of the current session, first to last reading (call with lock held)."""
    s = session_stats
    if s["start_time"] is None:
        return 0.0
    return s["last_time"] - s["start_time"]


def save_block_reason():
    """Why the current session cannot be saved, or None if it can (call with lock held)."""
    if not recording:
        return "Recording is not running."
    if not session_stats["readings"]:
        return "No ESP32 data in this session yet."
    if session_seconds() < MIN_SESSION_S:
        return (f"Record at least 1 minute of real ESP32 data before saving "
                f"({int(session_seconds())} s so far).")
    return None


def build_live_payload(since_seq=0, since_reading=0):
    """Shared payload for /api/live and /api/stream (call with lock held)."""
    now = time.time()
    connected = esp32_connected(now)
    # A disconnected ESP32 must not look live: its last values are withheld.
    r = latest_reading if connected else {}
    return {
        "reading": r,
        "actions": latest_actions if connected else [],
        "position": pipeline.position_tracker.get_current_position(),
        "timestamp": timestamp,
        "minutes": timestamp // 60,
        "seconds": timestamp % 60,
        # log lines with sequence numbers so the browser can skip ones it has shown
        "recent_actions": [
            {"seq": seq, "timestamp": ts, "text": text}
            for seq, ts, text in recent_actions if seq > since_seq
        ],
        "action_seq": action_seq,
        # every real ESP32 reading since the browser's last one, for the live graph
        "readings": [s for s in recent_readings if s["seq"] > since_reading],
        "reading_seq": reading_seq,
        "connected": connected,
        "input_mode": input_mode,
        "source": data_source,
        "recording": recording,
        "last_saved": last_saved,
        "session_seconds": round(session_seconds()),
        "auto_save_after": AUTO_SAVE_AFTER_S,
        "save_blocked_reason": save_block_reason(),
        "last_update_age": round(now - last_update, 1) if last_update else None,
        # ESP32 fields, flattened for convenience (also inside "reading")
        "sound": r.get("sound"),
        "pressure": r.get("pressure"),
        "weight_g": r.get("weight_g"),
        "raw_position": r.get("position"),
        "accel_magnitude": r.get("accel_magnitude"),
        "roll": r.get("roll"),
        "pitch": r.get("pitch"),
        "live_sound": r.get("live_sound"),
        "sound_event": r.get("sound_event"),
        "snoring": r.get("snoring", False),
        "sleep_talking": r.get("sleep_talking", False),
        "classification_id": r.get("classification_id"),
        "classification_seconds_remaining": r.get("classification_seconds_remaining"),
        "sound_events": r.get("sound_events"),
        "vibration": r.get("vibration", False),
    }


def _sound_section(windows, label):
    """Dashboard-shaped snoring / sleep-talking summary built from ESP32 windows."""
    events = [{"start": w["start"], "end": w["end"], "duration": w["duration"]}
              for w in windows if w["event"] == label]
    total = sum(e["duration"] for e in events)
    return {
        "total_events": len(events),
        "total_duration_seconds": total,
        "total_duration_minutes": round(total / 60, 1),
        "events": events,
    }


def build_session_dashboard():
    """Session summary in which snoring / sleep talking are the ESP32's own classifications."""
    with lock:
        dashboard = generate_dashboard_data()
        windows = list(session_stats["windows"])
    dashboard["snoring"] = _sound_section(windows, "SNORING")
    dashboard["sleep_talking"] = _sound_section(windows, "SLEEP TALKING")
    return dashboard


def load_history():
    try:
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
        return history if isinstance(history, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "dashboard.html"))


@app.route("/dashboard_data.json")
def dashboard_json():
    """Report of the last saved real session. Files not tied to a saved session are ignored."""
    try:
        with open(DASHBOARD_FILE, "r") as f:
            dashboard = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        dashboard = None
    saved_ids = {s.get("session_id") for s in load_history()}
    if not isinstance(dashboard, dict) or dashboard.get("session_id") not in saved_ids:
        return jsonify({"error": "No completed sessions yet."}), 404
    resp = jsonify(dashboard)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/session_history.json")
def history_json():
    return jsonify(load_history())


@app.route("/api/esp32", methods=["POST"])
def esp32_ingest():
    """Receive one JSON reading from the ESP32 over Wi-Fi."""
    if input_mode == "serial":
        # The same reading also arrives over USB; taking both would count it twice.
        return jsonify({"status": "ignored", "message": "server is reading the ESP32 over USB serial"})
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "expected a JSON object"}), 400
    ingest_reading(data, "esp32")
    return jsonify({"status": "ok", "timestamp": timestamp})


@app.route("/api/live")
def live_data():
    """Return current sensor reading as JSON."""
    since = request.args.get("since", default=0, type=int)
    since_reading = request.args.get("since_reading", default=0, type=int)
    with lock:
        data = build_live_payload(since, since_reading)
    return jsonify(data)


@app.route("/api/stream")
def stream():
    """Server-Sent Events stream for live updates."""
    def generate():
        last_update_seen = -1
        last_seq = 0
        last_reading = 0
        while True:
            with lock:
                if last_update != last_update_seen:
                    data = build_live_payload(last_seq, last_reading)
                    last_update_seen = last_update
                    last_seq = action_seq
                    last_reading = reading_seq
                    yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/summary")
def summary():
    """Return current session summary for dashboard."""
    return jsonify(build_session_dashboard())


def save_session(auto=False):
    """
    Write the current session to History and start a fresh one.
    Returns (session, dashboard), or (None, reason) when it cannot be saved.
    The next ESP32 reading starts the new session automatically.
    """
    global session_stats, timestamp, last_processed, last_saved

    with lock:
        reason = save_block_reason()
        if reason:
            return None, reason
        stats = dict(session_stats, sources=sorted(session_stats["sources"]))
        dashboard = build_session_dashboard()

        history = load_history()
        start_time = stats["start_time"]
        end_time = stats["last_time"]        # the last real reading, not the moment of saving
        duration_s = max(0.0, end_time - start_time)
        session_id = max((s.get("session_id", 0) for s in history), default=0) + 1
        dashboard["session_id"] = session_id
        dashboard["duration_minutes"] = round(duration_s / 60, 1)
        # position changes as reported by the ESP32 (the state machine skips left→right turns)
        dashboard["position"]["total_transitions"] = stats["position_changes"]
        counts = stats["position_counts"]
        most_common = max(counts, key=counts.get) if counts else None
        total_pos = sum(counts.values())

        new_session = {
            "session_id": session_id,
            "date": time.strftime("%Y-%m-%d", time.localtime(start_time)),
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
            "duration_minutes": round(duration_s / 60, 1),
            "saved_by": "auto (ESP32 disconnected)" if auto else "Stop & save",
            "data_source": stats["sources"],
            "sleep_quality": dashboard.get("sleep_quality", "N/A"),
            "snoring": {
                "total_events": dashboard["snoring"]["total_events"],
                "total_duration_minutes": dashboard["snoring"]["total_duration_minutes"],
            },
            "sleep_talking": {
                "total_events": dashboard["sleep_talking"]["total_events"],
                "total_duration_minutes": dashboard["sleep_talking"]["total_duration_minutes"],
            },
            "position": {
                "total_transitions": stats["position_changes"],
                "most_common": most_common,
                "share_percent": {p: round(100 * n / total_pos, 1) for p, n in counts.items()},
            },
            "movement": {
                "avg_activity": round(stats["move_sum"] / stats["move_count"], 3) if stats["move_count"] else None,
                "peak_activity": round(stats["move_peak"], 3) if stats["move_peak"] is not None else None,
                "samples": stats["move_count"],
            },
            "restlessness_percentage": dashboard["restlessness_percentage"],
            "insomnia": {
                "score": dashboard["insomnia"]["score"],
                "level": dashboard["insomnia"]["level"],
                "episodes": dashboard["insomnia"]["episodes"],
            },
            "esp32": {
                "classified_windows": stats["classifications"],
                "vibration_activations": stats["vibration_count"],
            },
            "sensors": {
                "readings": stats["readings"],
                "sound_avg_db": round(stats["sound_sum"] / stats["readings"], 1) if stats["readings"] else None,
                "sound_peak_db": round(stats["sound_peak"], 1) if stats["sound_peak"] is not None else None,
                "weight_avg_g": round(stats["weight_sum"] / stats["weight_count"], 1) if stats["weight_count"] else None,
            },
        }
        history.append(new_session)

        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
        with open(DASHBOARD_FILE, "w") as f:
            json.dump(dashboard, f, indent=2)

        # fresh session: new statistics, new DSA pipeline, session clock back to 0
        session_stats = new_session_stats()
        pipeline.reset_pipeline()
        timestamp = 0
        last_processed = 0.0
        last_saved = {"session_id": session_id, "duration_minutes": new_session["duration_minutes"],
                      "auto": auto}

    print(f"  Session #{session_id} saved ({new_session['duration_minutes']} min, "
          f"{'auto after disconnect' if auto else 'Stop & save'})")
    return new_session, dashboard


def auto_save_loop():
    """Save the session to History once the ESP32 has been silent for AUTO_SAVE_AFTER_S."""
    while True:
        time.sleep(2)
        with lock:
            silent = bool(last_update) and time.time() - last_update >= AUTO_SAVE_AFTER_S
            ready = silent and session_stats["readings"] and save_block_reason() is None
        if ready:
            save_session(auto=True)


@app.route("/api/stop", methods=["POST"])
def stop_recording():
    """Save the session to History now. Only real ESP32 sessions are saved."""
    session, dashboard = save_session(auto=False)
    if session is None:
        return jsonify({"status": "error", "message": dashboard}), 409
    return jsonify({"status": "stopped", "session": session, "dashboard": dashboard})


# ===================================================================
# PDF REPORT FOR ONE SAVED SESSION
# ===================================================================

class MiniPDF:
    """
    Minimal A4 PDF writer: text, lines, filled / outlined boxes, colours and
    Helvetica. Built in, so the report download needs no extra package.
    Units are millimetres from the top-left corner of the page.
    """
    W, H = 210.0, 297.0
    FONTS = {"": "F1", "B": "F2", "I": "F3"}

    # Standard Helvetica / Helvetica-Bold advance widths (1/1000 em) for ASCII 32..126
    _W_REG = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
              1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
              333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
              556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
    _W_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
               556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
               975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
               667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
               333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
               611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]

    def __init__(self):
        self.pages = []
        self.font, self.style, self.size = "F1", "", 10.0

    def add_page(self):
        self.pages.append([])

    def _out(self, op):
        self.pages[-1].append(op)

    @staticmethod
    def _pt(mm):
        return mm * 72 / 25.4

    @staticmethod
    def _rgb(c):
        return " ".join(f"{v / 255:.3f}" for v in c)

    def set_font(self, style="", size=10.0):
        self.style, self.font, self.size = style, self.FONTS.get(style, "F1"), float(size)

    def text_width(self, text):
        table = self._W_BOLD if self.style == "B" else self._W_REG
        units = sum(table[ord(ch) - 32] if 32 <= ord(ch) <= 126 else 556 for ch in str(text))
        return units / 1000 * self.size * 25.4 / 72

    def text(self, x, y, text, color=(33, 41, 51), align="left"):
        """Draw text with its baseline at y. align: left / right / center around x."""
        if align == "right":
            x -= self.text_width(text)
        elif align == "center":
            x -= self.text_width(text) / 2
        safe = str(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self._out(f"{self._rgb(color)} rg BT /{self.font} {self.size:g} Tf "
                  f"{self._pt(x):.2f} {self._pt(self.H - y):.2f} Td ({safe}) Tj ET")

    def rect(self, x, y, w, h, fill=None, stroke=None, width=0.25):
        ops = []
        if fill:
            ops.append(f"{self._rgb(fill)} rg")
        if stroke:
            ops.append(f"{self._rgb(stroke)} RG {self._pt(width):.2f} w")
        ops.append(f"{self._pt(x):.2f} {self._pt(self.H - y - h):.2f} {self._pt(w):.2f} {self._pt(h):.2f} re")
        ops.append("B" if fill and stroke else "f" if fill else "S")
        self._out(" ".join(ops))

    def line(self, x1, y1, x2, y2, color=(200, 205, 212), width=0.25):
        self._out(f"{self._rgb(color)} RG {self._pt(width):.2f} w {self._pt(x1):.2f} {self._pt(self.H - y1):.2f} m "
                  f"{self._pt(x2):.2f} {self._pt(self.H - y2):.2f} l S")

    def output(self):
        objs = {1: "<< /Type /Catalog /Pages 2 0 R >>",
                3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                4: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
                5: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique /Encoding /WinAnsiEncoding >>"}
        kids = []
        n = 6
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1", "replace")
            objs[n] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
            objs[n + 1] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self._pt(self.W):.2f} {self._pt(self.H):.2f}] "
                           f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {n} 0 R >>")
            kids.append(f"{n + 1} 0 R")
            n += 2
        objs[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"

        out = bytearray(b"%PDF-1.4\n")
        offsets = {}
        for num in sorted(objs):
            offsets[num] = len(out)
            body = objs[num] if isinstance(objs[num], bytes) else objs[num].encode("latin-1")
            out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % n
        for num in range(1, n):
            out += b"%010d 00000 n \n" % offsets[num]
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%EOF\n" % (n, xref)
        return bytes(out)


def _pdf_text(value):
    """Core PDF fonts are Latin-1 only; replace anything outside it."""
    return (str(value).replace("—", "-").replace("–", "-").replace("→", "->")
            .encode("latin-1", "replace").decode("latin-1"))


# Report palette: muted teal accent plus one muted tone per sound class.
# Every colour stays mid-to-dark so the report still reads in grayscale.
INK = (33, 41, 51)
MUTED = (100, 112, 125)
BORDER = (208, 217, 224)
PANEL = (246, 249, 250)
TEAL = (17, 110, 122)
TEAL_TINT = (229, 242, 243)
SNORE = (176, 62, 84)
TALK = (104, 78, 160)


def build_session_pdf(session):
    """
    Render one saved session as a PDF. Every value printed comes from the
    saved session record in session_history.json; anything the session did
    not record is left out rather than filled in.
    """
    pdf = MiniPDF()
    L, R = 16.0, MiniPDF.W - 16.0          # content edges
    width = R - L
    bottom = MiniPDF.H - 16.0
    state = {"y": 0.0, "page": 0}

    snore = session.get("snoring") or {}
    talk = session.get("sleep_talking") or {}
    ins = session.get("insomnia") or {}
    esp = session.get("esp32") or {}
    windows = esp.get("classified_windows") or {}
    sensors = session.get("sensors") or {}
    pos = session.get("position") or {}
    move = session.get("movement") or {}

    def fmt_minutes(m):
        if m is None:
            return None
        m = float(m)
        h, mm = divmod(int(round(m)), 60)
        return f"{h} h {mm:02d} min" if h else f"{m:g} min"

    def fmt_position(p):
        return str(p).replace("_", " ").title() if p else None

    def fmt_move(v):
        return f"{v:.2f}" if v is not None else None

    def page_frame():
        pdf.add_page()
        state["page"] += 1
        pdf.rect(8, 8, MiniPDF.W - 16, MiniPDF.H - 16, stroke=BORDER, width=0.3)
        # header band
        pdf.rect(8, 8, MiniPDF.W - 16, 24, fill=TEAL_TINT)
        pdf.rect(8, 32, MiniPDF.W - 16, 0.9, fill=TEAL)
        pdf.set_font("B", 18)
        pdf.text(L, 20.5, "SLEEP GUARDIAN", TEAL)
        pdf.set_font("", 10.5)
        pdf.text(L, 27, "Sleep Session Report", MUTED)
        pdf.set_font("B", 10.5)
        if session.get("session_id") is not None:
            pdf.text(R, 20.5, f"Session #{session['session_id']}", INK, "right")
        pdf.set_font("", 9.5)
        if session.get("date"):
            pdf.text(R, 27, _pdf_text(session["date"]), MUTED, "right")
        # footer
        pdf.set_font("", 7.5)
        pdf.text(L, MiniPDF.H - 11, _pdf_text("All values come from the saved ESP32 session record. "
                                              "Generated by Sleep Guardian on " + time.strftime("%Y-%m-%d %H:%M")),
                 MUTED)
        pdf.text(R, MiniPDF.H - 11, f"Page {state['page']}", MUTED, "right")
        state["y"] = 40.0

    def need(h):
        if state["y"] + h > bottom:
            page_frame()

    def heading(text, color=TEAL):
        need(30)                          # never leave a heading alone at the bottom of a page
        y = state["y"] + 5
        pdf.rect(L, y - 4.2, 1.4, 5.2, fill=color)
        pdf.set_font("B", 11.5)
        pdf.text(L + 3.6, y, _pdf_text(text), color)
        pdf.line(L, y + 2.4, R, y + 2.4, BORDER)
        state["y"] = y + 5.5

    def cards(items, cols=3, h=14.5):
        """Grid of small boxes: (label, value, sub, accent). Items without a value are skipped."""
        items = [it for it in items if it[1] not in (None, "")]
        if not items:
            return
        gap = 3.0
        cw = (width - gap * (cols - 1)) / cols
        for i in range(0, len(items), cols):
            need(h + gap)
            y = state["y"]
            for j, (label, value, sub, accent) in enumerate(items[i:i + cols]):
                x = L + j * (cw + gap)
                pdf.rect(x, y, cw, h, fill=PANEL, stroke=BORDER)
                pdf.rect(x, y, 1.2, h, fill=accent or TEAL)
                pdf.set_font("", 7.8)
                pdf.text(x + 4, y + 4.9, _pdf_text(label.upper()), MUTED)
                pdf.set_font("B", 12.5 if len(str(value)) <= 16 else 10.5)
                pdf.text(x + 4, y + 11.0, _pdf_text(value), INK)
                if sub:
                    pdf.set_font("", 7.5)
                    pdf.text(x + cw - 3, y + 11.0, _pdf_text(sub), MUTED, "right")
            state["y"] = y + h + gap
        state["y"] += 1.5

    def table(rows):
        """Bordered key/value table: (label, value, accent). Rows without a value are skipped."""
        rows = [r for r in rows if r[1] not in (None, "")]
        if not rows:
            return
        rh = 6.0
        need(rh * len(rows) + 4)
        y0 = state["y"]
        pdf.rect(L, y0, width, rh * len(rows), stroke=BORDER)
        for i, (label, value, accent) in enumerate(rows):
            y = y0 + i * rh
            if i % 2:
                pdf.rect(L + 0.15, y, width - 0.3, rh, fill=PANEL)
            if i:
                pdf.line(L, y, R, y, BORDER, 0.2)
            pdf.rect(L + 3, y + 1.9, 2.2, 2.2, fill=accent or TEAL)
            pdf.set_font("", 9.3)
            pdf.text(L + 8, y + 4.2, _pdf_text(label), INK)
            pdf.set_font("B", 9.8)
            pdf.text(R - 4, y + 4.2, _pdf_text(value), INK, "right")
        state["y"] = y0 + rh * len(rows) + 4.5

    def note(text):
        need(6)
        pdf.set_font("I", 7.8)
        pdf.text(L, state["y"] + 1.5, _pdf_text(text), MUTED)
        state["y"] += 5

    page_frame()

    # --- session details ---
    sources = {"esp32": "ESP32 over Wi-Fi", "serial": "ESP32 over USB serial"}
    data_source = ", ".join(sources.get(s, s) for s in session.get("data_source") or []) or None
    details = [("Started", session.get("start_time")), ("Ended", session.get("end_time")),
               ("Saved by", session.get("saved_by"))]
    details = [d for d in details if d[1]]
    if details:
        y = state["y"]
        pdf.rect(L, y, width, 9, fill=PANEL, stroke=BORDER)
        x = L + 4
        for label, value in details:
            pdf.set_font("", 8.5)
            pdf.text(x, y + 5.8, label, MUTED)
            x += pdf.text_width(label) + 2.5
            pdf.set_font("B", 8.8)
            pdf.text(x, y + 5.8, _pdf_text(value), INK)
            x += pdf.text_width(_pdf_text(value)) + 9
        state["y"] = y + 13

    # --- summary ---
    heading("Session Summary")
    cards([
        ("Sleep duration", fmt_minutes(session.get("duration_minutes")), None, TEAL),
        ("Sleep quality", session.get("sleep_quality"), None, TEAL),
        ("Snoring events", snore.get("total_events"), None, SNORE),
        ("Sleep talking events", talk.get("total_events"), None, TALK),
        ("Vibration interventions", esp.get("vibration_activations"), None, TEAL),
        ("Movement activity", fmt_move(move.get("avg_activity")), "average", TEAL),
    ])

    # --- sound analysis ---
    heading("Sound Analysis")
    sound_rows = []
    if "total_events" in snore:
        dur = snore.get("total_duration_minutes")
        sound_rows.append(("Snoring events", f"{snore['total_events']}" + (f"   ({dur} min)" if dur is not None else ""), SNORE))
    if "total_events" in talk:
        dur = talk.get("total_duration_minutes")
        sound_rows.append(("Sleep talking events", f"{talk['total_events']}" + (f"   ({dur} min)" if dur is not None else ""), TALK))
    if "classified_windows" in esp:
        # every completed ESP32 window, including the classes that did not occur
        sound_rows += [
            ("Snoring (60 s windows)", windows.get("SNORING", 0), SNORE),
            ("Sleep talking (60 s windows)", windows.get("SLEEP TALKING", 0), TALK),
            ("Noise (60 s windows)", windows.get("NOISE", 0), MUTED),
            ("No significant sound (60 s windows)", windows.get("NO SIGNIFICANT SOUND", 0), MUTED),
        ]
    if "vibration_activations" in esp:
        sound_rows.append(("Vibration activations", esp["vibration_activations"], TEAL))
    table(sound_rows)
    if "classified_windows" in esp:
        note("Snoring and sleep talking are the ESP32's own 60-second classifications.")

    # --- sleep quality / risk ---
    heading("Sleep Quality / Risk")
    cards([
        ("Sleep quality", session.get("sleep_quality"), None, TEAL),
        ("Insomnia score", f"{ins['score']} / 100" if "score" in ins else None, ins.get("level"), TEAL),
        ("Insomnia episodes", len(ins["episodes"]) if isinstance(ins.get("episodes"), list) else None, None, TEAL),
        ("Restlessness", f"{session['restlessness_percentage']}%"
         if session.get("restlessness_percentage") is not None else None, None, TEAL),
    ], cols=4, h=16.0)

    # --- movement & position (MPU6500) ---
    if move.get("samples") or pos.get("most_common"):
        heading("Movement & Position (MPU6500)")
        share = (pos.get("share_percent") or {}).get(pos.get("most_common"))
        cards([
            ("Avg movement activity", fmt_move(move.get("avg_activity")), None, TEAL),
            ("Peak movement activity", fmt_move(move.get("peak_activity")), None, TEAL),
            ("Most common position", fmt_position(pos.get("most_common")),
             f"{share:g}% of time" if share is not None else None, TEAL),
        ])
        if move.get("samples"):
            note("Movement activity = |acceleration magnitude - 1.0| from the MPU6500: 0.00 is lying still, "
                 f"higher is more movement ({move['samples']} readings).")

    # --- sensor / session information ---
    heading("Sensor / Session Information")
    cards([
        ("Position changes", pos.get("total_transitions"), None, TEAL),
        ("Readings recorded", sensors.get("readings") or None, None, TEAL),
        ("Average sound level", f"{sensors['sound_avg_db']} dB" if sensors.get("sound_avg_db") is not None else None, None, TEAL),
        ("Peak sound level", f"{sensors['sound_peak_db']} dB" if sensors.get("sound_peak_db") is not None else None, None, TEAL),
        ("Avg load-cell weight", f"{sensors['weight_avg_g']} g" if sensors.get("weight_avg_g") is not None else None, None, TEAL),
        ("Data source", data_source, None, TEAL),
    ])

    return pdf.output()


@app.route("/api/history/<int:session_id>/report")
def session_report(session_id):
    """Download the PDF report for one saved session from session_history.json."""
    session = next((s for s in load_history() if s.get("session_id") == session_id), None)
    if session is None:
        return jsonify({"error": f"Session {session_id} not found"}), 404
    pdf_bytes = build_session_pdf(session)
    filename = f"sleep-guardian-report-{session.get('date', 'session')}-{session_id}.pdf"
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)


if __name__ == "__main__":
    args = sys.argv[1:]
    use_hardware = "--hardware" in args or "--hw" in args
    if "--demo" in args or "--simulate" in args:
        print("Demo / simulated data has been removed. The dashboard only shows real ESP32 readings.")
        sys.exit(1)

    port = None
    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = args[idx + 1]

    print("=" * 60)
    print("  Sleep Guardian — Live Dashboard Server")
    print("=" * 60)

    recording = True
    threading.Thread(target=auto_save_loop, daemon=True).start()
    if use_hardware:
        input_mode = "serial"
        print(f"\n  Mode: ESP32 over USB serial ({SERIAL_BAUD} baud)")
        print("  (Close the Arduino Serial Monitor first — only one program can open the port.)")
        threading.Thread(target=sensor_loop_hardware, args=(port,), daemon=True).start()
    else:
        print("\n  Mode: ESP32 over Wi-Fi (POST /api/esp32)")

    print("  Dashboard: http://localhost:5000")
    print("  ESP32 endpoint: POST http://<laptop-ip>:5000/api/esp32\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

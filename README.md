# Sleep Guardian

A sleep monitor that detects snoring and sleep talking, tracks sleeping position and restlessness, and gently vibrates to nudge the sleeper when snoring is detected.

An ESP32 with sensors sits on the pillow and sends readings over Wi-Fi to a small Flask server running on a laptop. The server runs the readings through a data-processing pipeline and serves a live web dashboard.

```
ESP32 (sensors)  ──POST /api/esp32──▶  Flask server (laptop)  ──GET /api/live──▶  Web dashboard
```

---

## Features

- **Live monitor**: current sound state, the latest 60-second sound classification, a countdown to the next classification, vibration motor status, and live readouts for sound level, movement, weight and position.
- **Live sensor graph**: sound, position (left / normal / right steps) and weight plotted over time, with sound-detected moments and vibration periods marked. Export it as **PNG** or **CSV**.
- **Event log**: position changes, classifications, vibration on/off, restlessness and insomnia alerts as they happen.
- **Session report**: snoring and sleep-talking events, an event timeline, position breakdown and insomnia indicators. Export it as **CSV**, **JSON** or **PDF**.
- **History**: every completed real session, with a **Download** button on each row for a PDF report of that session, and the change since the previous session.
- **Real data only**: there is no simulated or demo data. Every live value, graph point, event and saved session comes from ESP32 readings.
- Light and dark mode, and works on phones.

---

## Hardware

| Part | Purpose |
|---|---|
| ESP32 | Reads the sensors, classifies sound, drives the motor, sends data over Wi-Fi |
| HX711 + load cell | Weight / pressure on the pillow (is someone lying on it) |
| MPU6500 | Movement and head position (roll, pitch, acceleration) |
| INMP441 microphone | Sound level and sound detection |
| Vibration motor | Nudge to change position after snoring is classified |

---

## Project structure

```
sleep-guardian-main/
├── server.py              Flask server: receives ESP32 data, serves the dashboard and API
├── main.py                Processing pipeline (also runs standalone as a simulation)
├── dashboard.html         The web dashboard (single file, no build step)
├── requirements.txt       Python dependencies
├── dashboard_data.json    Last saved session report (created when you save a session)
├── session_history.json   All completed sessions (starts empty)
├── dsa/                   Data structures and algorithms used by the pipeline
│   ├── circular_buffer.py     Rolling sensor history
│   ├── sliding_window.py      Movement / restlessness classification
│   ├── priority_queue.py      Alert handling by severity
│   ├── signal_correlator.py   Combines sensors by timestamp (hash map)
│   ├── event_log.py           Event history and sleep quality summary (linked list)
│   ├── peak_finder.py         Snoring pattern detection
│   ├── state_machine.py       Sleep position tracking
│   ├── sound_classifier.py    Snoring vs sleep talking (zero-crossing rate)
│   └── insomnia_detector.py   Long restless streaks (run-length encoding)
└── arduino/sleep_guardian/sleep_guardian.ino   Older serial-only sensor sketch (see note below)
```

---

## Setup

You need **Python 3.9 or newer**.

1. Open a terminal in the `sleep-guardian-main` folder (the one that contains `server.py`).

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

   This installs Flask (the server) and pyserial (only needed for USB serial mode). PDF reports are built in and need no extra package.

---

## Running

| Command | What it does |
|---|---|
| `python server.py` | **ESP32 over Wi-Fi** (normal use). Real data only; waits for the ESP32 to post. |
| `python server.py --hardware` | **ESP32 over USB serial** (115200 baud). Add `--port COM3` (or your port) to pick the port. Close the Arduino Serial Monitor first. The sketch prints its JSON line only while Wi-Fi is connected. |
| `python main.py` | Runs a 10-minute simulation in the terminal only. Never used by the server or the dashboard. |

Then open **http://localhost:5000** in a browser.

To open the dashboard from a phone on the same Wi-Fi, use `http://<laptop-ip>:5000`. On Windows, find the laptop IP with `ipconfig` (look for "IPv4 Address" under "Wi-Fi").

### Connecting the ESP32

In the ESP32 code, set the server URL to:

```
http://<laptop-ip>:5000/api/esp32
```

The laptop and the ESP32 must be on the same Wi-Fi network. The first time the ESP32 connects, Windows may ask whether to allow Python through the firewall. Allow it for **Private networks**.

### Recording and saving a session

1. Start the server (`python server.py`) and switch on the ESP32. Recording starts automatically.
2. In the morning, press **Stop & save session** on the Live monitor, then press it again to confirm. If you just switch the ESP32 off, the session is saved automatically 30 seconds after its last reading.
3. The session is added to the **History** tab, and the **Session report** tab shows its details.
4. On the History tab, press **Download** on any row to get the PDF report for that session.

The Stop & save button stays disabled, with the reason shown next to it, while the session has less than 1 minute of real ESP32 data. After every save (button or automatic) a new session starts with the next ESP32 reading, so there is no need to restart the server.

### PDF report contents

Each PDF is built only from the values stored for that session:

- Date, duration, sleep quality, start and end time
- **Sound analysis:** snoring events, sleep-talking events, how many 60-second windows the ESP32 classified as snoring, sleep talking, noise or no significant sound, and vibration activations
- **Sleep quality / risk:** insomnia score and level, insomnia episodes, restlessness
- **Sensor / session information:** position changes, readings recorded, average and peak sound level, average load-cell weight, data source

If a value was not recorded for a session, its line is left out. Nothing is estimated or filled in.

---

## ESP32 data format

The ESP32 sends one JSON object per reading to `POST /api/esp32`:

```json
{
  "sound": 61.2,
  "pressure": 523.4,
  "weight_g": 523.4,
  "position": "TURNED LEFT",
  "accel_magnitude": 1.02,
  "roll": 8.5,
  "pitch": -2.1,

  "live_sound": "SOUND DETECTED",
  "sound_event": "SNORING",
  "snoring": true,
  "sleep_talking": false,
  "classification_seconds_remaining": 35,
  "sound_events": 8,

  "vibration": true
}
```

| Field | Meaning |
|---|---|
| `sound` | Sound level in dB |
| `pressure`, `weight_g` | Load cell reading. If only one is sent, the server copies it to the other. |
| `position` | Head position text, for example `BACK`, `TURNED LEFT`, `TURNED RIGHT`, `STOMACH` |
| `accel_magnitude`, `roll`, `pitch` | Acceleration magnitude (1.00 at rest) and tilt (degrees) from the MPU6500. The dashboard and report show **Movement activity** = abs(accel_magnitude - 1.0): 0.00 is lying still |
| `live_sound` | `QUIET` or `SOUND DETECTED`: the microphone state **right now** |
| `sound_event` | Result of the **last completed** 60-second window: `SNORING`, `SLEEP TALKING`, `NOISE` or `NO SIGNIFICANT SOUND` |
| `snoring`, `sleep_talking` | Flags for the last completed classification. The server sets them from `sound_event`, so `SNORING` ticks only Snoring and `SLEEP TALKING` ticks only Sleep talking |
| `classification_id` | Goes up by one each time the ESP32 completes a 60-second window. The server uses it to log each classification exactly once |
| `classification_seconds_remaining` | Seconds until the current 60-second window is classified |
| `sound_events` | Number of sound events counted in the current window |
| `vibration` | Whether the vibration motor is running |

Every field is optional. Missing fields fall back to safe defaults, so an older ESP32 sketch that only sends `sound`, `pressure`, `position` and `accel_magnitude` still works. The ESP32 can post as often as it likes: the dashboard updates on every post, the live graph gets one point per post, and the analysis pipeline runs once per second. If no reading arrives for 5 seconds the dashboard shows the ESP32 as disconnected and blanks the live values.

Snoring and sleep talking counts in the event log, session report, History and PDF come only from the ESP32's own 60-second classifications.

### How the sound logic works

There are two separate sound states, and the dashboard shows them separately on purpose:

1. **Live detection (immediate):** `live_sound` changes the moment the microphone hears something.
2. **Classification (every 60 seconds):** the ESP32 collects sound events for a full 60-second window, then classifies the pattern. During the window, `sound_event` keeps showing the **previous** result.

**Vibration** happens only after a classification, and it is controlled entirely by the ESP32. The server and the dashboard only display it; they never trigger it.

Example: at 20 seconds into a window, the dashboard can show "Current sound: SOUND DETECTED" together with "Sound classification: NO SIGNIFICANT SOUND". That is expected. The classification changes to `SNORING` only when the window completes.

---

## API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | The dashboard |
| `/api/esp32` | POST | Receive one ESP32 reading (JSON). Returns `400` if the body is not a JSON object. |
| `/api/live` | GET | Latest reading, recent log entries, data source and how old the data is. Optional `?since=<seq>` returns only newer log entries. |
| `/api/stream` | GET | The same data as a Server-Sent Events stream |
| `/api/summary` | GET | Current session summary (same shape as `dashboard_data.json`) |
| `/api/stop` | POST | Stop recording and save the session to history. Returns `409` with a reason if it can't be saved (under 1 minute, already saved). |
| `/session_history.json` | GET | All completed sessions |
| `/api/history/<session_id>/report` | GET | PDF report for one saved session. `404` if the session doesn't exist. |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'flask'` | Run `pip install -r requirements.txt`. |
| Dashboard says "Server offline" | The server is not running. Start it with `python server.py`. |
| Stop & save button is greyed out | The reason is shown next to the button. Usually less than 1 minute of ESP32 data has been recorded. |
| Dashboard says "Waiting for ESP32" or "ESP32 disconnected" | The ESP32 is not reaching the server. Check the URL and IP in the ESP32 code, that both are on the same Wi-Fi, and the Windows firewall prompt. |
| ESP32 worked yesterday but not today | The laptop IP may have changed. Run `ipconfig` and update the ESP32 code. |
| "Address already in use" on port 5000 | Another program is using port 5000. Close it, or change `port=5000` at the bottom of `server.py`. |
| History says "No completed sessions yet." | No real session has been saved yet. Record one and press **Stop & save session**. |

---

## Note on the Arduino sketch

`arduino/sleep_guardian/sleep_guardian.ino` is the ESP32 sketch for the current hardware (HX711, MPU6500, INMP441, Wi-Fi). It posts the JSON format described above to `/api/esp32` once per second and prints the same JSON line on the Serial Monitor (115200 baud). Set `WIFI_SSID`, `WIFI_PASSWORD` and `SERVER_URL` (the laptop's IP) before uploading.

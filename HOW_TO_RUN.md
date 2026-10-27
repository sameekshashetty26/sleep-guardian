# Sleep Guardian — How to Run (step by step)

Follow the steps in order. The ESP32 sends its readings over Wi-Fi to a Python server on a laptop, and the website runs on that laptop.

```
ESP32 (sensors) ──Wi-Fi──▶ python server.py (laptop) ──▶ http://localhost:5000
```

---

## 1. What you need

1. A Windows laptop with **Python 3.10 or newer**. Get it from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"** during install.
2. The **Arduino IDE** from https://www.arduino.cc/en/software
3. The ESP32 board with the sensors wired as in step 2, and a USB data cable.
4. This project folder (the one that contains `server.py`, `dashboard.html` and the `arduino` folder).

---

## 2. Wiring (must match the pins in the sketch)

| Part | Pin on part | ESP32 pin |
|---|---|---|
| HX711 (load cell) | DT | GPIO 33 |
| | SCK | GPIO 19 |
| | VCC / GND | 3.3V / GND |
| INMP441 (microphone) | SCK | GPIO 14 |
| | WS | GPIO 15 |
| | SD | GPIO 32 |
| | L/R | GND |
| | VDD / GND | 3.3V / GND |
| MPU6500 (motion) | SDA | GPIO 21 |
| | SCL | GPIO 22 |
| | VCC / GND | 3.3V / GND |
| Vibration motor | control (through a transistor/driver) | GPIO 4 |

---

## 3. Set up the Arduino IDE (one time)

1. Open Arduino IDE → **File → Preferences** → in "Additional boards manager URLs" paste:
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
2. **Tools → Board → Boards Manager** → search **esp32** → install **"esp32 by Espressif Systems"**.
   (If the sketch later fails to compile with I2S errors, install version **2.0.17** instead.)
3. **Sketch → Include Library → Manage Libraries** → search **HX711** → install **"HX711 Arduino Library" by Bogdan Necula**.

---

## 4. Decide the Wi-Fi network

The ESP32 and the laptop must be on the **same Wi-Fi**. The ESP32 only works with **2.4 GHz** Wi-Fi.

**Option A — laptop hotspot (easiest, this is what the sketch is set up for)**

1. Windows **Settings → Network & internet → Mobile hotspot**.
2. Press **Edit**: set a network name and password, and set **Band = 2.4 GHz**. Turn the hotspot **On**.
3. The laptop's address on its own hotspot is normally **192.168.137.1**. Check it: open Command Prompt and run
   ```
   ipconfig
   ```
   Look for the adapter "Local Area Connection* …" → **IPv4 Address**.

**Option B — normal home Wi-Fi router**

1. Connect the laptop to the router's 2.4 GHz Wi-Fi.
2. Run `ipconfig` and note the **IPv4 Address** under "Wireless LAN adapter Wi-Fi" (for example `192.168.1.25`).

---

## 5. Edit and upload the ESP32 sketch

1. Open `arduino/sleep_guardian/sleep_guardian.ino` in the Arduino IDE.
2. Change these three lines at the top to **your** network and **your** laptop IP from step 4:
   ```cpp
   const char* WIFI_SSID = "YOUR_WIFI_NAME";
   const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
   const char* SERVER_URL = "http://192.168.137.1:5000/api/esp32";   // <laptop IP>:5000/api/esp32
   ```
3. Plug in the ESP32. **Tools → Board → esp32 → ESP32 Dev Module**. **Tools → Port →** the COM port that appeared.
4. Press **Upload** (→ arrow). If it stays on "Connecting…", hold the **BOOT** button on the ESP32 until the upload starts.
5. Open **Tools → Serial Monitor** and set the speed to **115200 baud**.
6. While it starts up:
   - **Remove all weight from the load cell** (it tares for 5 seconds).
   - **Keep the room silent** for 6 seconds (microphone calibration).
7. You should see `WiFi CONNECTED!` and the ESP32 IP. Until the server is running (step 6) it will show `HTTP ERROR`; that is expected.

---

## 6. Start the Python server on the laptop

1. Open **Command Prompt** (or PowerShell) **in the project folder** (the folder with `server.py`):
   ```
   cd "C:\path\to\sleep-guardian-main"
   ```
2. Install the packages (one time):
   ```
   python -m pip install -r requirements.txt
   ```
3. Start the server:
   ```
   python server.py
   ```
4. If Windows Firewall asks, press **Allow access** (tick Private **and** Public networks). Without this the ESP32 cannot reach the laptop.
5. Leave this window open. To stop the server later press **Ctrl + C**.

> If `python` is not found, use `py` instead: `py -m pip install -r requirements.txt` and `py server.py`.

---

## 7. Open the website

1. In a browser on the laptop open **http://localhost:5000**
2. Check that it is working:
   - Arduino Serial Monitor shows `HTTP response: 200`.
   - The website top bar shows **Recording · ESP32 · Wi-Fi**.
   - The numbers on the Live monitor match the latest `Sending data:` line in the Serial Monitor.
3. (Optional) From a phone on the same Wi-Fi open `http://<laptop IP>:5000`.

---

## 8. Using it

1. **Put weight on the pillow** — the ESP32 only reads posture and sound while the load cell reads more than 0 g.
2. **Live monitor** shows sound, position, weight and the live graph. Every 60 seconds the ESP32 classifies the sound as SNORING, SLEEP TALKING, NOISE or NO SIGNIFICANT SOUND, and the matching box is ticked.
3. **Saving a session** (needs at least 1 minute of data):
   - Press **Stop & save session**, then press it **again** to confirm, **or**
   - Just switch the ESP32 off — the session is saved automatically **30 seconds** after the last reading.
   A new session starts by itself with the next reading. No need to restart the server.
4. **History** tab → **Download** gives the PDF report of that session. **Session report** shows the last saved session.

---

## 9. Problems and fixes

| What you see | Fix |
|---|---|
| Serial Monitor: `WiFi CONNECTION FAILED` | Wrong name/password in the sketch, or the Wi-Fi is 5 GHz. Use 2.4 GHz. |
| Serial Monitor: `HTTP ERROR: connection refused` | Server not running, wrong IP in `SERVER_URL`, or the firewall blocked it (allow Python in Windows Firewall). |
| Website: "Waiting for ESP32" / "ESP32 disconnected" | Same as above — the ESP32 is not reaching the server. |
| Website: "Server offline" | The `python server.py` window was closed. Start it again. |
| `No module named 'flask'` | Run `python -m pip install -r requirements.txt` again. |
| "Address already in use" / port 5000 busy | Close the other program using port 5000 (or an older server window). |
| Stop & save is greyed out | The reason is shown next to it — usually less than 1 minute of data yet. |
| Sensors show 0 / nothing changes | Nothing on the load cell: the ESP32 skips reading posture and sound when the weight is 0 g. |
| Upload fails | Use a data USB cable, pick the right COM port, hold **BOOT** while uploading. |

---

## Quick summary (every time)

1. Turn on the hotspot / Wi-Fi.
2. Power the ESP32 (keep load cell empty and room quiet for ~10 s at start).
3. In the project folder: `python server.py`
4. Open **http://localhost:5000**

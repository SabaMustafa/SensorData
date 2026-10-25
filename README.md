# Queen Bee Watch — Version 2

Live hive camera with YOLO queen detection, plus Raspberry Pi sensor data
(temperature, humidity, weight, ...) on a web dashboard. Built API-first, so the
same server will also serve the future mobile app.

Version 1 stays untouched in `../bee_hive_app` (including all training scripts
and datasets). This folder only contains what the application needs.

## How it fits together

```
 Raspberry Pi                              This PC (Flask server)                 Clients
 ────────────                              ──────────────────────                 ───────
 camera_stream.py ── MJPEG video ───────►  camera thread + YOLO  ── /video_feed ─► Web dashboard
 sensor_sender.py ── POST /api/v1/... ──►  API + SQLite database ── /api/v1 ────► Mobile app (later)
```

* If the Pi camera is offline, the dashboard shows the demo video and keeps
  trying to reconnect (every 30 s).
* If the server is offline, the Pi keeps readings in `pi/unsent_readings.jsonl`
  and sends them later — no data is lost.

## Project layout

```
run.py                  start the server
config.py               all settings (values come from .env)
app/
  __init__.py           create_app(): database, default hive, camera thread
  models.py             database tables: Hive, SensorReading, QueenEvent
  api/routes.py         JSON API  (/api/v1/...)
  dashboard/routes.py   web pages (/, /hives/<id>, /mobile)
  streaming/routes.py   video stream (/video_feed/<id>, /snapshot/<id>.jpg)
  services/camera.py    live camera reader, reconnect, demo fallback
  services/detector.py  YOLO queen detector + box tracker
  templates/, static/   HTML, CSS, JavaScript
models/                 best.pt (queen model), bee_count_best.pt (for later)
data/demo/              DemoVideo.mp4
pi/                     scripts that run ON the Raspberry Pi
tests/                  automatic API tests (pytest)
```

## 1. Run on this PC

```bash
cd C:\Users\kgj6801\PycharmProjects\bee_hive_app_v2
.venv\Scripts\activate
copy .env.example .env        # then edit .env
python run.py
```

Open http://localhost:5000. Without a camera configured you will see the demo video.

**Test without the Pi** — in a second terminal, send simulated sensor data:

```bash
.venv\Scripts\python pi\sensor_sender.py --driver simulated --interval 10
```

**Run the tests:**

```bash
.venv\Scripts\python -m pytest
```

If you set the project up again from scratch:
`py -3.13 -m venv .venv` then `.venv\Scripts\pip install -r requirements.txt`.

## 2. Connect the Raspberry Pi

1. **Find the IP addresses.** On the PC run `ipconfig` (e.g. `192.168.1.20`).
   On the Pi run `hostname -I` (e.g. `192.168.1.50`). Give both a fixed IP in your router if possible.
2. **Copy the `pi` folder to the Pi**, e.g. to `/home/pi/hive/pi`.
3. **Install on the Pi:**
   ```bash
   python3 -m venv --system-site-packages ~/hive-venv
   ~/hive-venv/bin/pip install -r ~/hive/pi/requirements-pi.txt
   ```
4. **Camera:** test it, then open `http://192.168.1.50:8000/stream.mjpg` in a browser.
   ```bash
   ~/hive-venv/bin/python ~/hive/pi/camera_stream.py --camera usb      # USB webcam
   ~/hive-venv/bin/python ~/hive/pi/camera_stream.py --camera picam    # Pi Camera Module
   ```
5. **Sensors:** `cp .env.example .env` in the `pi` folder and set `SERVER_URL`,
   `API_KEY` and `SENSOR_DRIVER`. Put your existing sensor-reading code into
   `read_custom()` in `sensor_sender.py` and use `SENSOR_DRIVER=custom`
   (or use the ready `dht22` / `bme280` drivers). Test once:
   ```bash
   ~/hive-venv/bin/python ~/hive/pi/sensor_sender.py --once
   ```
6. **On the PC**, set in `.env`:
   `CAMERA_URL=http://192.168.1.50:8000/stream.mjpg` and restart `python run.py`.
7. **Start automatically at boot** (on the Pi): see the instructions at the top of
   `pi/systemd/hive-camera.service` and `pi/systemd/hive-sensors.service`.

**Windows firewall:** the first time `run.py` starts, allow Python on *private networks*,
otherwise the Pi cannot reach port 5000.

## API reference (v1)

| Method | Endpoint | Purpose |
|---|---|---|
| GET  | `/api/v1/health` | Server is running |
| GET  | `/api/v1/hives` | List hives |
| GET  | `/api/v1/hives/1/status` | Latest sensors, queen status, camera state |
| POST | `/api/v1/hives/1/readings` | Pi sends readings (header `X-API-Key`) |
| GET  | `/api/v1/hives/1/readings?hours=24` | History for charts |
| GET  | `/api/v1/hives/1/queen/events?hours=24` | When the queen was seen on the live camera |
| POST | `/api/v1/detect` | Queen detection on one image (JSON base64 or file upload) |

Reading format (single object, a list, or `{"readings": [...]}`):

```json
{"temperature_c": 34.6, "humidity_pct": 58.2, "weight_kg": 41.3,
 "recorded_at": "2026-10-25T14:30:00Z", "sound_db": 52}
```

Unknown fields such as `sound_db` are stored too, so new sensors need no server change.

## Notes and next steps

* **Phone camera page** (`/mobile`): phones allow camera access only over HTTPS.
  Put `cert.pem` and `key.pem` into a `certs/` folder (the ones from version 1 work)
  and `run.py` will use HTTPS automatically.
* **API key:** change `DEFAULT_HIVE_API_KEY` in `.env` *before the first start*
  (it is saved when the database is created).
* **Database:** SQLite file `instance/beehive.db`. Moving to PostgreSQL later
  only needs `DATABASE_URL` in `.env`.
* Planned later: user login, alerts (temperature / queen not seen), more hives,
  database migrations (Flask-Migrate), on-phone detection (export `best.pt` to TFLite/CoreML).

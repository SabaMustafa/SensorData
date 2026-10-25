#!/usr/bin/env python3
"""
Raspberry Pi sensor sender (runs ON THE PI).

Reads the hive sensors every INTERVAL seconds and sends them to the Flask API:

    POST {SERVER_URL}/api/v1/hives/{HIVE_ID}/readings   (header X-API-Key)

If the server can't be reached, readings are saved in a local file and sent
later, so no data is lost while the PC is off or the Wi-Fi drops.

Settings come from pi/.env (copy .env.example), e.g.
    SERVER_URL=http://192.168.1.20:5000
    HIVE_ID=1
    API_KEY=dev-hive-1-key
    SENSOR_DRIVER=simulated      # simulated | dht22 | bme280 | custom
    INTERVAL_SECONDS=30

Run:  python3 sensor_sender.py            (forever)
      python3 sensor_sender.py --once     (one reading, for testing)

The "simulated" driver also works on Windows, so the whole system can be
tested before the Pi is connected.
"""
import argparse
import json
import math
import os
import random
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    """Minimal .env reader (no extra package needed on the Pi)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)


# ---------------------------------------------------------------------
# Sensor drivers: each returns a dict of values.
# Known keys: temperature_c, humidity_pct, weight_kg.
# Any other key (e.g. "sound_db", "outside_temp_c") is stored too.
# ---------------------------------------------------------------------
def read_simulated():
    """Realistic fake values (brood nest ~34-35 °C) for testing without hardware."""
    t = time.time()
    day = math.sin(t / 86400 * 2 * math.pi)
    return {
        "temperature_c": round(34.5 + 0.6 * day + random.uniform(-0.2, 0.2), 2),
        "humidity_pct": round(60 + 5 * day + random.uniform(-1, 1), 1),
        "weight_kg": round(42 + 0.3 * day + random.uniform(-0.05, 0.05), 2),
    }


_dht = None


def read_dht22():
    # pip install adafruit-circuitpython-dht ; sudo apt install libgpiod2
    global _dht
    import adafruit_dht
    import board

    if _dht is None:
        pin = getattr(board, os.getenv("DHT_PIN", "D4"))
        _dht = adafruit_dht.DHT22(pin)

    # DHT sensors often fail single reads; try a few times
    for _ in range(5):
        try:
            t, h = _dht.temperature, _dht.humidity
            if t is not None and h is not None:
                return {"temperature_c": round(t, 2), "humidity_pct": round(h, 1)}
        except RuntimeError:
            pass
        time.sleep(2)
    raise RuntimeError("DHT22 read failed")


_bme = None


def read_bme280():
    # pip install adafruit-circuitpython-bme280 ; enable I2C with raspi-config
    global _bme
    import board
    from adafruit_bme280 import basic as adafruit_bme280

    if _bme is None:
        _bme = adafruit_bme280.Adafruit_BME280_I2C(board.I2C(), address=int(os.getenv("BME280_ADDRESS", "0x76"), 16))
    return {
        "temperature_c": round(_bme.temperature, 2),
        "humidity_pct": round(_bme.humidity, 1),
        "pressure_hpa": round(_bme.pressure, 1),
    }


def read_custom():
    """
    >>> PUT YOUR EXISTING SENSOR CODE HERE <<<
    Copy the reading part of your current Pi dashboard script into this
    function and return the values as a dict, for example:

        return {"temperature_c": 34.6, "humidity_pct": 58.2, "weight_kg": 41.3}
    """
    raise NotImplementedError("Add your sensor code to read_custom() in sensor_sender.py")


DRIVERS = {
    "simulated": read_simulated,
    "dht22": read_dht22,
    "bme280": read_bme280,
    "custom": read_custom,
}


# ---------------------------------------------------------------------
# Offline buffer (one JSON reading per line)
# ---------------------------------------------------------------------
class Buffer:
    def __init__(self, path, max_items=50000):
        self.path = path
        self.max_items = max_items

    def load(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def save(self, items):
        items = items[-self.max_items:]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
        os.replace(tmp, self.path)


class Sender:
    def __init__(self, server_url, hive_id, api_key, buffer):
        self.url = f"{server_url.rstrip('/')}/api/v1/hives/{hive_id}/readings"
        self.headers = {"X-API-Key": api_key}
        self.buffer = buffer

    def send(self, reading):
        pending = self.buffer.load() + [reading]

        # Send oldest first, up to 500 per request (API limit)
        while pending:
            batch = pending[:500]
            try:
                r = requests.post(self.url, json={"readings": batch}, headers=self.headers, timeout=10)
            except requests.RequestException as e:
                print(f"Server unreachable ({e.__class__.__name__}), {len(pending)} reading(s) buffered")
                break

            if r.status_code == 201:
                pending = pending[len(batch):]
                print(f"Sent {len(batch)} reading(s): {batch[-1]}")
            elif r.status_code in (400, 401, 404):
                # Not fixable by retrying (bad key, wrong hive id, invalid data)
                print(f"Server rejected data: {r.status_code} {r.text}")
                if r.status_code == 400:
                    pending = pending[len(batch):]  # drop invalid data
                break
            else:
                print(f"Server error {r.status_code}, will retry")
                break

        self.buffer.save(pending)


def main():
    load_env(os.path.join(HERE, ".env"))

    p = argparse.ArgumentParser(description="Send hive sensor readings to the server")
    p.add_argument("--once", action="store_true", help="send one reading and exit")
    p.add_argument("--driver", default=os.getenv("SENSOR_DRIVER", "simulated"), choices=DRIVERS)
    p.add_argument("--interval", type=float, default=float(os.getenv("INTERVAL_SECONDS", "30")))
    p.add_argument("--server", default=os.getenv("SERVER_URL", "http://127.0.0.1:5000"))
    p.add_argument("--hive-id", type=int, default=int(os.getenv("HIVE_ID", "1")))
    p.add_argument("--api-key", default=os.getenv("API_KEY", "dev-hive-1-key"))
    args = p.parse_args()

    read = DRIVERS[args.driver]
    sender = Sender(args.server, args.hive_id, args.api_key,
                    Buffer(os.path.join(HERE, "unsent_readings.jsonl")))
    print(f"Driver: {args.driver} | server: {sender.url} | every {args.interval:g} s")

    while True:
        started = time.time()
        try:
            values = read()
            values["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            sender.send(values)
        except Exception as e:
            print("Sensor read failed:", e)

        if args.once:
            break
        time.sleep(max(1.0, args.interval - (time.time() - started)))


if __name__ == "__main__":
    main()

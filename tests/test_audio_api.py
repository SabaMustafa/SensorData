import os
import shutil

import pytest

from config import TestConfig


@pytest.fixture(autouse=True)
def clean_audio_dir():
    shutil.rmtree(TestConfig.AUDIO_DIR, ignore_errors=True)
    yield
    shutil.rmtree(TestConfig.AUDIO_DIR, ignore_errors=True)


def test_resent_readings_are_not_doubled(client, auth):
    body = {"readings": [{"temperature_c": 30, "recorded_at": "2026-10-25T00:00:00Z"}]}
    assert client.post("/api/v1/hives/1/readings", headers=auth, json=body).json["saved"] == 1
    again = client.post("/api/v1/hives/1/readings", headers=auth, json=body).json
    assert again["saved"] == 0 and again["duplicates"] == 1


def test_audio_analysis_saved_once(client, auth):
    result = {"filename": "20261025_125215.WAV", "recorded_at": "2026-10-24T23:52:15Z",
              "rms_dbfs": -36.1, "dominant_hz": 199.9, "sample_rate_hz": 16000,
              "peaks": [{"hz": 199.9, "db": -51.3}]}
    res = client.post("/api/v1/hives/1/audio-analysis", headers=auth, json={"results": [result]})
    assert res.status_code == 201 and res.json["saved"] == 1
    res = client.post("/api/v1/hives/1/audio-analysis", headers=auth, json={"results": [result]})
    assert res.json["saved"] == 0

    rows = client.get("/api/v1/hives/1/audio-analysis?hours=24000").json["results"]
    assert rows[0]["dominant_hz"] == 199.9 and rows[0]["peaks"][0]["db"] == -51.3


def test_audio_analysis_requires_api_key(client):
    res = client.post("/api/v1/hives/1/audio-analysis", json={"filename": "a.WAV"})
    assert res.status_code == 401


def test_recording_upload_and_download(client, auth):
    wav = b"RIFF" + b"\0" * 100
    headers = {**auth, "Content-Type": "audio/wav", "X-Recorded-At": "2026-10-24T23:52:15Z"}
    res = client.put("/api/v1/hives/1/recordings/20261025_125215.WAV", headers=headers, data=wav)
    assert res.status_code == 201
    assert res.json["recording"]["size_bytes"] == len(wav)

    res = client.put("/api/v1/hives/1/recordings/20261025_125215.WAV", headers=headers, data=wav)
    assert res.status_code == 200 and res.json["status"] == "exists"

    assert client.get("/api/v1/hives/1/recordings/20261025_125215.WAV").data == wav
    listed = client.get("/api/v1/hives/1/recordings?hours=24000").json["recordings"]
    assert [r["filename"] for r in listed] == ["20261025_125215.WAV"]


def test_recording_rejects_unsafe_names_and_bad_keys(client, auth):
    res = client.put("/api/v1/hives/1/recordings/notes.txt", headers=auth, data=b"x")
    assert res.status_code == 400
    res = client.put("/api/v1/hives/1/recordings/a.WAV", headers={"X-API-Key": "no"}, data=b"x")
    assert res.status_code == 401


def test_recordings_switch_refuses_uploads_when_off(client, auth):
    assert client.get("/api/v1/hives/1/settings").json["recordings_enabled"] is True

    res = client.put("/api/v1/hives/1/settings", json={"recordings_enabled": False})
    assert res.status_code == 200 and res.json["recordings_enabled"] is False
    assert client.get("/api/v1/hives/1/status").json["audio"]["recordings_enabled"] is False

    headers = {**auth, "Content-Type": "audio/wav"}
    res = client.put("/api/v1/hives/1/recordings/20261025_130000.WAV", headers=headers, data=b"RIFF" * 50)
    assert res.status_code == 403
    assert not os.path.exists(os.path.join(TestConfig.AUDIO_DIR, "1", "20261025_130000.WAV"))

    # Analysis results are small and still accepted
    res = client.post("/api/v1/hives/1/audio-analysis", headers=auth,
                      json={"filename": "20261025_130000.WAV", "rms_dbfs": -40})
    assert res.status_code == 201

    client.put("/api/v1/hives/1/settings", json={"recordings_enabled": True})
    res = client.put("/api/v1/hives/1/recordings/20261025_130000.WAV", headers=headers, data=b"RIFF" * 50)
    assert res.status_code == 201


def test_settings_rejects_bad_values(client):
    res = client.put("/api/v1/hives/1/settings", json={"recordings_enabled": "no"})
    assert res.status_code == 400


def test_recordings_refused_when_storage_full(app, client, auth):
    app.config["AUDIO_MAX_STORAGE_GB"] = 300 / 1024 ** 3  # 300 bytes
    headers = {**auth, "Content-Type": "audio/wav"}
    assert client.put("/api/v1/hives/1/recordings/a.WAV", headers=headers, data=b"x" * 200).status_code == 201
    res = client.put("/api/v1/hives/1/recordings/b.WAV", headers=headers, data=b"x" * 200)
    assert res.status_code == 507
    assert client.get("/api/v1/hives/1/settings").json["used_bytes"] == 200

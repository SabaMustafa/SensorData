from datetime import datetime, timedelta, timezone


def iso_ago(**kwargs):
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def test_health(client):
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json["status"] == "ok"


def test_default_hive_created(client):
    hives = client.get("/api/v1/hives").json["hives"]
    assert len(hives) == 1
    assert hives[0]["id"] == 1


def test_reading_requires_api_key(client):
    res = client.post("/api/v1/hives/1/readings", json={"temperature_c": 34})
    assert res.status_code == 401

    res = client.post("/api/v1/hives/1/readings", json={"temperature_c": 34},
                      headers={"X-API-Key": "wrong"})
    assert res.status_code == 401


def test_post_single_reading_and_status(client, auth):
    res = client.post("/api/v1/hives/1/readings", headers=auth,
                      json={"temperature": 34.6, "humidity": 58.2, "weight_kg": 41.3, "sound_db": 52})
    assert res.status_code == 201
    assert res.json["saved"] == 1

    status = client.get("/api/v1/hives/1/status").json
    latest = status["sensors"]["latest"]
    assert status["sensors"]["online"] is True
    assert latest["temperature_c"] == 34.6
    assert latest["humidity_pct"] == 58.2
    assert latest["extra"] == {"sound_db": 52}
    assert status["camera"]["mode"] == "disabled"


def test_post_batch_and_history(client, auth):
    readings = [{"temperature_c": 30 + i, "recorded_at": iso_ago(minutes=10 - i)} for i in range(5)]
    res = client.post("/api/v1/hives/1/readings", headers=auth, json={"readings": readings})
    assert res.status_code == 201
    assert res.json["saved"] == 5

    hist = client.get("/api/v1/hives/1/readings?hours=1").json
    assert hist["count"] == 5
    assert [p["temperature_c"] for p in hist["points"]] == [30, 31, 32, 33, 34]


def test_history_downsampling(client, auth):
    readings = [{"temperature_c": 30, "recorded_at": iso_ago(minutes=i)} for i in range(100)]
    client.post("/api/v1/hives/1/readings", headers=auth, json=readings)

    hist = client.get("/api/v1/hives/1/readings?hours=24&max_points=10").json
    assert hist["count"] == 100
    assert len(hist["points"]) == 10


def test_old_readings_outside_range(client, auth):
    client.post("/api/v1/hives/1/readings", headers=auth,
                json={"temperature_c": 30, "recorded_at": iso_ago(hours=48)})
    assert client.get("/api/v1/hives/1/readings?hours=24").json["count"] == 0


def test_invalid_readings_rejected(client, auth):
    cases = [
        {"temperature_c": 500},
        {"humidity_pct": "wet"},
        {},
        {"temperature_c": 34, "recorded_at": "not-a-date"},
        {"temperature_c": 34, "recorded_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    ]
    for body in cases:
        res = client.post("/api/v1/hives/1/readings", headers=auth, json=body)
        assert res.status_code == 400, body
        assert "error" in res.json


def test_unknown_hive(client, auth):
    assert client.get("/api/v1/hives/99/status").status_code == 404
    assert client.post("/api/v1/hives/99/readings", headers=auth, json={"temperature_c": 1}).status_code == 404


def test_detect_disabled_in_tests(client):
    res = client.post("/api/v1/detect", json={"image": "abc"})
    assert res.status_code == 503


def test_dashboard_pages(client):
    res = client.get("/")
    assert res.status_code == 302
    assert client.get("/hives/1").status_code == 200
    assert client.get("/mobile").status_code == 200

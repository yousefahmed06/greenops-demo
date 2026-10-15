"""Run from the backend folder:  python test_day2.py
Uses the FAKE carbon file so it works before the real data is ready."""
import os
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.environ["CARBON_PATH"] = str(BASE / "data" / "carbon_fake.csv")
os.environ["SCHEDULER_AUTOSTART"] = "0"
os.environ["LOG_DIR"] = tempfile.mkdtemp()
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

import pandas as pd
from fastapi.testclient import TestClient
import main

KEYS = {"id", "status", "region", "region_key", "provider", "start", "carbon_g",
        "naive_region", "naive_carbon_g", "saved_pct", "skipped", "reason",
        "started_at", "finished_at", "name"}


def deadline_in(hours):
    return (pd.Timestamp.now(tz="UTC").floor("h") + pd.Timedelta(hours=hours)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(**kw):
    base = {"name": "t", "image": "demo-job:1", "duration_hours": 2,
            "deadline": deadline_in(24), "power_kw": 0.1, "needs_gpu": False}
    base.update(kw)
    return base


with TestClient(main.app) as c:
    assert c.get("/health").json() == {"ok": True}
    assert len(c.get("/regions").json()) == 3

    # 1. flexible task -> matches the frozen decision format, saves carbon
    r = c.post("/tasks", json=task())
    d = r.json()
    assert r.status_code == 200 and set(d) == KEYS, set(d) ^ KEYS
    assert d["status"] == "Scheduled" and d["saved_pct"] > 0
    print("flexible task :", d["reason"])

    # 2. tight deadline -> only one possible start (now)
    d2 = c.post("/tasks", json=task(deadline=deadline_in(2))).json()
    now = pd.Timestamp.now(tz="UTC").floor("h").strftime("%Y-%m-%dT%H:%M:%SZ")
    assert d2["start"] == now
    print("tight deadline:", d2["reason"])

    # 3. GPU task -> poland is skipped (GPU 'Verify')
    d3 = c.post("/tasks", json=task(needs_gpu=True)).json()
    assert {"region": "poland", "reason": "GPU not confirmed"} in d3["skipped"]
    assert d3["region"] != "poland"
    print("gpu task      :", d3["region"], d3["skipped"])

    # 4. impossible deadline -> clear error
    r4 = c.post("/tasks", json=task(duration_hours=5, deadline=deadline_in(1)))
    assert r4.status_code == 422, r4.text
    print("impossible    :", r4.json()["detail"])

    # 5. bad input is rejected by validation
    assert c.post("/tasks", json=task(duration_hours=0)).status_code == 422

    # 6. list and fetch
    assert len(c.get("/tasks").json()) == 3
    assert c.get(f"/tasks/{d['id']}").json()["id"] == d["id"]
    assert c.get("/tasks/9999").status_code == 404

print("Day 2 tests passed")

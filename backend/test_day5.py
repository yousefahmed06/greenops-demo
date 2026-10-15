"""Run from the backend folder:  python test_day5.py
The whole flow with Docker replaced by a fake: samples -> decisions -> run -> summary."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.environ["CARBON_PATH"] = str(BASE / "data" / "carbon_fake.csv")
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["LOG_DIR"] = tempfile.mkdtemp()
os.environ["SCHEDULER_AUTOSTART"] = "0"
os.environ["DEMO_SPEED"] = "1"

import pandas as pd
from fastapi.testclient import TestClient
import dispatcher
import main

dispatcher.start_container = lambda cmd, log: subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(1); print('job done')"],
    stdout=log, stderr=subprocess.STDOUT)

with TestClient(main.app) as c:
    # carbon data for the chart
    carbon = c.get("/carbon").json()
    assert set(carbon) == {"sweden", "germany", "poland"} and all(len(v) == 48 for v in carbon.values())

    # the four sample tasks
    out = c.post("/demo/samples").json()
    assert [d["name"] for d in out] == ["nightly-backup", "urgent-report", "etl-pipeline", "ml-training"]
    assert not any("error" in d for d in out)
    now = main.clock.now()
    starts = {d["name"]: pd.Timestamp(d["start"]) for d in out}
    assert starts["urgent-report"] <= now, "tight deadline must start now"
    assert starts["nightly-backup"] > now, "flexible task must wait"
    gpu = out[3]
    assert {"region": "poland", "reason": "GPU not confirmed"} in gpu["skipped"] and gpu["region"] != "poland"
    print("samples        : 4 decisions, GPU filter works, one task delayed")

    # run everything: fast-forward past the last start, tick until finished
    c.post("/clock/advance", json={"hours": 40})
    end = time.time() + 20
    while time.time() < end:
        main.scheduler.tick()
        tasks = c.get("/tasks").json()
        if all(t["status"] in ("Completed", "Failed") for t in tasks):
            break
        time.sleep(0.3)
    assert [t["status"] for t in tasks] == ["Completed"] * 4, [t["status"] for t in tasks]
    print("run            : all 4 containers completed")

    # summary used by the page
    s = c.get("/summary").json()
    assert s["tasks"] == 4 and s["naive_carbon_g"] > s["carbon_g"] > 0 and s["saved_pct"] > 0
    assert abs(s["saved_pct"] - (s["naive_carbon_g"] - s["carbon_g"]) / s["naive_carbon_g"] * 100) < 0.1
    print(f"summary        : {s['carbon_g']} g vs {s['naive_carbon_g']} g naive = {s['saved_pct']}% saved")

    # reset leaves a clean demo
    c.post("/reset")
    assert c.get("/tasks").json() == [] and c.get("/summary").json()["tasks"] == 0

print("Day 5 tests passed")

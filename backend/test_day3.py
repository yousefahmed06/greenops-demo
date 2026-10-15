"""Run from the backend folder:  python test_day3.py
Docker is replaced by a fake (a tiny Python process), so this works on any laptop."""
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
os.environ["SCHEDULER_AUTOSTART"] = "0"        # the test calls tick() itself
os.environ["DEMO_SPEED"] = "1"

import pandas as pd
from fastapi.testclient import TestClient
import clock as clock_module
import dispatcher
import main
import store

calls, MODE = [], {"fail": False, "missing_docker": False}


def fake_start(cmd, log_file):
    calls.append(cmd)
    if MODE["missing_docker"]:
        raise FileNotFoundError("docker")
    code = f"import sys,time; time.sleep(1); print('job done'); sys.exit({1 if MODE['fail'] else 0})"
    return subprocess.Popen([sys.executable, "-c", code], stdout=log_file, stderr=subprocess.STDOUT)


dispatcher.start_container = fake_start


def deadline_in(hours):
    return (main.get_now() + pd.Timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(**kw):
    base = {"name": "t", "image": "demo-job:1", "duration_hours": 2,
            "deadline": deadline_in(24), "power_kw": 0.1, "needs_gpu": False}
    base.update(kw)
    return base


def wait_for(c, task_id, status, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        main.scheduler.tick()
        got = c.get(f"/tasks/{task_id}").json()
        if got["status"] == status:
            return got
        time.sleep(0.3)
    raise AssertionError(f"task {task_id} never reached {status}: {got['status']}")


# ---- the clock on its own ----
ck = clock_module.DemoClock(speed=3600)
t0 = ck.now(); time.sleep(0.5)
assert 1500 < (ck.now() - t0).total_seconds() < 2500, "speed 3600 should run 1 hour per second"
ck.advance(3); assert (ck.now() - t0) > pd.Timedelta(hours=3)
print("clock          : ok")

with TestClient(main.app) as c:
    # ---- a flexible task is delayed, then runs exactly once ----
    d = c.post("/tasks", json=task()).json()
    assert d["status"] == "Scheduled" and d["started_at"] is None
    assert pd.Timestamp(d["start"]) > main.clock.now(), "task should wait for a cleaner slot"
    main.scheduler.tick()
    assert c.get(f"/tasks/{d['id']}").json()["status"] == "Scheduled" and not calls
    print("delayed task   :", d["region"], "starts", d["start"])

    c.post("/clock/advance", json={"hours": 30})            # fast-forward past the start time
    main.scheduler.tick(); main.scheduler.tick()             # two ticks: still one start
    assert len(calls) == 1, "double start!"
    assert "region=" + d["region_key"] in calls[0] and "WORK_SECONDS=120" in calls[0]
    assert c.get(f"/tasks/{d['id']}").json()["status"] == "Running"
    done = wait_for(c, d["id"], "Completed")
    assert done["started_at"] and done["finished_at"]
    assert "job done" in c.get(f"/tasks/{d['id']}/log").json()["log"]
    print("run to the end : Completed, started", done["started_at"])

    # ---- a task with a tight deadline starts immediately ----
    d2 = c.post("/tasks", json=task(deadline=deadline_in(2))).json()
    main.scheduler.tick()
    assert c.get(f"/tasks/{d2['id']}").json()["status"] == "Running"
    wait_for(c, d2["id"], "Completed")

    # ---- a failing job is reported as Failed ----
    MODE["fail"] = True
    d3 = c.post("/tasks", json=task(deadline=deadline_in(2))).json()
    main.scheduler.tick()
    failed = wait_for(c, d3["id"], "Failed")
    MODE["fail"] = False
    print("failing job    : Failed (exit code", store.get(main.DB_PATH, d3["id"])["exit_code"], ")")

    # ---- Docker missing: clear message in the log ----
    MODE["missing_docker"] = True
    d4 = c.post("/tasks", json=task(deadline=deadline_in(2))).json()
    main.scheduler.tick()
    assert c.get(f"/tasks/{d4['id']}").json()["status"] == "Failed"
    assert "Docker" in c.get(f"/tasks/{d4['id']}/log").json()["log"]
    MODE["missing_docker"] = False
    print("no docker      : Failed with a helpful log")

    # ---- safety and validation ----
    assert c.post("/tasks", json=task(image="evil:latest")).status_code == 422
    assert c.post("/tasks", json=task(image="--privileged")).status_code == 422
    assert c.post("/clock/speed", json={"speed": 0}).status_code == 422
    assert c.post("/clock/speed", json={"speed": 360}).json()["speed"] == 360
    assert c.get("/clock").json()["speed"] == 360

    # ---- reset clears everything ----
    assert c.post("/reset").json()["ok"] and c.get("/tasks").json() == []
    print("safety + reset : ok")

    # ---- a job that was Running when the server stopped becomes Failed ----
    d5 = c.post("/tasks", json=task(deadline=deadline_in(2))).json()
    store.mark_running(main.DB_PATH, d5["id"], "2026-01-01T00:00:00Z", "x")

with TestClient(main.app) as c2:
    assert c2.get(f"/tasks/{d5['id']}").json()["status"] == "Failed"
    print("restart        : orphaned job marked Failed")

print("Day 3 tests passed")

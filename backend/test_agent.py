"""Run from the backend folder:  python test_agent.py
Tests the platform side of the agent protocol (enrollment, claiming, leases, reporting)."""
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
os.environ["ADMIN_KEY"] = "admin-secret"
os.environ["DEMO_API_KEY"] = "dash-secret"

import pandas as pd
from fastapi.testclient import TestClient
import dispatcher
import main
import store

local_runs = []
dispatcher.start_container = lambda cmd, log: (local_runs.append(cmd), subprocess.Popen(
    [sys.executable, "-c", "print('local job')"], stdout=log, stderr=subprocess.STDOUT))[1]

DASH = {"X-API-Key": "dash-secret"}
ADMIN = {"X-Admin-Key": "admin-secret"}


def deadline_in(hours):
    return (main.get_now() + pd.Timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def task(**kw):
    base = {"name": "t", "image": "demo-job:1", "duration_hours": 2,
            "deadline": deadline_in(24), "power_kw": 0.1, "needs_gpu": False}
    base.update(kw)
    return base


def bearer(token):
    return {"Authorization": "Bearer " + token}


with TestClient(main.app) as c:
    # ---- dashboard API key and CORS preflight ----
    assert c.get("/tasks").status_code == 401
    assert c.get("/tasks", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/tasks", headers=DASH).status_code == 200
    assert c.get("/health").status_code == 200
    pre = c.options("/tasks", headers={"Origin": "https://x.github.io", "Access-Control-Request-Method": "POST",
                                       "Access-Control-Request-Headers": "x-api-key,content-type"})
    assert pre.status_code == 200 and "access-control-allow-origin" in pre.headers
    print("api key + CORS : ok")

    # ---- enrollment is admin-only ----
    assert c.post("/admin/agents", json={"region": "sweden"}).status_code == 403
    assert c.post("/admin/agents", json={"region": "sweden"}, headers={"X-Admin-Key": "no"}).status_code == 403
    assert c.post("/admin/agents", json={"region": "mars"}, headers=ADMIN).status_code == 404
    tok_se = c.post("/admin/agents", json={"region": "sweden"}, headers=ADMIN).json()["token"]
    tok_de = c.post("/admin/agents", json={"region": "germany"}, headers=ADMIN).json()["token"]
    assert len(tok_se) > 30 and tok_se != tok_de
    with store.connect(main.DB_PATH) as con:       # only a hash is stored, never the token
        assert tok_se not in str(con.execute("SELECT * FROM agents").fetchall())
    print("enrollment     : admin only, tokens stored hashed")

    # ---- agent endpoints need a valid token ----
    assert c.post("/agent/heartbeat", json={}).status_code == 401
    assert c.post("/agent/heartbeat", json={}, headers=bearer("bad")).status_code == 401
    assert c.post("/agent/jobs/claim", headers=bearer("bad")).status_code == 401

    # ---- an enrolled agent that is offline: the task waits, nothing runs locally ----
    d = c.post("/tasks", json=task(), headers=DASH).json()
    assert d["region"] == "sweden"
    c.post("/clock/advance", json={"hours": 30}, headers=DASH)
    main.scheduler.tick()
    assert c.get(f"/tasks/{d['id']}", headers=DASH).json()["status"] == "Scheduled" and not local_runs
    print("offline agent  : task waits (no silent local run)")

    # ---- heartbeat makes it online -> task is queued -> claimed once -> reported ----
    hb = c.post("/agent/heartbeat", json={"running": []}, headers=bearer(tok_se)).json()
    assert hb["region"] == "sweden"
    assert c.get("/agents", headers=DASH).json()[1]["online"] is True      # sweden (sorted after germany)
    main.scheduler.tick()
    assert c.get(f"/tasks/{d['id']}", headers=DASH).json()["status"] == "Queued"

    assert c.post("/agent/jobs/claim", headers=bearer(tok_de)).json() == []   # other region gets nothing
    jobs = c.post("/agent/jobs/claim", headers=bearer(tok_se)).json()
    assert len(jobs) == 1 and jobs[0]["image"] == "demo-job:1" and jobs[0]["work_seconds"] == 120
    assert jobs[0]["region_key"] == "swedencentral"
    assert c.post("/agent/jobs/claim", headers=bearer(tok_se)).json() == [], "double hand-out!"
    assert c.get(f"/tasks/{d['id']}", headers=DASH).json()["status"] == "Dispatching"

    # a different region's agent cannot report this task
    r = c.post(f"/agent/jobs/{d['id']}/status", json={"status": "Running"}, headers=bearer(tok_de))
    assert r.status_code == 403
    assert c.post(f"/agent/jobs/{d['id']}/status", json={"status": "Running"}, headers=bearer(tok_se)).status_code == 200
    got = c.get(f"/tasks/{d['id']}", headers=DASH).json()
    assert got["status"] == "Running" and got["started_at"]
    # invalid transition and bad status are rejected
    assert c.post(f"/agent/jobs/{d['id']}/status", json={"status": "Bogus"}, headers=bearer(tok_se)).status_code == 422
    r = c.post(f"/agent/jobs/{d['id']}/status", json={"status": "Completed", "exit_code": 0,
               "log": "agent host: ip-172-31-0-9 | runner: docker\njob done\n"}, headers=bearer(tok_se))
    assert r.status_code == 200
    done = c.get(f"/tasks/{d['id']}", headers=DASH).json()
    assert done["status"] == "Completed" and done["finished_at"]
    assert "ip-172-31-0-9" in c.get(f"/tasks/{d['id']}/log", headers=DASH).json()["log"]
    assert c.post(f"/agent/jobs/{d['id']}/status", json={"status": "Failed"}, headers=bearer(tok_se)).status_code == 409
    print("agent flow     : Queued -> Dispatching -> Running -> Completed, log shows the host")

    # ---- lease expiry: a task taken but never confirmed is offered again ----
    d2 = c.post("/tasks", json=task(deadline=deadline_in(2)), headers=DASH).json()
    main.scheduler.tick()
    assert c.get(f"/tasks/{d2['id']}", headers=DASH).json()["status"] == "Queued"
    assert len(c.post("/agent/jobs/claim", headers=bearer(tok_se)).json()) == 1
    with store.connect(main.DB_PATH) as con:
        con.execute("UPDATE tasks SET lease_until = ? WHERE id = ?", (time.time() - 1, d2["id"])); con.commit()
    main.scheduler.tick()
    assert c.get(f"/tasks/{d2['id']}", headers=DASH).json()["status"] == "Queued"
    assert len(c.post("/agent/jobs/claim", headers=bearer(tok_se)).json()) == 1
    print("lease expiry   : unconfirmed task is offered again")

    # ---- a Running task missing from the heartbeat is failed (agent restarted) ----
    c.post(f"/agent/jobs/{d2['id']}/status", json={"status": "Running"}, headers=bearer(tok_se))
    main.TRACKING_GRACE = 0
    hb = c.post("/agent/heartbeat", json={"running": []}, headers=bearer(tok_se)).json()
    assert hb["failed_untracked"] == [d2["id"]]
    assert c.get(f"/tasks/{d2['id']}", headers=DASH).json()["status"] == "Failed"
    print("reconcile      : task the agent lost track of is marked Failed")

    # ---- agent silent for too long: its running jobs are failed ----
    d3 = c.post("/tasks", json=task(deadline=deadline_in(2)), headers=DASH).json()
    main.scheduler.tick()
    c.post("/agent/jobs/claim", headers=bearer(tok_se))
    c.post(f"/agent/jobs/{d3['id']}/status", json={"status": "Running"}, headers=bearer(tok_se))
    store.touch_agent(main.DB_PATH, "sweden", time.time() - 1000)
    main.scheduler.tick()
    assert c.get(f"/tasks/{d3['id']}", headers=DASH).json()["status"] == "Failed"
    print("agent lost     : silent agent's jobs are marked Failed")

    # ---- revoke = kill switch; region without an agent runs locally again ----
    assert c.delete("/admin/agents/sweden", headers=ADMIN).status_code == 200
    assert c.post("/agent/heartbeat", json={}, headers=bearer(tok_se)).status_code == 401
    d4 = c.post("/tasks", json=task(deadline=deadline_in(2)), headers=DASH).json()
    main.scheduler.tick()
    assert local_runs, "without an agent the task should run on this machine"
    print("revoke         : token dead, region falls back to local run")

    # ---- re-enrolling replaces the token ----
    t1 = c.post("/admin/agents", json={"region": "poland"}, headers=ADMIN).json()["token"]
    t2 = c.post("/admin/agents", json={"region": "poland"}, headers=ADMIN).json()["token"]
    assert c.post("/agent/heartbeat", json={}, headers=bearer(t1)).status_code == 401
    assert c.post("/agent/heartbeat", json={}, headers=bearer(t2)).status_code == 200

    # ---- a platform restart must not fail jobs that agents are still running ----
    d5 = c.post("/tasks", json=task(deadline=deadline_in(2)), headers=DASH).json()
    store.queue(main.DB_PATH, d5["id"])
    store.mark_running(main.DB_PATH, d5["id"], "2026-01-01T00:00:00Z", "x")

with TestClient(main.app) as c2:
    assert c2.get(f"/tasks/{d5['id']}", headers=DASH).json()["status"] == "Running"
    print("restart        : agent jobs keep running")

print("Agent platform tests passed")

"""Regression tests for the bugs fixed after the review. Run:  python test_fixes.py"""
import os
import sys
import tempfile
import time

tmp = tempfile.mkdtemp()
os.environ.update(SCHEDULER_AUTOSTART="0", DB_PATH=os.path.join(tmp, "t.db"), LOG_DIR=os.path.join(tmp, "logs"))

from fastapi.testclient import TestClient  # noqa: E402

import dispatcher  # noqa: E402
import main  # noqa: E402

JOB = os.path.join(os.path.dirname(__file__), "..", "job", "job.py")


def task(c, hours_ahead=24, dur=2):
    now = c.get("/clock").json()["now"]
    deadline = (main.pd.Timestamp(now) + main.pd.Timedelta(hours=hours_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return c.post("/tasks", json={"name": "t", "image": "demo-job:1", "duration_hours": dur, "deadline": deadline})


with TestClient(main.app) as c:
    # 1. the job container gets and honours WORK_SECONDS / REGION
    cmd = dispatcher.build_command("demo-job:1", "swedencentral", 7, 12)
    assert "WORK_SECONDS=12" in cmd and "REGION=swedencentral" in cmd
    src = open(JOB).read()
    assert "WORK_SECONDS" in src and "REGION" in src
    print("job env        : WORK_SECONDS and REGION are passed and read")

    # 2. the clock can be fast-forwarded far past the 48 h of recorded data
    for _ in range(4):
        c.post("/clock/advance", json={"hours": 48})
    r = task(c)
    assert r.status_code == 200, r.text
    print("long clock run : still schedules after +192 h")

    # 3. an absurd deadline is rejected quickly instead of hanging the server
    t0 = time.time()
    r = task(c, hours_ahead=24 * 365 * 70)
    assert r.status_code == 422 and "too far" in r.json()["detail"], r.text
    assert time.time() - t0 < 5
    print("far deadline   : rejected in %.2f s" % (time.time() - t0))

    # 4. summary has a completed-only block
    s = c.get("/summary").json()
    assert "completed" in s and s["completed"]["tasks_counted"] == 0
    print("summary        : planned and completed numbers are separate")

# 5. strict placement: no agent for the region -> the task waits, nothing runs locally
import scheduler  # noqa: E402

scheduler.STRICT_PLACEMENT = True
started = []
with TestClient(main.app) as c:
    main.scheduler._start = lambda row, now: started.append(row["id"])
    r = task(c, hours_ahead=2, dur=1)
    assert r.status_code == 200, r.text
    c.post("/clock/advance", json={"hours": 3})
    main.scheduler.tick()
    assert not started, "strict placement must not run locally"
    assert c.get("/tasks").json()[-1]["status"] == "Scheduled"
print("strict mode    : task waits for its region's agent")
print("Fix tests passed")

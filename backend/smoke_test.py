"""Check the WHOLE system against a running server (real Docker, real clock).
    1. start the server:  uvicorn main:app --port 8001
    2. in another terminal:  python smoke_test.py            (or give the API address)
It resets the demo, loads the 4 sample tasks, fast-forwards the clock, waits for every
container to finish and prints a result table. Exit code 0 means everything worked."""
import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001"
TIMEOUT = 600


def call(path, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"FAILED {method} {path}: {e.code} {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach {API} ({e.reason}). Is the server running?")


def parse(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check(cond, message):
    print(("  ok   " if cond else "  FAIL ") + message)
    if not cond:
        raise SystemExit(1)


print("1. server and data")
check(call("/health")["ok"], "server answers")
call("/reset", "POST")
carbon = call("/carbon")
check(len(carbon) == 3 and all(len(v) >= 24 for v in carbon.values()), f"carbon data for {sorted(carbon)}")

print("2. sample tasks")
decisions = call("/demo/samples", "POST")
check(len(decisions) == 4 and not any("error" in d for d in decisions), "4 decisions, no errors")
for d in decisions:
    print(f"     #{d['id']} {d['name']:15} -> {d['region_key']:20} start {d['start']}  saved {d['saved_pct']}%")
gpu = [d for d in decisions if d["name"] == "ml-training"][0]
check(any(s["reason"].startswith("GPU") for s in gpu["skipped"]), "GPU task skipped a region that is not confirmed")
check(any(d["start"] > call("/clock")["now"] for d in decisions), "at least one task is delayed for a cleaner slot")

print("3. fast-forward and run the containers")
last_start = max(parse(d["start"]) for d in decisions)
while True:
    now = parse(call("/clock")["now"])
    hours = math.ceil((last_start - now).total_seconds() / 3600) + 1
    if hours <= 0:
        break
    call("/clock/advance", "POST", {"hours": min(hours, 48)})
    if hours <= 48:
        break
end = time.time() + TIMEOUT
while time.time() < end:
    tasks = call("/tasks")
    states = {t["id"]: t["status"] for t in tasks}
    print("     " + "  ".join(f"#{i}:{s}" for i, s in states.items()))
    if all(s in ("Completed", "Failed") for s in states.values()):
        break
    time.sleep(3)
else:
    raise SystemExit("Timed out waiting for the containers.")

print("4. results")
for t in tasks:
    print(f"     #{t['id']} {t['name']:15} {t['status']:10} {t['region_key']:20} {t['carbon_g']:>7} g vs {t['naive_carbon_g']:>7} g")
failed = [t for t in tasks if t["status"] != "Completed"]
for t in failed:
    print(f"\n--- log of failed task #{t['id']} ---\n" + call(f"/tasks/{t['id']}/log")["log"])
check(not failed, "every container completed")
s = call("/summary")
print(f"     total: {s['carbon_g']} g carbon-aware vs {s['naive_carbon_g']} g naive = {s['saved_pct']}% saved")
check(s["saved_pct"] > 0, "carbon-aware total is lower than naive")
print("\nSMOKE TEST PASSED")

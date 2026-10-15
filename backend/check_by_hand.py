"""Recompute one task's carbon from the RAW csv, without engine.py.
Run from the backend folder while the API is running:
    python check_by_hand.py 1 --duration 2 --power 0.1 --naive-start 2026-10-14T00:00:00Z
"""
import argparse
import json
import math
import os
import urllib.request
from pathlib import Path

import pandas as pd

API = os.getenv("API", "http://localhost:8001")
CARBON = os.getenv("CARBON_PATH",
                   str(Path(__file__).resolve().parent.parent / "data" / "carbon.csv"))


def get(path):
    with urllib.request.urlopen(API + path) as r:
        return json.load(r)


def grams(df, region, start, hours, power):
    """power (kW) x ROUNDED-UP hours x average gCO2/kWh over those hours (same as engine.py)."""
    window = df[(df["region"] == region) & (df["t"] >= start)
                & (df["t"] < start + pd.Timedelta(hours=hours))]
    if len(window) != hours:
        return None, window
    return power * hours * window["gco2_per_kwh"].mean(), window


def report(label, mine, api, tol):
    if mine is None or api is None:
        print(f"{label}: cannot compare (by hand={mine}, API={api})")
        return False
    ok = abs(mine - api) <= tol
    print(f"{label}: by hand {mine:.2f} | API {api} | {'OK' if ok else 'MISMATCH'}")
    return ok


p = argparse.ArgumentParser()
p.add_argument("task_id", type=int)
p.add_argument("--duration", type=float, required=True,
               help="task duration in hours; 2.5 is treated as 3, like the engine")
p.add_argument("--power", type=float, default=0.1)
p.add_argument("--naive-start",
               help="simulated hour the task was submitted, e.g. 2026-10-14T00:00:00Z")
a = p.parse_args()

clock, task = get("/clock"), get(f"/tasks/{a.task_id}")
df = pd.read_csv(CARBON)
df["t"] = pd.to_datetime(df["timestamp_utc"], utc=True)
df["t"] += pd.Timestamp(clock["sim_start"]) - df["t"].min()      # same re-basing as the server
hours = max(1, math.ceil(a.duration))

start = pd.Timestamp(task["start"])
mine, window = grams(df, task["region"], start, hours, a.power)
print(f"Carbon-aware: {task['region']} from {start:%Y-%m-%d %H:%M} for {hours} h")
print(window[["t", "gco2_per_kwh"]].to_string(index=False))
ok = report("Carbon-aware grams", mine, task["carbon_g"], 0.1)

if a.naive_start:
    n_mine, _ = grams(df, task["naive_region"], pd.Timestamp(a.naive_start), hours, a.power)
    ok &= report("Naive grams", n_mine, task["naive_carbon_g"], 0.1)
    if mine is not None and n_mine:
        ok &= report("Saved %", 100 * (n_mine - mine) / n_mine, task["saved_pct"], 0.2)
else:
    print("(no --naive-start given: only the carbon-aware number was checked)")
print("ALL OK" if ok else "CHECK THE DIFFERENCES ABOVE")
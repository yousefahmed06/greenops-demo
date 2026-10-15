"""The web API (FastAPI). Run from the backend folder:
    uvicorn main:app --reload --port 8001
"""
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import engine
import store
from clock import DemoClock
from scheduler import Scheduler

BASE = Path(__file__).resolve().parent.parent
CARBON_PATH = os.getenv("CARBON_PATH", str(BASE / "data" / "carbon.csv"))
REGIONS_PATH = os.getenv("REGIONS_PATH", str(BASE / "data" / "regions.csv"))
DB_PATH = os.getenv("DB_PATH", str(BASE / "greenops.db"))
LOG_DIR = os.getenv("LOG_DIR", str(BASE / "logs"))
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "germany")
ALLOWED_IMAGES = [i.strip() for i in os.getenv("ALLOWED_IMAGES", "demo-job:1").split(",")]

clock = DemoClock(speed=float(os.getenv("DEMO_SPEED", "360")))   # 360 = 1 hour per 10 s
scheduler = Scheduler(DB_PATH, clock, LOG_DIR)
state = {}


def get_now():
    """The simulated time, rounded down to the hour (used for decisions)."""
    return clock.now().floor("h")


def rebuild_series():
    """Shift the recorded carbon data so its first row is the clock's current hour."""
    carbon = engine.rebase(state["carbon_raw"], get_now())
    state["series"] = engine.build_series(carbon)


@asynccontextmanager
async def lifespan(app):
    state["carbon_raw"], state["regions"] = engine.load_data(CARBON_PATH, REGIONS_PATH)
    clock.reset()
    rebuild_series()
    store.init(DB_PATH)
    store.fail_orphans(DB_PATH)
    if os.getenv("SCHEDULER_AUTOSTART", "1") == "1":
        scheduler.start()
    yield
    scheduler.stop()
    scheduler.stop_all_jobs()


app = FastAPI(title="GreenOps demo API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class TaskIn(BaseModel):
    name: str
    image: str
    duration_hours: float = Field(gt=0)
    deadline: datetime
    power_kw: float = Field(default=0.1, gt=0)
    needs_gpu: bool = False


class SpeedIn(BaseModel):
    speed: float = Field(ge=1, le=3600)      # simulated seconds per real second


class AdvanceIn(BaseModel):
    hours: float = Field(gt=0, le=48)


def to_utc(ts: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def clock_info():
    return {"now": clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "speed": clock.speed}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/regions")
def list_regions():
    return state["regions"].to_dict(orient="records")


@app.get("/clock")
def read_clock():
    return clock_info()


@app.post("/clock/speed")
def set_clock_speed(body: SpeedIn):
    clock.set_speed(body.speed)
    return clock_info()


@app.post("/clock/advance")
def advance_clock(body: AdvanceIn):
    clock.advance(body.hours)
    return clock_info()


@app.post("/reset")
def reset_demo():
    """Clear all tasks, stop running jobs, restart the clock and the carbon window."""
    scheduler.stop_all_jobs()
    store.reset(DB_PATH)
    clock.reset()
    rebuild_series()
    return {"ok": True, **clock_info()}


def submit(task: TaskIn):
    """Decide and save one task (shared by POST /tasks and the sample loader)."""
    if task.image not in ALLOWED_IMAGES:
        raise HTTPException(status_code=422,
                            detail=f"Image not allowed in the demo. Allowed: {ALLOWED_IMAGES}")
    try:
        decision = engine.choose(
            {"duration_hours": task.duration_hours, "deadline": to_utc(task.deadline),
             "power_kw": task.power_kw, "needs_gpu": task.needs_gpu},
            state["series"], state["regions"], get_now(), DEFAULT_REGION)
    except engine.NoOptionError as e:
        raise HTTPException(status_code=422, detail=str(e))
    task_id = store.insert(DB_PATH, decision, {
        "name": task.name, "image": task.image, "duration_hours": task.duration_hours,
        "power_kw": task.power_kw, "needs_gpu": task.needs_gpu})
    return store.public(store.get(DB_PATH, task_id))


@app.post("/tasks")
def create_task(task: TaskIn):
    return submit(task)


@app.get("/carbon")
def carbon_series():
    """Carbon data for the charts: {region: [[time, gCO2/kWh], ...]} on the demo clock."""
    return {region: [[ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(v)] for ts, v in s.items()]
            for region, s in state["series"].items()}


@app.get("/summary")
def summary():
    rows = [r for r in store.list_all(DB_PATH) if r["status"] != "Failed"]
    aware = sum(r["decision"]["carbon_g"] for r in rows)
    naive = sum(r["decision"]["naive_carbon_g"] for r in rows)
    saved = round((naive - aware) / naive * 100, 1) if naive > 0 else 0.0
    return {"tasks": len(rows), "carbon_g": round(aware, 1),
            "naive_carbon_g": round(naive, 1), "saved_pct": saved}


SAMPLES = [  # (name, duration h, deadline in h from now, power kW, needs GPU)
    ("nightly-backup", 2, 30, 0.1, False),   # flexible: should wait for a clean slot
    ("urgent-report",  2, 2,  0.1, False),   # tight deadline: runs now
    ("etl-pipeline",   3, 12, 0.2, False),   # medium flexibility
    ("ml-training",    4, 24, 0.3, True),    # needs a GPU: regions marked 'Verify' are skipped
]


@app.post("/demo/samples")
def load_samples():
    """Submit the four sample tasks (deadlines are relative to the demo clock)."""
    now, out = get_now(), []
    for name, dur, hours, power, gpu in SAMPLES:
        task = TaskIn(name=name, image=ALLOWED_IMAGES[0], duration_hours=dur,
                      deadline=(now + pd.Timedelta(hours=hours)).to_pydatetime(),
                      power_kw=power, needs_gpu=gpu)
        try:
            out.append(submit(task))
        except HTTPException as e:
            out.append({"name": name, "error": e.detail})
    return out


@app.get("/tasks")
def list_tasks():
    return [store.public(r) for r in store.list_all(DB_PATH)]


@app.get("/tasks/{task_id}")
def get_task(task_id: int):
    row = store.get(DB_PATH, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return store.public(row)


@app.get("/tasks/{task_id}/log")
def task_log(task_id: int):
    row = store.get(DB_PATH, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    path = Path(row["log_path"]) if row["log_path"] else None
    text = path.read_text(errors="replace") if path and path.exists() else ""
    return {"log": text[-3000:]}

"""The web API (FastAPI). Run from the backend folder:
    uvicorn main:app --reload --port 8001
"""
import hashlib
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import compare
import engine
import store
from clock import DemoClock
from scheduler import ONLINE_WINDOW, Scheduler, work_seconds

BASE = Path(__file__).resolve().parent.parent
CARBON_PATH = os.getenv("CARBON_PATH", str(BASE / "data" / "carbon_aws.csv"))
REGIONS_PATH = os.getenv("REGIONS_PATH", str(BASE / "data" / "regions_aws.csv"))
DB_PATH = os.getenv("DB_PATH", str(BASE / "greenops.db"))
LOG_DIR = os.getenv("LOG_DIR", str(BASE / "logs"))
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "germany")
ALLOWED_IMAGES = [i.strip() for i in os.getenv("ALLOWED_IMAGES", "demo-job:1").split(",")]
API_KEY = os.getenv("DEMO_API_KEY", "")        # empty = no key needed (local use only!)
ADMIN_KEY = os.getenv("ADMIN_KEY", "")         # empty = agent enrollment is disabled
ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]
MAX_DEADLINE_HOURS = float(os.getenv("MAX_DEADLINE_HOURS", "168"))   # reject deadlines further away than this
HORIZON_HOURS = MAX_DEADLINE_HOURS + 24     # carbon data is always kept this far ahead of the demo clock
LEASE_SECONDS = 60          # an agent must confirm a task within this time
TRACKING_GRACE = 15         # seconds before a Running task missing from a heartbeat is failed

clock = DemoClock(speed=float(os.getenv("DEMO_SPEED", "360")))   # 360 = 1 hour per 10 s
scheduler = Scheduler(DB_PATH, clock, LOG_DIR)
state = {}


def get_now():
    """The simulated time, rounded down to the hour (used for decisions)."""
    return clock.now().floor("h")


def rebuild_series(new_anchor=True):
    """Shift the recorded carbon data so its first row is the clock's current hour.
    The data is repeated as often as needed to stay HORIZON_HOURS ahead of the clock."""
    if new_anchor or "anchor" not in state:
        state["anchor"] = get_now()
    until = get_now() + pd.Timedelta(hours=HORIZON_HOURS)
    cycles = engine.cycles_needed(state["carbon_raw"], state["anchor"], until)
    carbon = engine.rebase(state["carbon_raw"], state["anchor"], cycles)
    state["series"] = engine.build_series(carbon)
    state["cycles"] = cycles


def ensure_series():
    """Extend the carbon data if the (fast-forwarded) clock has caught up with its end."""
    until = get_now() + pd.Timedelta(hours=HORIZON_HOURS)
    if engine.cycles_needed(state["carbon_raw"], state["anchor"], until) > state["cycles"]:
        rebuild_series(new_anchor=False)


@asynccontextmanager
async def lifespan(app):
    state["carbon_raw"], state["regions"] = engine.load_data(CARBON_PATH, REGIONS_PATH)
    clock.reset()
    rebuild_series()
    store.init(DB_PATH)
    store.fail_orphans(DB_PATH)
    if not API_KEY:
        print("WARNING: DEMO_API_KEY is empty - anyone who can reach this API can reset it and "
              "move the clock. Fine on localhost; set a key before exposing it.")
    if os.getenv("SCHEDULER_AUTOSTART", "1") == "1":
        scheduler.start()
    yield
    scheduler.stop()
    scheduler.stop_all_jobs()


app = FastAPI(title="GreenOps demo API", lifespan=lifespan)

PUBLIC_PREFIXES = ("/health", "/agent/", "/admin/", "/docs", "/openapi.json", "/redoc")


@app.middleware("http")
async def api_key_guard(request, call_next):
    """If DEMO_API_KEY is set, the dashboard endpoints need the X-API-Key header.
    Agents use their own token and admin calls use ADMIN_KEY (checked in their endpoints)."""
    if API_KEY and request.method != "OPTIONS" and not request.url.path.startswith(PUBLIC_PREFIXES):
        if not hmac.compare_digest(request.headers.get("x-api-key", ""), API_KEY):
            return JSONResponse({"detail": "Missing or wrong API key"}, status_code=401)
    return await call_next(request)


app.add_middleware(CORSMiddleware, allow_origins=ORIGINS,
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
    ensure_series()
    if to_utc(task.deadline) > get_now() + pd.Timedelta(hours=MAX_DEADLINE_HOURS):
        raise HTTPException(status_code=422,
                            detail=f"Deadline too far away (maximum {MAX_DEADLINE_HOURS:g} hours from now)")
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
    ensure_series()
    return {region: [[ts.strftime("%Y-%m-%dT%H:%M:%SZ"), float(v)] for ts, v in s.items()]
            for region, s in state["series"].items()}


@app.get("/summary")
def summary():
    rows = [r for r in store.list_all(DB_PATH) if r["status"] != "Failed"]
    aware = sum(r["decision"]["carbon_g"] for r in rows)
    naive = sum(r["decision"]["naive_carbon_g"] for r in rows)
    saved = round((naive - aware) / naive * 100, 1) if naive > 0 else 0.0
    # "tasks / carbon_g / ..." are PLANNED numbers (they include tasks that have not run yet);
    # "completed" only counts tasks that really finished.
    return {"tasks": len(rows), "carbon_g": round(aware, 1),
            "naive_carbon_g": round(naive, 1), "saved_pct": saved,
            "completed": compare.summarize([store.public(r) for r in rows])}


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


# =============================================================== agents
def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def require_admin(x_admin_key: str = Header(default="")):
    if not ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Agent enrollment is disabled (set ADMIN_KEY)")
    if not hmac.compare_digest(x_admin_key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Wrong admin key")


def agent_region(authorization: str = Header(default="")):
    """The region of the agent that sent this request (from its bearer token)."""
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    region = store.agent_for_token(DB_PATH, sha(token)) if token else None
    if region is None:
        raise HTTPException(status_code=401, detail="Unknown or revoked agent token")
    return region


class EnrollIn(BaseModel):
    region: str


@app.post("/admin/agents")
def enroll_agent(body: EnrollIn, _=Depends(require_admin)):
    """Create (or replace) the token of one region's agent. The token is shown only once."""
    if body.region not in set(state["regions"].region_id):
        raise HTTPException(status_code=404, detail="Unknown region")
    token = secrets.token_urlsafe(32)
    store.set_agent(DB_PATH, body.region, sha(token), time.time())
    return {"region": body.region, "token": token}


@app.delete("/admin/agents/{region}")
def revoke_agent(region: str, _=Depends(require_admin)):
    """Kill switch: the agent can no longer receive or report jobs."""
    if not store.delete_agent(DB_PATH, region):
        raise HTTPException(status_code=404, detail="No agent for this region")
    return {"ok": True}


@app.get("/agents")
def list_agents():
    """For the dashboard: which regions have an agent and whether it is online."""
    now = time.time()
    return [{"region": a["region"],
             "online": a["last_seen"] is not None and now - a["last_seen"] <= ONLINE_WINDOW,
             "seconds_since_seen": None if a["last_seen"] is None else round(now - a["last_seen"])}
            for a in store.agents(DB_PATH)]


class HeartbeatIn(BaseModel):
    running: List[int] = []
    host: Optional[str] = None


@app.post("/agent/heartbeat")
def agent_heartbeat(body: HeartbeatIn, region: str = Depends(agent_region)):
    now = time.time()
    store.touch_agent(DB_PATH, region, now)
    lost = store.fail_untracked(DB_PATH, region, set(body.running), now, TRACKING_GRACE,
                                clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {"region": region, "speed": clock.speed, "failed_untracked": lost}


@app.post("/agent/jobs/claim")
def agent_claim(region: str = Depends(agent_region)):
    """Give the agent its due tasks. It must confirm each one (status Running) within the lease."""
    store.touch_agent(DB_PATH, region, time.time())
    rows = store.claim_queued(DB_PATH, region, time.time() + LEASE_SECONDS)
    return [{"id": r["id"], "image": r["task"]["image"], "region_key": r["decision"]["region_key"],
             "work_seconds": work_seconds(r["task"]["duration_hours"], clock.speed)}
            for r in rows]


class StatusIn(BaseModel):
    status: str = Field(pattern="^(Running|Completed|Failed)$")
    exit_code: Optional[int] = None
    log: Optional[str] = None


@app.post("/agent/jobs/{task_id}/status")
def agent_status_report(task_id: int, body: StatusIn, region: str = Depends(agent_region)):
    now = time.time()
    store.touch_agent(DB_PATH, region, now)
    sim = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    ok, message = store.report(DB_PATH, task_id, region, body.status, sim, body.exit_code, now)
    if not ok:
        raise HTTPException(status_code=403 if "belong" in message else 409, detail=message)
    if body.log:
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        log_path = Path(LOG_DIR) / f"task_{task_id}.log"
        log_path.write_text(body.log[-20000:], errors="replace")
        store.set_log_path(DB_PATH, task_id, str(log_path))
    return {"ok": True, "message": message}

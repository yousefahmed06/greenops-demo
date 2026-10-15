"""The scheduler: starts due tasks and watches running ones."""
import os
import threading
import time
from pathlib import Path

import dispatcher
import store


ONLINE_WINDOW = float(os.getenv("AGENT_ONLINE_SECONDS", "30"))   # heartbeat newer than this = online
LOST_AFTER = float(os.getenv("AGENT_LOST_SECONDS", "120"))      # silent longer than this = jobs lost


def work_seconds(duration_hours, speed):
    """The container works for a scaled-down time, so the demo stays short."""
    return int(min(120, max(5, round(duration_hours * 3600 / speed))))


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class Scheduler:
    def __init__(self, db_path, clock, log_dir):
        self.db_path = db_path
        self.clock = clock
        self.log_dir = Path(log_dir)
        self.running = {}                  # task id -> (process, log file)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    # ---- one pass of the loop (tests call this directly) ----
    def tick(self):
        import pandas as pd
        with self._lock:
            now = self.clock.now()
            # 1. check running jobs
            for task_id, (proc, logf) in list(self.running.items()):
                code = proc.poll()
                if code is not None:
                    logf.close()
                    store.mark_done(self.db_path, task_id,
                                    "Completed" if code == 0 else "Failed", iso(now), code)
                    del self.running[task_id]
            # 2. tasks whose time has come: run here, or hand them to the region's agent
            real = time.time()
            for row in store.scheduled(self.db_path):
                if pd.Timestamp(row["decision"]["start"]) <= now:
                    agent = store.agent_status(self.db_path, row["decision"]["region"], real, ONLINE_WINDOW)
                    if agent is None:                              # no agent for this region
                        if store.claim(self.db_path, row["id"]):   # mark Dispatching first
                            self._start(row, now)
                    elif agent["online"]:
                        store.queue(self.db_path, row["id"])       # the agent will collect it
                    # enrolled but offline: wait (never silently run in the wrong place)
            # 3. housekeeping for agent jobs
            store.requeue_expired(self.db_path, real)
            for a in store.agents(self.db_path):
                if a["last_seen"] is not None and real - a["last_seen"] > LOST_AFTER:
                    store.fail_agent_jobs(self.db_path, a["region"], iso(now))

    def _start(self, row, now):
        task, decision, task_id = row["task"], row["decision"], row["id"]
        # the container works for a scaled-down time, so the demo stays short
        secs = work_seconds(task["duration_hours"], self.clock.speed)
        cmd = dispatcher.build_command(task["image"], decision["region_key"], task_id, secs)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"task_{task_id}.log"
        logf = open(log_path, "w")
        logf.write("$ " + " ".join(cmd) + "\n")
        logf.flush()
        try:
            proc = dispatcher.start_container(cmd, logf)
        except OSError as e:
            logf.write(f"Could not start the container: {e}\n"
                       "Is Docker installed and running?\n")
            logf.close()
            store.mark_running(self.db_path, task_id, iso(now), str(log_path))
            store.mark_done(self.db_path, task_id, "Failed", iso(now), None)
            return
        store.mark_running(self.db_path, task_id, iso(now), str(log_path))
        self.running[task_id] = (proc, logf)

    # ---- background thread ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(1.0):
            try:
                self.tick()
            except Exception as e:                 # keep the loop alive
                print("scheduler error:", e)

    def stop(self):
        self._stop.set()

    def stop_all_jobs(self):
        with self._lock:
            for proc, logf in self.running.values():
                if proc.poll() is None:
                    proc.terminate()
                logf.close()
            self.running.clear()

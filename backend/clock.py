"""The demo clock: simulated time that can run faster than real time."""
import threading
import time

import pandas as pd


class DemoClock:
    def __init__(self, speed=360.0):
        self._lock = threading.Lock()
        self.speed = float(speed)          # simulated seconds per real second
        self._sim_anchor = None
        self._real_anchor = None
        self.reset()

    def reset(self):
        """Start again from the current hour (simulated time = real hour)."""
        with self._lock:
            self._sim_anchor = pd.Timestamp.now(tz="UTC").floor("h")
            self._real_anchor = time.monotonic()

    def _now(self):
        elapsed = time.monotonic() - self._real_anchor
        return self._sim_anchor + pd.Timedelta(seconds=elapsed * self.speed)

    def now(self):
        with self._lock:
            return self._now()

    def set_speed(self, speed):
        with self._lock:
            self._sim_anchor = self._now()      # keep the current time, change the pace
            self._real_anchor = time.monotonic()
            self.speed = float(speed)

    def advance(self, hours):
        """Fast-forward the simulated time."""
        with self._lock:
            self._sim_anchor = self._sim_anchor + pd.Timedelta(hours=hours)

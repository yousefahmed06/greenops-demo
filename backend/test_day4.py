import pandas as pd

from compare import series_points, summarize


def task(status, aware, naive):
    return {"status": status, "carbon_g": aware, "naive_carbon_g": naive}


# 1. totals count only Completed tasks that have both numbers
rows = [task("Completed", 10.0, 40.0), task("Completed", 30.0, 60.0),
        task("Scheduled", 1.0, 100.0), task("Completed", 5.0, None)]
assert summarize(rows) == {"tasks_counted": 2, "naive_g": 100.0, "aware_g": 40.0,
                           "saved_g": 60.0, "saved_pct": 60.0}

# 2. unfinished tasks can be included on request
s = summarize(rows, only_completed=False)
assert s["tasks_counted"] == 3 and s["naive_g"] == 200.0
assert s["aware_g"] == 41.0 and s["saved_pct"] == 79.5

# 3. nothing to count: no division by zero
s = summarize([])
assert s["tasks_counted"] == 0 and s["saved_pct"] is None

# 4. a task worse than run-now gives a negative saving, not a crash
s = summarize([task("Completed", 50.0, 40.0)])
assert s["saved_g"] == -10.0 and s["saved_pct"] == -25.0

# 5. chart data: UTC "Z" timestamps, rounded values, NaN becomes None
idx = pd.date_range("2026-10-14 00:00", periods=3, freq="h", tz="UTC")
pts = series_points({"sweden": pd.Series([10.0, float("nan"), 12.34], index=idx)})
assert pts["sweden"][0] == {"t": "2026-10-14T00:00:00Z", "g": 10.0}
assert pts["sweden"][1]["g"] is None
assert pts["sweden"][2]["g"] == 12.3

print("Day 4 tests passed")
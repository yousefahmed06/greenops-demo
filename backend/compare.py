"""Small pure helpers for Day 4: headline totals and chart data."""


def summarize(tasks, only_completed=True):
    """Total carbon of run-now vs carbon-aware, over tasks that have both numbers."""
    naive = aware = 0.0
    counted = 0
    for t in tasks:
        if only_completed and t.get("status") != "Completed":
            continue
        n, a = t.get("naive_carbon_g"), t.get("carbon_g")
        if n is None or a is None:
            continue
        naive += n
        aware += a
        counted += 1
    saved = naive - aware
    pct = round(100 * saved / naive, 1) if naive > 0 else None
    return {"tasks_counted": counted, "naive_g": round(naive, 1),
            "aware_g": round(aware, 1), "saved_g": round(saved, 1), "saved_pct": pct}


def series_points(series):
    """{region: pandas Series} -> {region: [{"t": "...Z", "g": 52.3}, ...]} for the line chart."""
    out = {}
    for region, s in series.items():
        out[region] = [
            {"t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "g": None if v != v else round(float(v), 1)}      # v != v is True only for NaN
            for ts, v in s.items()
        ]
    return out
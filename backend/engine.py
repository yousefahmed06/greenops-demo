"""The decision engine: picks the best (region, start time) for a task."""
import math
import pandas as pd

DELAY_PENALTY = 0.01   # score gets 1% worse for every hour a task waits


class NoOptionError(Exception):
    """Raised when no region/time can meet the deadline."""


def load_data(carbon_path, regions_path):
    carbon = pd.read_csv(carbon_path)
    carbon["timestamp_utc"] = pd.to_datetime(carbon["timestamp_utc"], utc=True)
    regions = pd.read_csv(regions_path)
    return carbon, regions


def rebase(carbon, now, cycles=1):
    """Shift the recorded timestamps so the first row is 'now'.

    The recorded data is short (e.g. 48 h). With cycles > 1 it is repeated back to back,
    so the demo clock can run (or be fast-forwarded) far past the first day without
    running out of carbon data.
    """
    first = carbon["timestamp_utc"].min()
    period = carbon["timestamp_utc"].max() - first + pd.Timedelta(hours=1)
    shift = now - first
    parts = []
    for i in range(max(1, int(cycles))):
        part = carbon.copy()
        part["timestamp_utc"] = part["timestamp_utc"] + shift + period * i
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def cycles_needed(carbon, anchor, until):
    """How many repeats of the recorded data are needed to cover time up to `until`."""
    first = carbon["timestamp_utc"].min()
    period = carbon["timestamp_utc"].max() - first + pd.Timedelta(hours=1)
    return max(1, int(math.ceil((until - anchor) / period)))


def build_series(carbon):
    """One carbon time series per region: {'sweden': Series, ...}"""
    return {name: g.set_index("timestamp_utc")["gco2_per_kwh"].sort_index()
            for name, g in carbon.groupby("region")}


def eligible(needs_gpu, regions, series_by_region):
    """Return (allowed region ids, list of skipped regions with reasons)."""
    ok, skipped = [], []
    for r in regions.itertuples():
        if r.region_id not in series_by_region:
            skipped.append({"region": r.region_id, "reason": "no carbon data"})
        elif needs_gpu and r.gpu_available != "Yes":
            skipped.append({"region": r.region_id, "reason": "GPU not confirmed"})
        else:
            ok.append(r.region_id)
    return ok, skipped


def window_grams(series, start, hours, power_kw):
    """Carbon (grams) if a job runs for `hours` starting at `start`."""
    end = start + pd.Timedelta(hours=hours - 1)
    window = series.loc[start:end]
    if len(window) != hours:          # data does not cover the whole run
        return None
    return power_kw * hours * float(window.mean())


def choose(task, series_by_region, regions, now,
           default_region="germany", delay_penalty=DELAY_PENALTY):
    hours = max(1, math.ceil(task["duration_hours"]))
    deadline = task["deadline"]
    power = task["power_kw"]

    ok, skipped = eligible(task["needs_gpu"], regions, series_by_region)
    if not ok:
        raise NoOptionError("No eligible region for this task.")

    latest_start = deadline - pd.Timedelta(hours=hours)
    if latest_start < now:
        raise NoOptionError("Deadline is too close: the task cannot finish in time.")

    best, options = None, 0
    for region in ok:
        start = now
        # never scan past the end of the data: a far-away deadline must not make this loop huge
        last_start = min(latest_start, series_by_region[region].index.max())
        while start <= last_start:
            grams = window_grams(series_by_region[region], start, hours, power)
            if grams is not None:
                options += 1
                wait_h = (start - now) / pd.Timedelta(hours=1)
                score = grams * (1 + delay_penalty * wait_h)
                if best is None or score < best["score"]:
                    best = {"region": region, "start": start,
                            "grams": grams, "score": score}
            start += pd.Timedelta(hours=1)
    if best is None:
        raise NoOptionError("No carbon data covers the time before the deadline.")

    naive_region = default_region if default_region in ok else ok[0]
    naive = window_grams(series_by_region[naive_region], now, hours, power)
    if naive is None:
        raise NoOptionError("No carbon data for running immediately.")

    saved = (naive - best["grams"]) / naive * 100 if naive > 0 else 0.0
    meta = regions[regions.region_id == best["region"]].iloc[0]
    wait_h = int((best["start"] - now) / pd.Timedelta(hours=1))
    reason = (f"Best of {options} options before the deadline: "
              f"{best['region']} after waiting {wait_h} h, "
              f"{saved:.1f}% less carbon than running now in {naive_region}.")
    return {
        "region": best["region"],
        "region_key": meta.region_key,
        "provider": meta.provider,
        "start": best["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "carbon_g": round(best["grams"], 1),
        "naive_region": naive_region,
        "naive_carbon_g": round(naive, 1),
        "saved_pct": round(saved, 1),
        "skipped": skipped,
        "reason": reason,
    }

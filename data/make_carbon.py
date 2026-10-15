"""Build data/carbon.csv from the three yearly per-region files.
Run from anywhere:  python data/make_carbon.py
"""
from pathlib import Path

import pandas as pd

FILES = {"sweden":  r"D:\greenops-demo\data\raw\SE.csv",
         "germany": r"D:\greenops-demo\data\raw\DE.csv",
         "poland":  r"D:\greenops-demo\data\raw\PL.csv"}

START = pd.Timestamp("2025-06-14", tz="UTC")      # the files cover 2025
END = pd.Timestamp("2025-06-16", tz="UTC")        # exclusive: 48 hours
OUT = Path(__file__).resolve().parent / "carbon.csv"   # always lands in the data folder


frames = []
for region, path in FILES.items():
    df = pd.read_csv(path, encoding="utf-8-sig")             # utf-8-sig removes a hidden BOM
    missing = {"timestamp_utc", "gco2_per_kwh"} - set(df.columns)
    if missing:
        raise SystemExit(f"{region}: missing columns {missing}. Real columns: {df.columns.tolist()}")
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    first, last = df["timestamp_utc"].min(), df["timestamp_utc"].max()
    df = df[(df["timestamp_utc"] >= START) & (df["timestamp_utc"] < END)].copy()
    if df.empty:
        raise SystemExit(f"{region}: no rows between {START:%Y-%m-%d} and {END:%Y-%m-%d}. "
                         f"The file covers {first:%Y-%m-%d} to {last:%Y-%m-%d}: change START and END.")
    df["region"] = region                                     # the names the engine and regions.csv use
    frames.append(df[["timestamp_utc", "region", "gco2_per_kwh"]])

out = pd.concat(frames).sort_values(["region", "timestamp_utc"])

# Day 1 checks: 48 hourly rows per region, the same hours everywhere, no missing values
expected = set(pd.date_range(START, END, freq="h", inclusive="left"))
problems = []
for region, g in out.groupby("region"):
    if len(g) != 48:
        problems.append(f"{region}: {len(g)} rows, expected 48")
    if set(g["timestamp_utc"]) != expected:
        problems.append(f"{region}: hours missing, extra or duplicated")
    if g["gco2_per_kwh"].isna().any():
        problems.append(f"{region}: {int(g['gco2_per_kwh'].isna().sum())} missing values")
if problems:
    raise SystemExit("NOT WRITTEN:\n  " + "\n  ".join(problems))

out["timestamp_utc"] = out["timestamp_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
out.to_csv(OUT, index=False)
print("Wrote", OUT)
print(out.groupby("region")["gco2_per_kwh"].agg(["count", "min", "mean", "max"]).round(1))
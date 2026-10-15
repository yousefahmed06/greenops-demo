import pandas as pd

carbon  = pd.read_csv("data/carbon.csv", parse_dates=["timestamp_utc"])
regions = pd.read_csv("data/regions.csv")

counts = carbon.groupby("region").size()
print(counts)
assert (counts == 48).all(), "each region needs 48 rows"
assert carbon.gco2_per_kwh.notna().all(), "missing carbon values"
assert set(carbon.region) == set(regions.region_id), "region names do not match"

for name, g in carbon.groupby("region"):
    gaps = g.timestamp_utc.diff().dropna().unique()
    assert len(gaps) == 1, f"{name}: hours are not evenly spaced"

merged = carbon.merge(regions, left_on="region", right_on="region_id")
assert len(merged) == len(carbon), "merge lost rows"
print(merged.groupby("region").gco2_per_kwh.agg(["min", "max"]))
print("Day 1 data check passed")
"""Creates data/carbon_fake.csv - FAKE numbers for testing the engine only.
Never show this file in the demo. Replace it with the real carbon.csv from Day 1."""
import math
from pathlib import Path
import pandas as pd

hours = pd.date_range("2023-06-14 00:00", periods=48, freq="h", tz="UTC")
rows = []
for i, ts in enumerate(hours):
    h = ts.hour
    sweden  = 40 + 10 * math.sin(i / 5)                        # very clean, small changes
    solar   = max(0.0, math.sin((h - 6) / 12 * math.pi))      # sun between 06:00 and 18:00
    germany = 420 - 250 * solar + 30 * math.sin(i / 7)         # cleaner at midday
    poland  = 650 + 60 * math.cos(h / 24 * 2 * math.pi)        # dirty, cleaner at night
    for region, v in (("sweden", sweden), ("germany", germany), ("poland", poland)):
        rows.append({"timestamp_utc": ts, "region": region, "gco2_per_kwh": round(v, 1)})

out = Path(__file__).parent / "carbon_fake.csv"
pd.DataFrame(rows).to_csv(out, index=False)
print("wrote", out, len(rows), "rows")

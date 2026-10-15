import pandas as pd

df = pd.read_csv("data/cloud_regions.csv")
pick = {"swedencentral":      ("sweden",  "SE"),
        "germanywestcentral": ("germany", "DE"),
        "polandcentral":      ("poland",  "PL")}

r = df[(df.provider == "Microsoft Azure") & (df.region_key.isin(pick))].copy()
r["region_id"]  = r.region_key.map(lambda k: pick[k][0])
r["grid_zone"]  = r.region_key.map(lambda k: pick[k][1])
r = r[["region_id", "provider", "region_key", "region_name",
       "geography", "zones", "gpu_available", "grid_zone"]]
r.to_csv("data/regions.csv", index=False)
print(r)
"""Creates data/regions_aws.csv for the AWS sandbox test (N. Virginia + Oregon) from the
cloud regions dataset. Run from the repo folder:  python data/make_regions_aws.py
The grid_zone column is left as CHECK_ME on purpose: look up the right Electricity Maps zone
for each location on the data portal and type it in before you build carbon data."""
import pandas as pd

df = pd.read_csv("data/cloud_regions.csv")
pick = {"us-east-1": "virginia", "us-west-2": "oregon"}
r = df[(df.provider == "AWS") & df.region_key.isin(pick)].copy()
r["region_id"] = r.region_key.map(pick)
r["grid_zone"] = "CHECK_ME"
r = r[["region_id", "provider", "region_key", "region_name", "geography", "zones", "gpu_available", "grid_zone"]]
r.to_csv("data/regions_aws.csv", index=False)
print(r.to_string(index=False))

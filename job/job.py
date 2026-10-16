# job/job.py
# The platform / agent pass WORK_SECONDS (how long to work) and REGION (where the job was placed).
import os
import time

seconds = int(os.environ.get("WORK_SECONDS") or os.environ.get("JOB_SECONDS") or "40")
print("Job started, region:", os.environ.get("REGION", "unknown"), "| working for", seconds, "s", flush=True)
time.sleep(seconds)
print("Job done", flush=True)

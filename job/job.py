# job/job.py
import os
import time

seconds = int(os.environ.get("JOB_SECONDS", "40"))
print("Job started, region label:", os.environ.get("REGION", "unknown"), flush=True)
time.sleep(seconds)
print("Job done", flush=True)
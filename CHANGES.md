# Fixes after the code review

**Bugs**
- `job/job.py` now reads `WORK_SECONDS` (and `REGION`), which the platform and agent really send.
  `REGION` is now passed as an env var by the dispatcher, the agent and the Kubernetes manifest.
- Carbon data is repeated automatically (`engine.rebase(..., cycles)`, `ensure_series()`), so the demo
  never runs out of data when the clock runs fast or is advanced.
- Deadlines further away than `MAX_DEADLINE_HOURS` (default 168) are rejected, and the engine never
  scans past the end of the data, so one request can no longer hang the server.
- New `STRICT_PLACEMENT=1` option: a region without an agent waits instead of running on the platform host.
  Default stays 0 (local demo mode).
- Removed the dead, stale `frontend/app.js`.

**Safer defaults**
- The dashboard keeps `?key=` in `sessionStorage` and removes it from the address bar.
- The API prints a warning at startup when `DEMO_API_KEY` is empty.
- `/summary` keeps the planned numbers and adds a `completed` block (finished tasks only); the dashboard
  label says "planned".

**Repo**
- `.gitignore` fixed; `requirements-dev.txt`, `pytest.ini`, a test workflow and `test_fixes.py` added.
- Line endings converted to LF; `.venv`, `.git`, databases and logs are not in this zip.
- Run once in your clone: `git rm --cached greenops.db tasks.db logs/task_4.log`

**Not changed (on purpose)**
- Fractional durations are still rounded up to whole hours (documented in `check_by_hand.py`).
- Failed agent jobs are still not retried and there is no "deadline missed" state.

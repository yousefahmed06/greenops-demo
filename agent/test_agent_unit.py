"""Run from the agent folder:  python test_agent_unit.py
Tests the agent's own rules and error handling with a fake platform and a fake runner."""
import threading
import urllib.error

import agent


class FakeClient:
    def __init__(self, jobs=None):
        self.calls, self.jobs = [], jobs or []

    def call(self, path, method="GET", body=None):
        self.calls.append((path, body))
        if path == "/agent/jobs/claim":
            jobs, self.jobs = self.jobs, []
            return jobs
        return {}


class FakeRunner:
    name = "fake"

    def __init__(self, fail_start=False):
        self.fail_start, self.started, self.codes = fail_start, [], {}

    def start(self, job):
        if self.fail_start:
            raise agent.RunnerError("docker is not installed")
        self.started.append(job["id"])
        return {"id": job["id"]}

    def poll(self, h): return self.codes.get(h["id"])
    def logs(self, h): return "output\n"
    def cleanup(self, h): pass
    def stop(self, h): pass


def reports(client):
    return [(p.split("/")[3], b["status"], b.get("log")) for p, b in client.calls if "/status" in p]


job = {"id": 1, "image": "demo-job:1", "region_key": "swedencentral", "work_seconds": 30}

# 1. a normal job: Running, then Completed with the host in the log
c, r = FakeClient([job]), FakeRunner()
a = agent.Agent(c, r, ["demo-job:1"], host="vm-1", log=lambda *x: None)
a.step()
assert r.started == [1] and reports(c)[0][:2] == ("1", "Running")
r.codes[1] = 0
a.step()
rep = reports(c)
assert rep[1][:2] == ("1", "Completed") and "agent host: vm-1" in rep[1][2] and a.running == {}
assert any(b and b["running"] == [1] for p, b in c.calls if p == "/agent/heartbeat")
print("normal job      : ok")

# 2. the client's own rules reject images that are not on the allowed list
bad = dict(job, id=2, image="evil:latest")
c, r = FakeClient([bad]), FakeRunner()
a = agent.Agent(c, r, ["demo-job:1"], host="vm-1", log=lambda *x: None)
a.step()
assert r.started == [] and reports(c)[0][:2] == ("2", "Failed") and "not on this agent" in reports(c)[0][2]
for evil in ("--privileged", "demo job", "Demo-Job:1", ""):
    assert a.local_check(dict(job, image=evil)) is not None, evil
assert a.local_check(dict(job, work_seconds=9999)) == "job is longer than this agent allows"
print("local rules     : unknown images and bad names rejected")

# 3. the runner cannot start (for example Docker is not installed): reported as Failed
c, r = FakeClient([job]), FakeRunner(fail_start=True)
a = agent.Agent(c, r, ["demo-job:1"], host="vm-1", log=lambda *x: None)
a.step()
assert reports(c)[0][:2] == ("1", "Failed") and "docker is not installed" in reports(c)[0][2] and a.running == {}
print("start failure   : reported as Failed with the reason")

# 4. a failed container (exit code 1)
c, r = FakeClient([job]), FakeRunner()
a = agent.Agent(c, r, ["demo-job:1"], host="vm-1", log=lambda *x: None)
a.step(); r.codes[1] = 1; a.step()
assert reports(c)[1][:2] == ("1", "Failed")
print("failed container: ok")


# 5. revoked token stops the loop; unreachable platform backs off and keeps trying
class RevokedClient:
    def call(self, *a, **k): raise agent.PlatformError(401, "revoked")


msgs = []
a = agent.Agent(RevokedClient(), FakeRunner(), ["demo-job:1"], host="x", log=msgs.append)
a.run_forever(threading.Event(), 0.01)
assert any("rejected this agent" in m for m in msgs)

n = {"calls": 0}


class DownClient:
    def call(self, *a, **k):
        n["calls"] += 1
        raise urllib.error.URLError("connection refused")


stop = threading.Event()
a = agent.Agent(DownClient(), FakeRunner(), ["demo-job:1"], host="x", log=lambda *x: None)
t = threading.Thread(target=a.run_forever, args=(stop, 0.05)); t.start()
threading.Event().wait(0.8); stop.set(); t.join(2)
assert 2 <= n["calls"] <= 6, n   # retried, but with growing delays (not a tight loop)
print("revoked / down  : stops on 401, backs off when unreachable")

print("Agent unit tests passed")

#!/usr/bin/env python3
"""GreenOps agent - runs on a machine in one cloud region (a VM or a Kubernetes cluster).

It only makes OUTBOUND HTTPS calls to the platform:
    heartbeat  ->  claim jobs  ->  run them locally  ->  report status

The platform never connects to this machine and never holds its credentials.
Settings (environment variables):
    PLATFORM_URL     address of the platform, for example https://my-tunnel.example.com
    AGENT_TOKEN      the token created for THIS region (shown once at enrollment)
    RUNNER           docker (default) or kubernetes
    ALLOWED_IMAGES   images this agent will run, comma separated (default: demo-job:1)
    MAX_WORK_SECONDS refuse jobs longer than this (default 600)
    MAX_CPUS / MAX_MEMORY   limits for each job (default 1 and 512m)
    POLL_SECONDS     how often to ask for work (default 5)
    DOCKER_CMD       docker command if it is not just "docker" (for example "sudo docker")
Only the Python standard library is needed for the docker runner.
Usage:  python agent.py --check     (test the connection and Docker, then exit)
        python agent.py             (run until Ctrl+C)
"""
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,199}$")   # also blocks values starting with "-"


class PlatformError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, f"{status}: {message}")
        self.status = status


class RunnerError(Exception):
    pass


# ------------------------------------------------------------------ platform
class PlatformClient:
    def __init__(self, url, token, timeout=15):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail", "")
            except Exception:
                detail = ""
            raise PlatformError(e.code, str(detail))


# ------------------------------------------------------------------- runners
class DockerRunner:
    name = "docker"

    def __init__(self, docker_cmd=None, cpus="1", memory="512m"):
        cmd = docker_cmd or os.getenv("DOCKER_CMD", "docker")
        self.docker = shlex.split(cmd, posix=(os.name != "nt"))
        self.cpus, self.memory = cpus, memory

    def build_command(self, job):
        return self.docker + [
            "run", "--rm", "--pull=never",
            "--name", f"greenops-job-{job['id']}",
            "--label", f"greenops-task={job['id']}",
            "--label", f"region={job['region_key']}",
            "--cpus", str(self.cpus), "--memory", str(self.memory),
            "-e", f"WORK_SECONDS={job['work_seconds']}",
            "-e", f"REGION={job['region_key']}",
            job["image"]]

    def start(self, job):
        cmd = self.build_command(job)
        logf = tempfile.NamedTemporaryFile("w+", prefix="greenops-", suffix=".log", delete=False)
        logf.write("$ " + " ".join(cmd) + "\n")
        logf.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        except OSError as e:
            logf.close()
            raise RunnerError(f"could not start docker ({e}). Is Docker installed and running?")
        return {"proc": proc, "logf": logf}

    def poll(self, handle):
        return handle["proc"].poll()

    def logs(self, handle):
        handle["logf"].flush()
        with open(handle["logf"].name, errors="replace") as f:
            return f.read()[-15000:]

    def cleanup(self, handle):
        try:
            handle["logf"].close()
            os.unlink(handle["logf"].name)
        except OSError:
            pass

    def stop(self, handle):
        if handle["proc"].poll() is None:
            handle["proc"].terminate()


class KubernetesRunner:
    """Creates a Kubernetes Job in one namespace. Needs: pip install kubernetes
    (not tested against a real cluster in this project - see the guide)."""
    name = "kubernetes"

    def __init__(self, namespace="greenops", cpus="1", memory="512Mi", max_seconds=600):
        from kubernetes import client, config            # imported here: optional dependency
        try:
            config.load_incluster_config()               # when running inside the cluster
        except config.ConfigException:
            config.load_kube_config()                    # when testing from a laptop
        self.batch, self.core = client.BatchV1Api(), client.CoreV1Api()
        self.api_exception = client.exceptions.ApiException
        self.namespace, self.cpus, self.memory, self.max_seconds = namespace, cpus, memory, max_seconds

    @staticmethod
    def build_manifest(job, cpus="1", memory="512Mi", max_seconds=600):
        job_id = str(job["id"])
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"gt-{job_id}",
                         "labels": {"app": "greenops-task", "greenops-task": job_id,
                                    "region": job["region_key"]}},
            "spec": {
                "backoffLimit": 0,                        # never retry silently
                "ttlSecondsAfterFinished": 600,           # clean up after 10 minutes
                "activeDeadlineSeconds": max_seconds,     # hard time limit
                "template": {
                    "metadata": {"labels": {"greenops-task": job_id}},
                    "spec": {
                        "restartPolicy": "Never",
                        "containers": [{
                            "name": "job", "image": job["image"], "imagePullPolicy": "IfNotPresent",
                            "env": [{"name": "WORK_SECONDS", "value": str(job["work_seconds"])},
                                    {"name": "REGION", "value": str(job["region_key"])}],
                            "resources": {"requests": {"cpu": str(cpus), "memory": memory},
                                          "limits": {"cpu": str(cpus), "memory": memory}}}]}}}}

    def start(self, job):
        body = self.build_manifest(job, self.cpus, self.memory, self.max_seconds)
        try:
            self.batch.create_namespaced_job(self.namespace, body)
        except self.api_exception as e:
            if e.status != 409:                           # 409 = already exists = fine (idempotent)
                raise RunnerError(f"Kubernetes refused the job: {e.reason}")
        return {"name": body["metadata"]["name"]}

    def poll(self, handle):
        status = self.batch.read_namespaced_job_status(handle["name"], self.namespace).status
        if status.succeeded:
            return 0
        if status.failed:
            return 1
        return None

    def logs(self, handle):
        pods = self.core.list_namespaced_pod(self.namespace, label_selector=f"job-name={handle['name']}")
        if not pods.items:
            return "(no pod found)"
        try:
            return self.core.read_namespaced_pod_log(pods.items[0].metadata.name, self.namespace,
                                                     tail_lines=200)[-15000:]
        except self.api_exception as e:
            return f"(could not read logs: {e.reason})"

    def cleanup(self, handle):
        pass

    def stop(self, handle):
        pass


# --------------------------------------------------------------------- agent
class Agent:
    def __init__(self, client, runner, allowed_images, max_seconds=600, host=None, log=print):
        self.client, self.runner = client, runner
        self.allowed = list(allowed_images)
        self.max_seconds = max_seconds
        self.host = host or socket.gethostname()
        self.log = log
        self.running = {}                                # job id -> runner handle

    def header(self):
        return f"agent host: {self.host} | runner: {self.runner.name}\n"

    def local_check(self, job):
        """The CLIENT's own rules. Even a compromised platform cannot bypass them."""
        image = str(job.get("image", ""))
        if not IMAGE_RE.match(image):
            return "image name is not valid"
        if image not in self.allowed:
            return f"image '{image}' is not on this agent's allowed list"
        if int(job.get("work_seconds", 0)) > self.max_seconds:
            return "job is longer than this agent allows"
        return None

    def report(self, job_id, status, exit_code=None, log=None):
        self.client.call(f"/agent/jobs/{job_id}/status", "POST",
                         {"status": status, "exit_code": exit_code, "log": log})

    def _start(self, job):
        reason = self.local_check(job)
        if reason:
            self.log(f"job {job['id']} rejected: {reason}")
            self.report(job["id"], "Failed", None, self.header() + "Rejected by the agent: " + reason + "\n")
            return
        try:
            handle = self.runner.start(job)
        except Exception as e:                            # RunnerError or anything unexpected
            self.log(f"job {job['id']} could not start: {e}")
            self.report(job["id"], "Failed", None, self.header() + f"Could not start: {e}\n")
            return
        self.running[job["id"]] = handle
        self.log(f"job {job['id']} started ({job['image']})")
        self.report(job["id"], "Running", None, self.header())

    def _check_running(self):
        for job_id, handle in list(self.running.items()):
            code = self.runner.poll(handle)
            if code is None:
                continue
            status = "Completed" if code == 0 else "Failed"
            try:
                self.report(job_id, status, code, self.header() + self.runner.logs(handle))
            except PlatformError as e:
                if e.status not in (403, 404, 409):       # platform said "I don't know this job": drop it
                    raise
            self.log(f"job {job_id} {status.lower()} (exit code {code})")
            self.runner.cleanup(handle)
            del self.running[job_id]

    def step(self):
        """One round: heartbeat, collect new jobs, start them, report finished ones."""
        self.client.call("/agent/heartbeat", "POST", {"running": sorted(self.running), "host": self.host})
        for job in self.client.call("/agent/jobs/claim", "POST", {}) or []:
            self._start(job)
        self._check_running()

    def run_forever(self, stop_event, poll_seconds=5):
        delay = poll_seconds
        while not stop_event.is_set():
            try:
                self.step()
                delay = poll_seconds
            except PlatformError as e:
                if e.status == 401:
                    self.log("The platform rejected this agent's token (revoked or wrong). Stopping.")
                    return
                self.log(f"platform error {e}; retrying")
                delay = min(delay * 2, 60)
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.log(f"cannot reach the platform ({e}); retrying in {delay:.0f}s")
                delay = min(delay * 2, 60)               # exponential backoff
            stop_event.wait(delay)
        for handle in self.running.values():
            self.runner.stop(handle)


# ----------------------------------------------------------------------- cli
def build_runner():
    kind = os.getenv("RUNNER", "docker").lower()
    cpus = os.getenv("MAX_CPUS", "1")
    if kind == "kubernetes":
        return KubernetesRunner(os.getenv("K8S_NAMESPACE", "greenops"), cpus,
                                os.getenv("MAX_MEMORY", "512Mi"),
                                int(os.getenv("MAX_WORK_SECONDS", "600")))
    return DockerRunner(cpus=cpus, memory=os.getenv("MAX_MEMORY", "512m"))


def main():
    url, token = os.getenv("PLATFORM_URL", ""), os.getenv("AGENT_TOKEN", "")
    if not url or not token:
        sys.exit("Set PLATFORM_URL and AGENT_TOKEN first (see the guide).")
    client = PlatformClient(url, token)
    runner = build_runner()
    agent = Agent(client, runner,
                  [i.strip() for i in os.getenv("ALLOWED_IMAGES", "demo-job:1").split(",")],
                  int(os.getenv("MAX_WORK_SECONDS", "600")))

    if "--check" in sys.argv:
        try:
            hb = client.call("/agent/heartbeat", "POST", {"running": [], "host": agent.host})
        except PlatformError as e:
            sys.exit(f"Platform answered {e}. Check the token and that the platform is running.")
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"Cannot reach {url}: {e}")
        print(f"OK  platform reachable, this agent serves region: {hb['region']}")
        if isinstance(runner, DockerRunner):
            try:
                out = subprocess.run(runner.docker + ["version", "--format", "{{.Server.Version}}"],
                                     capture_output=True, text=True, timeout=20)
                print("OK  docker server " + out.stdout.strip() if out.returncode == 0
                      else "FAIL docker: " + (out.stderr.strip() or "not running"))
            except (OSError, subprocess.TimeoutExpired) as e:
                print(f"FAIL docker not usable: {e}")
        print("allowed images:", agent.allowed)
        return

    stop = threading.Event()
    print(f"agent started: host={agent.host} runner={runner.name} platform={url}")
    try:
        agent.run_forever(stop, float(os.getenv("POLL_SECONDS", "5")))
    except KeyboardInterrupt:
        stop.set()
        print("agent stopped")


if __name__ == "__main__":
    main()

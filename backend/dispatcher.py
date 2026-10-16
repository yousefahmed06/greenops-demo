"""Starts a container with 'docker run'. Kept tiny so it is easy to replace later."""
import os
import subprocess
import time

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")


def build_command(image, region_key, task_id, work_seconds):
    return [DOCKER_BIN, "run", "--rm",
            "--name", f"greenops-task-{task_id}-{int(time.time())}",
            "--label", f"region={region_key}",
            "--label", f"task_id={task_id}",
            "-e", f"WORK_SECONDS={work_seconds}",
            "-e", f"REGION={region_key}",
            image]


def start_container(cmd, log_file):
    """Start the process and return immediately (does not wait for it to finish)."""
    return subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)

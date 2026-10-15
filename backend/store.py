"""All database access (SQLite) in one place."""
import json
import sqlite3
from contextlib import closing

NEW_COLUMNS = {"task": "TEXT", "started_at": "TEXT", "finished_at": "TEXT",
               "exit_code": "INTEGER", "log_path": "TEXT",
               "executor": "TEXT", "lease_until": "REAL", "started_real": "REAL"}
COLS = "id, status, decision, task, started_at, finished_at, exit_code, log_path, executor"


def connect(path):
    return closing(sqlite3.connect(path))


def init(path):
    with connect(path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            decision TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS agents (
            region TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            last_seen REAL)""")
        have = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        for col, typ in NEW_COLUMNS.items():       # upgrade a Day 2 database
            if col not in have:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        con.commit()


def _row(r):
    return {"id": r[0], "status": r[1], "decision": json.loads(r[2]),
            "task": json.loads(r[3]) if r[3] else None,
            "started_at": r[4], "finished_at": r[5],
            "exit_code": r[6], "log_path": r[7], "executor": r[8]}


def public(row):
    """What the API returns: the frozen decision format + two time fields."""
    name = (row["task"] or {}).get("name")
    return {"id": row["id"], "name": name, "status": row["status"], **row["decision"],
            "started_at": row["started_at"], "finished_at": row["finished_at"]}


def insert(path, decision, task):
    with connect(path) as con:
        cur = con.execute(
            "INSERT INTO tasks (status, decision, task) VALUES ('Scheduled', ?, ?)",
            (json.dumps(decision), json.dumps(task)))
        con.commit()
        return cur.lastrowid


def get(path, task_id):
    with connect(path) as con:
        r = con.execute(f"SELECT {COLS} FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row(r) if r else None


def list_all(path):
    with connect(path) as con:
        rows = con.execute(f"SELECT {COLS} FROM tasks ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def scheduled(path):
    with connect(path) as con:
        rows = con.execute(f"SELECT {COLS} FROM tasks "
                           "WHERE status = 'Scheduled' AND task IS NOT NULL ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def claim(path, task_id):
    """Scheduled -> Dispatching. Returns True only for the first caller (no double start)."""
    with connect(path) as con:
        cur = con.execute("UPDATE tasks SET status = 'Dispatching' "
                          "WHERE id = ? AND status = 'Scheduled'", (task_id,))
        con.commit()
        return cur.rowcount == 1


def mark_running(path, task_id, started_at, log_path):
    with connect(path) as con:
        con.execute("UPDATE tasks SET status = 'Running', started_at = ?, log_path = ? "
                    "WHERE id = ?", (started_at, log_path, task_id))
        con.commit()


def mark_done(path, task_id, status, finished_at, exit_code):
    with connect(path) as con:
        con.execute("UPDATE tasks SET status = ?, finished_at = ?, exit_code = ? "
                    "WHERE id = ?", (status, finished_at, exit_code, task_id))
        con.commit()


def fail_orphans(path):
    """After a restart, jobs we were watching are lost: mark them Failed."""
    with connect(path) as con:
        con.execute("UPDATE tasks SET status = 'Failed' "
                    "WHERE status IN ('Running', 'Dispatching') "
                    "AND (executor IS NULL OR executor = 'local')")
        con.commit()


def reset(path):
    with connect(path) as con:
        con.execute("DELETE FROM tasks")
        con.execute("DELETE FROM sqlite_sequence WHERE name = 'tasks'")
        con.commit()


# ---------------------------------------------------------------- agents
def set_agent(path, region, token_hash, now_epoch):
    """Enroll (or re-enroll, which replaces the old token) the agent of a region."""
    with connect(path) as con:
        con.execute("INSERT OR REPLACE INTO agents (region, token_hash, created_at, last_seen) "
                    "VALUES (?, ?, ?, NULL)", (region, token_hash, now_epoch))
        con.commit()


def delete_agent(path, region):
    with connect(path) as con:
        cur = con.execute("DELETE FROM agents WHERE region = ?", (region,))
        con.commit()
        return cur.rowcount == 1


def agent_for_token(path, token_hash):
    with connect(path) as con:
        r = con.execute("SELECT region FROM agents WHERE token_hash = ?", (token_hash,)).fetchone()
    return r[0] if r else None


def touch_agent(path, region, now_epoch):
    with connect(path) as con:
        con.execute("UPDATE agents SET last_seen = ? WHERE region = ?", (now_epoch, region))
        con.commit()


def agents(path):
    with connect(path) as con:
        rows = con.execute("SELECT region, created_at, last_seen FROM agents ORDER BY region").fetchall()
    return [{"region": r[0], "created_at": r[1], "last_seen": r[2]} for r in rows]


def agent_status(path, region, now_epoch, online_window):
    """None if no agent is enrolled for the region, else {'online': bool}."""
    with connect(path) as con:
        r = con.execute("SELECT last_seen FROM agents WHERE region = ?", (region,)).fetchone()
    if r is None:
        return None
    return {"online": r[0] is not None and now_epoch - r[0] <= online_window}


def queue(path, task_id):
    """Scheduled -> Queued: waiting for the agent of the chosen region to pick it up."""
    with connect(path) as con:
        cur = con.execute("UPDATE tasks SET status = 'Queued', executor = 'agent' "
                          "WHERE id = ? AND status = 'Scheduled'", (task_id,))
        con.commit()
        return cur.rowcount == 1


def claim_queued(path, region, lease_until):
    """Hand the queued tasks of a region to its agent (each task is handed out once)."""
    claimed = []
    with connect(path) as con:
        rows = con.execute(f"SELECT {COLS} FROM tasks WHERE status = 'Queued' ORDER BY id").fetchall()
        for r in rows:
            row = _row(r)
            if row["decision"]["region"] != region:
                continue
            cur = con.execute("UPDATE tasks SET status = 'Dispatching', lease_until = ? "
                              "WHERE id = ? AND status = 'Queued'", (lease_until, row["id"]))
            if cur.rowcount == 1:
                claimed.append(row)
        con.commit()
    return claimed


def requeue_expired(path, now_epoch):
    """An agent took a task but never confirmed it: offer it again."""
    with connect(path) as con:
        cur = con.execute("UPDATE tasks SET status = 'Queued', lease_until = NULL "
                          "WHERE status = 'Dispatching' AND executor = 'agent' "
                          "AND lease_until IS NOT NULL AND lease_until < ?", (now_epoch,))
        con.commit()
        return cur.rowcount


def report(path, task_id, region, new_status, sim_iso, exit_code, now_epoch):
    """Apply a status report from an agent. Returns (ok, message)."""
    row = get(path, task_id)
    if row is None:
        return False, "Task not found"
    if row["decision"]["region"] != region or row["executor"] != "agent":
        return False, "This task does not belong to your agent"
    cur_status = row["status"]
    if cur_status == new_status:
        return True, "already " + new_status
    with connect(path) as con:
        if cur_status == "Dispatching" and new_status == "Running":
            con.execute("UPDATE tasks SET status = 'Running', started_at = ?, started_real = ?, "
                        "lease_until = NULL WHERE id = ?", (sim_iso, now_epoch, task_id))
        elif cur_status in ("Dispatching", "Running") and new_status in ("Completed", "Failed"):
            if row["started_at"] is None:
                con.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (sim_iso, task_id))
            con.execute("UPDATE tasks SET status = ?, finished_at = ?, exit_code = ?, "
                        "lease_until = NULL WHERE id = ?", (new_status, sim_iso, exit_code, task_id))
        else:
            return False, f"Cannot change {cur_status} to {new_status}"
        con.commit()
    return True, "ok"


def set_log_path(path, task_id, log_path):
    with connect(path) as con:
        con.execute("UPDATE tasks SET log_path = ? WHERE id = ?", (log_path, task_id))
        con.commit()


def fail_untracked(path, region, known_ids, now_epoch, grace, sim_iso):
    """Running tasks that the agent no longer lists in its heartbeat are marked Failed."""
    failed = []
    with connect(path) as con:
        rows = con.execute(f"SELECT {COLS}, started_real FROM tasks "
                           "WHERE status = 'Running' AND executor = 'agent'").fetchall()
        for r in rows:
            row = _row(r[:9])
            started_real = r[9]
            if (row["decision"]["region"] == region and row["id"] not in known_ids
                    and (started_real is None or now_epoch - started_real >= grace)):
                con.execute("UPDATE tasks SET status = 'Failed', finished_at = ? WHERE id = ?",
                            (sim_iso, row["id"]))
                failed.append(row["id"])
        con.commit()
    return failed


def fail_agent_jobs(path, region, sim_iso):
    """The agent has been silent too long: its Running/Dispatching tasks are lost."""
    failed = []
    with connect(path) as con:
        rows = con.execute(f"SELECT {COLS} FROM tasks "
                           "WHERE status IN ('Running', 'Dispatching') AND executor = 'agent'").fetchall()
        for r in rows:
            row = _row(r)
            if row["decision"]["region"] == region:
                con.execute("UPDATE tasks SET status = 'Failed', finished_at = ? WHERE id = ?",
                            (sim_iso, row["id"]))
                failed.append(row["id"])
        con.commit()
    return failed

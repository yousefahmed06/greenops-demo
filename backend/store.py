"""All database access (SQLite) in one place."""
import json
import sqlite3
from contextlib import closing

NEW_COLUMNS = {"task": "TEXT", "started_at": "TEXT", "finished_at": "TEXT",
               "exit_code": "INTEGER", "log_path": "TEXT"}
COLS = "id, status, decision, task, started_at, finished_at, exit_code, log_path"


def connect(path):
    return closing(sqlite3.connect(path))


def init(path):
    with connect(path) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            decision TEXT NOT NULL)""")
        have = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        for col, typ in NEW_COLUMNS.items():       # upgrade a Day 2 database
            if col not in have:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typ}")
        con.commit()


def _row(r):
    return {"id": r[0], "status": r[1], "decision": json.loads(r[2]),
            "task": json.loads(r[3]) if r[3] else None,
            "started_at": r[4], "finished_at": r[5],
            "exit_code": r[6], "log_path": r[7]}


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
                    "WHERE status IN ('Running', 'Dispatching')")
        con.commit()


def reset(path):
    with connect(path) as con:
        con.execute("DELETE FROM tasks")
        con.execute("DELETE FROM sqlite_sequence WHERE name = 'tasks'")
        con.commit()

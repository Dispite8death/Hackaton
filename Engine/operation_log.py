"""Durable checkpoints for operator-initiated work. One writer per database."""
import json
import time
from contextlib import ExitStack
from threading import Event

from .audit_worker import _cycle_lock

stopping = Event()


class ServerStopping(KeyboardInterrupt):
    """Allows the crawler to close its active visit as interrupted."""


def checkpoint():
    if stopping.is_set():
        raise ServerStopping("Сервер остановлен. Сохранённые этапы доступны для продолжения.")


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS portal_operation (
        id INTEGER PRIMARY KEY, record TEXT NOT NULL
    )""")


def lease(conn):
    stack = ExitStack()
    if not stack.enter_context(_cycle_lock(conn)):
        stack.close()
        raise ValueError("База занята другой операцией. Дождитесь её завершения.")
    return stack


def latest(conn):
    row = conn.execute("SELECT record FROM portal_operation ORDER BY id DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else {"state": "idle"}


def save(conn, job):
    now = time.time()
    job["elapsed"] += max(0, now - job["updated"])
    job["updated"] = now
    with conn:
        conn.execute("UPDATE portal_operation SET record=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"]))


def recover(conn, on_interrupt=None):
    job = latest(conn)
    if job["state"] == "running":
        # Called only while holding the OS lease. The previous owner has exited.
        if on_interrupt:
            on_interrupt(conn, job)
        job["updated"] = time.time()
        job["state"] = "interrupted"
        save(conn, job)
    return job


def status(conn, on_interrupt=None):
    job = latest(conn)
    if job["state"] == "running":
        with _cycle_lock(conn) as owned:
            if owned:
                job = recover(conn, on_interrupt)
    return job


def begin(conn, mode, steps, *, resume_id=None, context=None):
    previous = recover(conn)
    now = time.time()
    if resume_id is not None:
        if previous.get("id") != resume_id or previous["state"] not in ("interrupted", "error"):
            raise ValueError("Это задание уже изменилось. Обновите страницу.")
        previous.update(state="running", updated=now, detail="Продолжаем с сохранённого этапа")
        save(conn, previous)
        return previous
    job = dict(mode=mode, steps=steps, total=len(steps), done=0, state="running",
               started=now, updated=now, elapsed=0, detail="Начинаем", notes=[], context=context or {})
    with conn:
        job["id"] = conn.execute("INSERT INTO portal_operation(record) VALUES('{}')").lastrowid
        save(conn, job)
    return job


def complete_step(conn, job, note=None):
    job["done"] += 1
    if note:
        job["notes"].append(note)
    if job["done"] == job["total"]:
        job.update(state="completed", detail="Все этапы завершены")
    save(conn, job)


def stop(conn, error):
    conn.rollback()
    job = latest(conn)
    job.update(state="error" if isinstance(error, Exception) else "interrupted",
               detail=str(error)[:500] or job["detail"])
    save(conn, job)

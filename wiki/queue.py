"""Single persistent worker; checkpoints yield to chat and preserve resumable work."""
import asyncio
import json
import threading
import time
from uuid import uuid4


class Deferred(Exception):
    pass


class JobQueue:
    def __init__(self, store, settings, busy, resources, execute):
        self.store, self.settings = store, settings
        self.busy, self.resources, self.execute = busy, resources, execute
        self.task = None
        self.current = None
        self.force = False
        self.last_activity = time.monotonic()
        self.reason = ""
        self.guard = asyncio.Lock()
        self.committing = None
        self.mutation_lock = threading.Lock()

    def enqueue(self, kind, payload, priority=4, dedupe=None, review=False):
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if dedupe:
                old = db.execute("SELECT id FROM jobs WHERE dedupe=? AND status IN ('pending','running','review')", (dedupe,)).fetchone()
                if old:
                    return old[0]
            job_id = uuid4().hex
            db.execute("INSERT INTO jobs(id,kind,payload,priority,status,created,updated,dedupe) VALUES(?,?,?,?,?,?,?,?)", (job_id, kind, json.dumps(payload, ensure_ascii=False), priority, "review" if review else "pending", now, now, dedupe))
        return job_id

    def jobs(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 100").fetchall()
            counts = dict(db.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall())
        return {"jobs": [{**dict(r), "payload": json.loads(r["payload"]), "result": json.loads(r["result"]) if r["result"] else None} for r in rows], "counts": counts, "reason": self.reason, "running": self.current}

    def cancel(self, job_id):
        with self.mutation_lock:
            if self.committing == job_id:
                return {"status": "running", "message": "Écriture atomique déjà engagée ; consultez son résultat"}
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN ('pending','running','review')", (time.time(), job_id))
            if self.current == job_id:
                self.cancel_task()
        return {"status": "cancelled"}

    def cancel_task(self):
        # Settings/API mutations run in FastAPI's threadpool.
        task = self.task
        if task and not task.done():
            try:
                task.get_loop().call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # A closed loop is recovered from SQLite at next startup.

    def checkpoint(self, job_id=None, check_resources=True):
        settings = self.settings()
        if not settings["enabled"] or settings["paused"] or self.busy():
            raise Deferred("Priorité au chat, pause ou ressources insuffisantes")
        resource_exempt = False
        if job_id:
            with self.store.connect() as db:
                row = db.execute("SELECT status,kind,payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] != "running":
                raise Deferred("Tâche annulée")
            resource_exempt = (row["kind"] in {"write", "notion"} or
                               (row["kind"] == "extract" and bool(json.loads(row["payload"]).get("use_api"))))
        if check_resources and not resource_exempt and not self.resources():
            raise Deferred("Priorité au chat, pause ou ressources insuffisantes")

    async def tick(self):
        if self.guard.locked():
            return False
        async with self.guard:
            cfg = self.settings()
            try:
                # Select work before applying its resource gate: remote API
                # extraction uses no local inference memory and can run on a
                # modest machine without waiting for the RAM threshold.
                self.checkpoint(check_resources=False)
            except Deferred as exc:
                self.reason = str(exc)
                return False
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                # API extraction jobs bypass idle/manual mode; their worker
                # checkpoint skips only the local RAM gate, never pause/chat.
                immediate = bool(db.execute("SELECT 1 FROM jobs WHERE status='pending' AND priority<=3 LIMIT 1").fetchone())
                if not immediate and not self.force and (cfg["queue_mode"] == "manual" or time.monotonic() - self.last_activity < cfg["idle_seconds"]):
                    self.reason = "En attente d’inactivité ou d’un lancement manuel"
                    return False
                row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY priority,created LIMIT 1").fetchone()
                if not row:
                    self.force = False
                    self.reason = "File vide"
                    return False
                # Check the selected job before marking it running. This avoids
                # retrying a lightweight queue poll every second when RAM is low.
                light_job = (row["kind"] in {"write", "notion"} or
                             (row["kind"] == "extract" and bool(json.loads(row["payload"]).get("use_api"))))
                if not light_job and not self.resources():
                    self.reason = "Priorité au chat, pause ou ressources insuffisantes"
                    return False
                db.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated=? WHERE id=?", (time.time(), row["id"]))
            self.current = row["id"]
            self.reason = "Traitement en cours"
            try:
                self.task = asyncio.create_task(self.execute(dict(row)))
                result = await self.task
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET status='completed',progress=1,result=?,updated=? WHERE id=? AND status='running'", (json.dumps(result, ensure_ascii=False), time.time(), row["id"]))
            except (Deferred, asyncio.CancelledError) as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET status='pending',error=?,updated=? WHERE id=? AND status='running'", (str(exc)[:300], time.time(), row["id"]))
                if isinstance(exc, asyncio.CancelledError) and asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                with self.store.connect() as db:
                    db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE id=? AND status='running'", (str(exc)[:300], time.time(), row["id"]))
            finally:
                self.task = None
                self.current = None
                self.committing = None
            return True

    def recover(self):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='pending',error='Reprise après interruption' WHERE status='running'")

    def interrupt(self):
        self.last_activity = time.monotonic()
        with self.mutation_lock:
            if self.committing != self.current or self.current is None:
                self.cancel_task()

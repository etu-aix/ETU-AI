"""Markdown is the source of truth; SQLite is a rebuildable search index and journal."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import threading
import time
from uuid import uuid4

from wiki.frontmatter import properties, names


class Conflict(ValueError):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunks(text, size=1400):
    # Bounded chunks, including for a single very long paragraph.
    for start in range(0, len(text), size - 160):
        yield start, text[start:start + size]


class WikiStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root.parent / (self.root.name + ".sqlite3")
        self.lock = threading.RLock()
        self._scan_lock = threading.Lock()
        self._last_scan = 0.0
        self._file_stats = {}
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents(path TEXT PRIMARY KEY, title TEXT, hash TEXT,
                    origin TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, path TEXT, offset INTEGER,
                    text TEXT, hash TEXT, vector TEXT, model TEXT);
                CREATE INDEX IF NOT EXISTS chunk_path ON chunks(path);
                CREATE TABLE IF NOT EXISTS links(source TEXT, target TEXT, PRIMARY KEY(source,target));
                CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, path TEXT, content TEXT,
                    origin TEXT, created REAL, deleted INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, kind TEXT, payload TEXT, priority INTEGER,
                    status TEXT, progress REAL DEFAULT 0, attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    created REAL, updated REAL, result TEXT, dedupe TEXT);
                CREATE INDEX IF NOT EXISTS job_ready ON jobs(status,priority,created);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, conversation TEXT, message TEXT,
                    data TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
            if "metadata" not in columns:
                db.execute("ALTER TABLE documents ADD COLUMN metadata TEXT DEFAULT '{}' ")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text, path UNINDEXED, tokenize='unicode61 remove_diacritics 2')")
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, name):
        if not isinstance(name, str) or len(name) > 220 or "\\" in name or ":" in name:
            raise ValueError("Chemin Markdown invalide")
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or any(p in {"..", "."} or p.startswith(".") or p.endswith((" ", ".")) for p in parts):
            raise ValueError("Chemin Markdown invalide")
        if not name.endswith(".md") or any(re.search(r'[<>"|?*\x00-\x1f]', p) for p in parts):
            raise ValueError("Un fichier .md local est requis")
        if any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p, re.I) for p in parts):
            raise ValueError("Nom de fichier réservé")
        target = self.root.joinpath(*parts)
        for part in [target, *target.parents]:
            if part == self.root:
                break
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise ValueError("Les liens de fichiers sont refusés")
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("Chemin hors du Wiki")
        return target

    def read(self, name):
        target = self.path(name)
        if not target.exists():
            raise FileNotFoundError(name)
        content, _ = self._stable_content(target)
        with self.connect() as db:
            row = db.execute("SELECT origin, updated FROM documents WHERE path=?", (name,)).fetchone()
        metadata, _ = properties(content)
        return {"path": name, "content": content, "hash": digest(content), "frontmatter": metadata,
                **(dict(row) if row else {})}

    @staticmethod
    def _stable_content(target):
        for _ in range(3):
            before = target.stat()
            if before.st_size > 256_000:
                raise ValueError("Document limité à 256 Ko")
            content = target.read_text(encoding="utf-8")
            after = target.stat()
            fingerprint = (after.st_mtime_ns, after.st_size, after.st_ino)
            if (before.st_mtime_ns, before.st_size, before.st_ino) == fingerprint:
                return content, fingerprint
        raise Conflict("Document modifié pendant sa lecture : réessayez")

    def _index(self, db, name, content, origin):
        metadata, body = properties(content)
        title = next((line.lstrip("# ").strip() for line in body.splitlines() if line.startswith("# ")),
                     str(metadata.get("title") or Path(name).stem))
        db.execute("INSERT OR REPLACE INTO documents(path,title,hash,origin,updated,metadata) VALUES(?,?,?,?,?,?)",
                   (name, title[:240], digest(content), json.dumps(origin, ensure_ascii=False), time.time(),
                    json.dumps(metadata, ensure_ascii=False)))
        if self.fts:
            db.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (name,))
        old = {r["hash"]: (r["vector"], r["model"]) for r in db.execute("SELECT hash,vector,model FROM chunks WHERE path=?", (name,))}
        db.execute("DELETE FROM chunks WHERE path=?", (name,))
        for offset, text in chunks(content):
            h = digest(text)
            vector, model = old.get(h, (None, None))
            row = db.execute("INSERT INTO chunks(path,offset,text,hash,vector,model) VALUES(?,?,?,?,?,?)", (name, offset, text, h, vector, model))
            if self.fts:
                db.execute("INSERT INTO search(rowid,text,path) VALUES(?,?,?)", (row.lastrowid, text, name))
        db.execute("DELETE FROM links WHERE source=?", (name,))
        # Links are references, never paths to read or execute.
        for target in re.findall(r"\[\[([^\]\n]{1,220})\]\]", content):
            target = target.split("|", 1)[0].split("#", 1)[0].strip()
            if target:
                db.execute("INSERT OR IGNORE INTO links VALUES(?,?)", (name, target))

    def write(self, name, content, expected_hash, origin=None, delete=False):
        if not isinstance(content, str) or len(content.encode("utf-8")) > 256_000:
            raise ValueError("Document limité à 256 Ko")
        target = self.path(name)
        origin = origin or {"type": "manual"}
        with self.lock, self.connect() as db:
            # SQLite serializes writers, including another ETU process.
            db.execute("BEGIN IMMEDIATE")
            alias = db.execute("SELECT path FROM documents WHERE path=? COLLATE NOCASE", (name,)).fetchone()
            if alias and alias[0] != name:
                raise Conflict("Utilisez la casse du chemin existant : " + alias[0])
            previous = self.read(name)["content"] if target.exists() else None
            actual = digest(previous) if previous is not None else None
            if actual != expected_hash:
                raise Conflict("Document modifié : rechargez avant d’enregistrer")
            if previous == content and not delete:
                return {"path": name, "hash": actual, "unchanged": True}
            if previous is not None:
                last = db.execute("SELECT content,deleted FROM revisions WHERE path=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
                if not last or last["content"] != previous or last["deleted"]:
                    db.execute("INSERT INTO revisions(path,content,origin,created) VALUES(?,?,?,?)", (name, previous, json.dumps({"type": "before_edit"}), time.time()))
            if delete:
                target.unlink(missing_ok=True)
                if self.fts:
                    db.execute("DELETE FROM search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (name,))
                db.execute("DELETE FROM documents WHERE path=?", (name,))
                db.execute("DELETE FROM chunks WHERE path=?", (name,))
                db.execute("DELETE FROM links WHERE source=?", (name,))
                self._file_stats.pop(name, None)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
                try:
                    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # An external editor does not take our SQLite lock. Check
                    # again immediately before replacing its on-disk version.
                    latest = self._stable_content(target)[0] if target.exists() else None
                    if (digest(latest) if latest is not None else None) != actual:
                        raise Conflict("Document modifié par une autre application : rechargez avant d’enregistrer")
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                self._index(db, name, content, origin)
                latest, fingerprint = self._stable_content(target)
                if digest(latest) == digest(content):
                    self._file_stats[name] = fingerprint
                else:
                    self._file_stats.pop(name, None)
            db.execute("INSERT INTO revisions(path,content,origin,created,deleted) VALUES(?,?,?,?,?)", (name, content if not delete else previous or "", json.dumps(origin, ensure_ascii=False), time.time(), int(delete)))
        return {"path": name, "hash": None if delete else digest(content), "deleted": delete}

    def documents(self, query="", limit=1000):
        with self.connect() as db:
            needle = f"%{query[:200]}%"
            return [dict(r) for r in db.execute("SELECT * FROM documents WHERE path LIKE ? OR title LIKE ? OR metadata LIKE ? ORDER BY path LIMIT ?",
                                              (needle, needle, needle, min(2000, limit)))]

    def history(self, name):
        self.path(name)
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM revisions WHERE path=? ORDER BY id DESC LIMIT 50", (name,))]

    def deleted(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT path,max(created) AS updated FROM revisions WHERE path NOT IN (SELECT path FROM documents) GROUP BY path ORDER BY updated DESC LIMIT 200")]

    def restore(self, name, revision, expected_hash):
        with self.connect() as db:
            row = db.execute("SELECT content FROM revisions WHERE id=? AND path=?", (revision, name)).fetchone()
        if not row:
            raise FileNotFoundError("Révision introuvable")
        return self.write(name, row["content"], expected_hash, {"type": "restore", "revision": revision})

    def graph(self, query=""):
        docs = self.documents(query)
        paths = {r["path"] for r in docs}
        lookup = {}
        exact = {}
        for doc in self.documents(limit=2000):
            exact[doc["path"].casefold()] = doc["path"]
            exact[doc["path"].removesuffix(".md").casefold()] = doc["path"]
            metadata = json.loads(doc.get("metadata") or "{}")
            for alias in (Path(doc["path"]).stem, doc["title"], *names(metadata, "aliases"),
                          *names(metadata, "alias")):
                lookup.setdefault(alias.casefold(), set()).add(doc["path"])
        edges = []
        seen_edges = set()
        with self.connect() as db:
            for link in db.execute("SELECT * FROM links"):
                target_name = link["target"].casefold()
                targets = {exact[target_name]} if target_name in exact else lookup.get(target_name, set())
                if len(targets) == 1:
                    target = next(iter(targets))
                    key = (link["source"], target)
                    if link["source"] in paths and target in paths and key not in seen_edges:
                        edges.append({"source": link["source"], "target": target})
                        seen_edges.add(key)
        return {"nodes": [{"id": d["path"], "title": d["title"],
                            "aliases": names(json.loads(d.get("metadata") or "{}"), "aliases"),
                            "category": d["path"].split("/")[0] if "/" in d["path"] else "Wiki"}
                           for d in docs], "edges": edges, "limit": 1000}

    def reconcile(self, checkpoint=lambda: None):
        # Streaming traversal; stat checks avoid re-reading unchanged files on
        # frequent scans, while hashes remain the authority for changed files.
        seen = set()
        changed = 0
        for directory, folders, files in os.walk(self.root, followlinks=False):
            folders[:] = [f for f in folders if not f.startswith(".") and not Path(directory, f).is_symlink() and not (hasattr(Path(directory, f), "is_junction") and Path(directory, f).is_junction())]
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                checkpoint()
                name = Path(directory, filename).relative_to(self.root).as_posix()
                try:
                    target = self.path(name)
                    stat = target.stat()
                    fingerprint = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    if self._file_stats.get(name) == fingerprint:
                        seen.add(name)
                        continue
                    doc = self.read(name)
                except (OSError, ValueError):
                    continue
                seen.add(name)
                with self.lock, self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("SELECT hash FROM documents WHERE path=?", (name,)).fetchone()
                    if not old or old[0] != doc["hash"]:
                        # Re-read under the writer lock to avoid racing an API edit.
                        try:
                            doc = self.read(name)
                        except (OSError, ValueError):
                            continue
                        origin = {"type": "filesystem"}
                        self._index(db, name, doc["content"], origin)
                        db.execute("INSERT INTO revisions(path,content,origin,created) VALUES(?,?,?,?)", (name, doc["content"], json.dumps(origin), time.time()))
                        changed += 1
                    try:
                        latest, fingerprint = self._stable_content(target)
                    except (OSError, ValueError):
                        self._file_stats.pop(name, None)
                    else:
                        if digest(latest) == doc["hash"]:
                            self._file_stats[name] = fingerprint
                        else:
                            self._file_stats.pop(name, None)
        for doc in self.documents(limit=2000):
            checkpoint()
            if doc["path"] not in seen and not self.path(doc["path"]).exists():
                self.write(doc["path"], "", None, {"type": "filesystem_delete"}, delete=True)
                changed += 1
        self._last_scan = time.monotonic()
        return changed

    def sync_if_due(self, interval=3):
        """Pick up edits from Obsidian/other editors without a watcher dependency."""
        if time.monotonic() - self._last_scan < interval:
            return 0
        if not self._scan_lock.acquire(blocking=False):
            return 0
        try:
            if time.monotonic() - self._last_scan >= interval:
                return self.reconcile()
            return 0
        finally:
            self._scan_lock.release()

    def event(self, conversation, message, data):
        with self.connect() as db:
            db.execute("INSERT INTO events(conversation,message,data,created) VALUES(?,?,?,?)", (conversation, message, json.dumps(data, ensure_ascii=False), time.time()))

    def events(self, conversation):
        with self.connect() as db:
            return [{"id": r["id"], "message_id": r["message"], **json.loads(r["data"])} for r in db.execute("SELECT * FROM events WHERE conversation=? ORDER BY id LIMIT 500", (conversation,))]

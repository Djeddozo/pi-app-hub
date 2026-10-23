import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

class Registry:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS apps (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_key TEXT UNIQUE NOT NULL,
                    discovered TEXT NOT NULL, overrides TEXT NOT NULL DEFAULT '{}',
                    last_seen REAL NOT NULL, missing INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, app_id TEXT, outcome TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=15) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def reconcile(self, source, snapshots):
        counts = {"new": 0, "updated": 0, "missing": 0}
        now, seen = time.time(), set()
        with self.db() as db:
            for app in snapshots:
                key = app["key"]
                seen.add(key)
                row = db.execute("SELECT * FROM apps WHERE source_key=?", (key,)).fetchone()
                raw = json.dumps(app, sort_keys=True)
                if row:
                    old = json.loads(row["discovered"])
                    # Resource samples and uptime do not make every scan report an update.
                    fields = ("name", "icon", "status", "ports", "project_path", "web_url", "tailscale_url", "protected")
                    changed = any(old.get(k) != app.get(k) for k in fields) or {m["id"] for m in old["members"]} != {m["id"] for m in app["members"]}
                    counts["updated"] += int(not row["removed"] and (changed or row["missing"]))
                    db.execute("UPDATE apps SET discovered=?,last_seen=?,missing=0 WHERE id=?", (raw, now, row["id"]))
                else:
                    app_id = hashlib.sha256(key.encode()).hexdigest()[:20]
                    db.execute("INSERT INTO apps(id,source,source_key,discovered,last_seen) VALUES(?,?,?,?,?)", (app_id, source, key, raw, now))
                    counts["new"] += 1
            for row in db.execute("SELECT id,source_key,missing FROM apps WHERE source=? AND removed=0", (source,)):
                if row["source_key"] not in seen and not row["missing"]:
                    db.execute("UPDATE apps SET missing=1 WHERE id=?", (row["id"],))
                    counts["missing"] += 1
        return counts

    def list(self):
        with self.db() as db:
            rows = db.execute("SELECT * FROM apps WHERE removed=0 ORDER BY source_key").fetchall()
        apps = []
        for row in rows:
            app = json.loads(row["discovered"])
            overrides = json.loads(row["overrides"])
            app.update(overrides)
            app["protected"] = json.loads(row["discovered"]).get("protected", False) or overrides.get("protected", False)
            app.update(id=row["id"], last_seen=row["last_seen"], edited_fields=list(overrides))
            if row["missing"]:
                app.update(status="Missing", cpu=None, ram=None, uptime=0)
            apps.append(app)
        return sorted(apps, key=lambda a: a["name"].lower())

    def get(self, app_id):
        return next((a for a in self.list() if a["id"] == app_id), None)

    def edit(self, app_id, changes):
        with self.db() as db:
            row = db.execute("SELECT overrides FROM apps WHERE id=? AND removed=0", (app_id,)).fetchone()
            if not row:
                raise KeyError(app_id)
            metadata = json.loads(row["overrides"])
            metadata.update(changes)
            db.execute("UPDATE apps SET overrides=? WHERE id=?", (json.dumps(metadata), app_id))

    def remove(self, app_id):
        with self.db() as db:
            db.execute("UPDATE apps SET removed=1 WHERE id=?", (app_id,))

    def audit(self, actor, action, app_id, outcome, detail=""):
        from .security import redact
        with self.db() as db:
            db.execute("INSERT INTO activity(at,actor,action,app_id,outcome,detail) VALUES(?,?,?,?,?,?)",
                       (time.time(), actor[:200], action, app_id, outcome, redact(detail)[:2000]))
            db.execute("DELETE FROM activity WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM activity)")

    def activity(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 200")]

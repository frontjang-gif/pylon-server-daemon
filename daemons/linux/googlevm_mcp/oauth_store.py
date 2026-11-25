"""Transactional OAuth state. Only hashes of codes and bearer tokens are stored."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("CREATE TABLE IF NOT EXISTS oauth_records (kind TEXT, key TEXT, payload TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(kind, key))")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM oauth_records WHERE expires <= ?", (time.time(),))
                yield self
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def get(self, kind, key):
        row = self.db.execute("SELECT payload FROM oauth_records WHERE kind=? AND key=? AND expires>?",
                              (kind, key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, payload, expires):
        self.db.execute("INSERT OR REPLACE INTO oauth_records VALUES (?, ?, ?, ?)",
                        (kind, key, json.dumps(payload), expires))

    def delete(self, kind, key):
        self.db.execute("DELETE FROM oauth_records WHERE kind=? AND key=?", (kind, key))

    def count(self, kind):
        return self.db.execute("SELECT count(*) FROM oauth_records WHERE kind=?", (kind,)).fetchone()[0]

    def close(self):
        with self.lock:
            self.db.close()

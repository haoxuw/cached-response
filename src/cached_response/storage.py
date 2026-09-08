"""Short SQLite transactions; never hold a database lock during inference."""

import json
import os
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .config import Rule

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases (key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rules (scope TEXT NOT NULL, rule TEXT NOT NULL, PRIMARY KEY(scope, rule));
CREATE TABLE IF NOT EXISTS candidates (scope TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS verified_rules (key TEXT NOT NULL, rule TEXT NOT NULL, PRIMARY KEY(key, rule));
CREATE TABLE IF NOT EXISTS signature_counts (scope TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, requests INTEGER NOT NULL, hits INTEGER NOT NULL, misses INTEGER NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(scope, kind, signature));
CREATE TABLE IF NOT EXISTS signature_entries (scope TEXT NOT NULL, kind TEXT NOT NULL, signature TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(scope, kind, key));
CREATE INDEX IF NOT EXISTS signature_lookup ON signature_entries(scope, kind, signature);
"""
_stores_lock = threading.Lock()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self.path.is_symlink() or (
                hasattr(os, "getuid") and self.path.stat().st_uid != os.getuid()
            ):
                raise PermissionError(
                    "Cache must be a local file owned by this user"
                )
        else:
            os.close(fd)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        weakref.finalize(self, self.db.close)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        with self.lock, self.db:
            yield self.db

    def get(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT created, payload FROM entries WHERE key=?", (key,)
            ).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def put(self, key, owner, payload):
        with self.connect() as db:
            # A timed-out producer must not overwrite a newer owner's work.
            held = db.execute(
                "SELECT owner FROM leases WHERE key=?", (key,)
            ).fetchone()
            if held and held[0] == owner:
                db.execute(
                    "INSERT OR REPLACE INTO entries VALUES (?, ?, ?)",
                    (key, time.time(), payload),
                )
                db.execute(
                    "DELETE FROM leases WHERE key=? AND owner=?", (key, owner)
                )

    def claim(self, key, owner, duration):
        with self.connect() as db:
            db.execute(
                "DELETE FROM leases WHERE key=? AND expires < ?",
                (key, time.time()),
            )
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO leases VALUES (?, ?, ?)",
                    (key, owner, time.time() + duration),
                ).rowcount
            )

    def release(self, key, owner):
        with self.connect() as db:
            db.execute(
                "DELETE FROM leases WHERE key=? AND owner=?", (key, owner)
            )

    def learned(self, scope):
        with self.connect() as db:
            rows = db.execute(
                "SELECT rule FROM rules WHERE scope=? ORDER BY rule", (scope,)
            ).fetchall()
        return [Rule(**json.loads(row[0])) for row in rows]

    def learn(self, scope, rules):
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO rules VALUES (?, ?)",
                [
                    (
                        scope,
                        json.dumps(
                            {
                                "name": r.name,
                                "pattern": r.pattern,
                                "paths": r.paths,
                            },
                            sort_keys=True,
                        ),
                    )
                    for r in rules
                ],
            )

    def index(self, scope, key):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO candidates VALUES (?, ?)", (scope, key)
            )

    def candidates(self, scope, limit):
        with self.connect() as db:
            rows = db.execute(
                "SELECT e.key, e.created, e.payload FROM candidates c JOIN entries e ON e.key=c.key "
                "WHERE c.scope=? ORDER BY e.created DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [
            (key, created, json.loads(payload))
            for key, created, payload in rows
        ]

    def verified(self, key):
        with self.connect() as db:
            rows = db.execute(
                "SELECT rule FROM verified_rules WHERE key=?", (key,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def count_signatures(self, scope, signatures, hit):
        now = time.time()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO signature_counts VALUES (?, ?, ?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(scope, kind, signature) DO UPDATE SET "
                "requests=requests+1, hits=hits+excluded.hits, "
                "misses=misses+excluded.misses, last_seen=excluded.last_seen",
                [
                    (scope, kind, value, int(hit), int(not hit), now, now)
                    for kind, value in signatures.items()
                ],
            )

    def index_signatures(self, scope, signatures, key):
        with self.connect() as db:
            db.executemany(
                "INSERT OR REPLACE INTO signature_entries VALUES (?, ?, ?, ?)",
                [
                    (scope, kind, value, key)
                    for kind, value in signatures.items()
                ],
            )

    def signature_candidates(self, scope, signatures, limit):
        found = {}
        # The lexical bucket preserves known words and gets first priority.
        for kind, value in reversed(list(signatures.items())):
            with self.connect() as db:
                rows = db.execute(
                    "SELECT e.key, e.created, e.payload FROM signature_entries s "
                    "JOIN entries e ON e.key=s.key "
                    "WHERE s.scope=? AND s.kind=? AND s.signature=? "
                    "ORDER BY e.created DESC, e.key LIMIT ?",
                    (scope, kind, value, limit),
                ).fetchall()
            for key, created, payload in rows:
                found.setdefault(key, (key, created, json.loads(payload), kind))
        for key, created, payload in self.candidates(scope, limit):
            found.setdefault(key, (key, created, payload, "recent"))
        return list(found.values())

    def signature_stats(self, limit=20):
        with self.connect() as db:
            cursor = db.execute(
                "SELECT * FROM signature_counts ORDER BY requests DESC, "
                "scope, kind, signature LIMIT ?",
                (limit,),
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def approve(self, key, rule):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO verified_rules VALUES (?, ?)",
                (key, json.dumps(rule, sort_keys=True)),
            )

    def revoke(self, key, rule):
        with self.connect() as db:
            db.execute(
                "DELETE FROM verified_rules WHERE key=? AND rule=?",
                (key, json.dumps(rule, sort_keys=True)),
            )


@lru_cache(maxsize=32)
def _store(path):
    return Store(path)


def get_store(path):
    # lru_cache alone can run its factory concurrently on the first lookup.
    with _stores_lock:
        return _store(path)
